#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Raspberry Pi - AODV only (tanpa PSO-GA) + logging metrik AODV (RSSI, PDR, End-to-End Delay)
ke database statistik_jaringan (tabel baru khusus AODV).

KOMPATIBILITAS PROTOKOL (mengikuti program lama):
- UDP broadcast dengan header: [pkt_type:1][src_mac:6][dst_mac:6][ttl:1] + payload JSON utf-8
- Port: 5000
- Packet types:
  PKT_HELLO=0, PKT_RREQ=1, PKT_RREP=2, PKT_DATA=3, PKT_RERR=4, PKT_ACK=7
- Payload DATA memuat: packet_id (int), source, destination, timestamp, path/route (list)
- ACK memuat: packet_id, sent_ts, ack_ts, route, hop_metrics(list of dict with rssi/delay/pdr optional)

Yang direkam ke DB:
- RSSI (diambil dari hop_metrics jika ada; fallback ke RSSI lokal)
- PDR end-to-end berbasis window ACK/sent (window_pdr)
- End-to-End delay (ms) berbasis ACK

Catatan penting:
- AODV forwarding multi-hop di lapangan bergantung implementasi ESP32 Anda.
  Script ini melakukan route discovery (RREQ/RREP) dan akan mengirim DATA via next_hop jika ada.
  Jika route tidak ditemukan, script akan fallback kirim langsung ke MAC destination jika MAC-nya diketahui.
"""

import json
import socket
import struct
import time
import threading
import random
import re
import platform
import subprocess
from collections import deque, defaultdict
import logging

import mysql.connector
from mysql.connector import Error

# ================== KONSTAN ==================
BROADCAST_ADDR = "255.255.255.255"
UDP_PORT = 5000

HELLO_INTERVAL = 2
ROUTE_TIMEOUT = 10
MAX_HOPS = 10

SINK_NODE_ID = 0  # default tujuan pengujian (ubah jika perlu)

# Window untuk PDR end-to-end (ACK)
E2E_WINDOW_SEC = 60

# Packet types (harus sama dengan ESP32)
PKT_HELLO = 0
PKT_RREQ = 1
PKT_RREP = 2
PKT_DATA = 3
PKT_RERR = 4
PKT_ACK = 7

# ======== MAC addresses (sesuaikan dengan perangkat Anda) ========
MAC_ADDRESSES = {
    0: "78:1C:3C:B9:78:54",  # Sink node (ESP32)
    1: "6C:C8:40:4D:C9:3C",
    2: "1C:69:20:B8:D9:60",
    3: "78:1C:3C:B7:DF:20",
    # 4: isi MAC RPi/ESP32 sesuai kebutuhan bila ada
}

RASPBERRY_PI_ID = "RPI-" + platform.node()

logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s - %(message)s', datefmt='%H:%M:%S')
logger = logging.getLogger(__name__)
print_lock = threading.Lock()


def safe_print(message, banner=False):
    with print_lock:
        if banner:
            print("\n" + "=" * 60)
            print(f"{message:^60}")
            print("=" * 60)
        else:
            print(message)

# ================== DATABASE (AODV only) ==================


class DatabaseHandler:
    """
    Tabel baru khusus AODV: aodv_metrics

    Menyimpan 3 metrik:
    - rssi_dbm
    - pdr_pct  (window end-to-end)
    - e2e_delay_ms
    """
    def __init__(self, host, database, user, password):
        self.host, self.database, self.user, self.password = host, database, user, password
        self.connection = None
        self.connect()

    def connect(self):
        try:
            self.connection = mysql.connector.connect(
                host=self.host, database=self.database, user=self.user, password=self.password
            )
            if self.connection.is_connected():
                safe_print("Berhasil terhubung ke database MySQL", banner=True)
                self.create_table()
        except Error as e:
            safe_print(f"Error connecting to MySQL: {e}")

    def check_connection(self):
        try:
            if self.connection is None or not self.connection.is_connected():
                self.connect()
            return self.connection is not None and self.connection.is_connected()
        except Error:
            try:
                self.connect()
                return self.connection is not None and self.connection.is_connected()
            except Error as e:
                safe_print(f"Failed to reconnect to database: {e}")
                return False

    def create_table(self):
        q = """
        CREATE TABLE IF NOT EXISTS aodv_metrics (
            id BIGINT AUTO_INCREMENT PRIMARY KEY,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
            packet_id VARCHAR(80) NOT NULL,
            source_node INT NOT NULL,
            destination_node INT NOT NULL,
            rssi_dbm FLOAT NULL,
            pdr_pct FLOAT NULL,
            e2e_delay_ms FLOAT NULL,
            route TEXT NULL,
            hops INT NULL,
            raspberry_id VARCHAR(50)
        )
        """
        try:
            c = self.connection.cursor()
            c.execute(q)
            self.connection.commit()
            safe_print("Tabel aodv_metrics siap digunakan (AODV only)")
        except Error as e:
            safe_print(f"Error creating table aodv_metrics: {e}")

    def insert_metric(self, packet_id, source_node, destination_node,
                      rssi_dbm, pdr_pct, e2e_delay_ms, route=None, hops=None):
        if not self.check_connection():
            return None

        q = """
        INSERT INTO aodv_metrics
        (packet_id, source_node, destination_node, rssi_dbm, pdr_pct, e2e_delay_ms, route, hops, raspberry_id)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """
        try:
            c = self.connection.cursor()
            c.execute(q, (str(packet_id), int(source_node), int(destination_node),
                          rssi_dbm, pdr_pct, e2e_delay_ms,
                          route, hops, RASPBERRY_PI_ID))
            self.connection.commit()
            return c.lastrowid
        except Error as e:
            safe_print(f"Error inserting aodv_metrics: {e}")
            return None

    def close_connection(self):
        if self.connection and self.connection.is_connected():
            self.connection.close()
            safe_print("Koneksi database ditutup")


# ================== NODE AODV ONLY ==================


class AODVNode:
    def __init__(self, node_id, db_cfg=None):
        self.node_id = int(node_id)
        self.mac_address = self.normalize_mac(MAC_ADDRESSES.get(node_id, self.generate_mac()))
        self.sequence_num = 0

        # routing_table: dest_id -> {next_hop, hop_count, seq_num, last_update, path}
        self.routing_table = {}
        # reverse route untuk origin rreq: origin_id -> next_hop (kembali)
        self.reverse_route = {}

        # seen RREQ untuk mencegah flood loop: (origin, rreq_id)
        self.seen_rreq = set()

        # kondisi untuk menunggu RREP
        self._rrep_cv = threading.Condition()
        self._last_rrep_time = 0.0

        # ===== End-to-End tracking =====
        # packet_id (string) -> dict(dest, t0, route, hops)
        self.e2e_pending = {}
        # dest -> deque[(t_sent, packet_id)]
        self.e2e_sent_log = defaultdict(lambda: deque(maxlen=5000))
        # dest -> deque[(t_ack, packet_id)]
        self.e2e_ack_log = defaultdict(lambda: deque(maxlen=5000))
        self.e2e_seen_acks = set()

        # Socket
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.socket.settimeout(0.1)
        self.socket.bind(('0.0.0.0', UDP_PORT))

        # DB handler
        self.db_handler = None
        if db_cfg:
            self.db_handler = DatabaseHandler(**db_cfg)

        # Threads
        self.recv_thread = threading.Thread(target=self.receive_packets, daemon=True)
        self.recv_thread.start()
        self.hello_thread = threading.Thread(target=self.send_hello_periodic, daemon=True)
        self.hello_thread.start()

        safe_print(f"Raspberry Pi di Node {self.node_id}", banner=True)
        safe_print(f"MAC: {self.mac_address}")
        safe_print(f"ID Unik Raspberry Pi: {RASPBERRY_PI_ID}")
        safe_print(f"MAC addresses yang dikenal: {MAC_ADDRESSES}")

    # ---------- Util ----------
    def is_valid_mac(self, mac_address):
        return bool(re.compile(r'^([0-9A-Fa-f]{2}[:-]){5}([0-9A-Fa-f]{2})|([0-9A-Fa-f]{12})$').match(mac_address))

    def normalize_mac(self, mac_address):
        if not self.is_valid_mac(mac_address):
            return self.generate_mac()
        return mac_address.replace(':', '').replace('-', '').upper()

    def denormalize_mac(self, mac12):
        mac12 = self.normalize_mac(mac12)
        return ':'.join(mac12[i:i+2] for i in range(0, 12, 2))

    def generate_mac(self):
        return ''.join([f"{random.randint(0, 255):02X}" for _ in range(6)])

    def get_next_sequence_num(self):
        self.sequence_num += 1
        return self.sequence_num

    def mac_to_node_id(self, mac_address):
        nm = self.normalize_mac(mac_address)
        for nid, mac in MAC_ADDRESSES.items():
            if self.normalize_mac(mac) == nm:
                return int(nid)
        return -1

    def node_id_to_mac(self, node_id):
        mac = MAC_ADDRESSES.get(int(node_id), "")
        if not self.is_valid_mac(mac):
            return ""
        return self.normalize_mac(mac)

    def mac_to_bytes(self, mac_address):
        return bytes.fromhex(self.normalize_mac(mac_address))

    def bytes_to_mac(self, mac_bytes):
        return ':'.join(f'{b:02X}' for b in mac_bytes)

    # ---------- Metrik aktual ----------
    def get_actual_rssi(self):
        """
        RSSI di RPi: diambil dari iwconfig wlan0 (kalau memakai Wi-Fi).
        Jika interface Anda bukan wlan0, ganti di bawah.
        """
        try:
            res = subprocess.check_output(['iwconfig', 'wlan0'], text=True)
            m = re.search(r'Signal level=(-?\d+) dBm', res)
            if m:
                return float(m.group(1))
        except Exception:
            pass
        return None  # jangan paksa -80 agar tidak "palsu"

    # ---------- Transmisi ----------
    def send_packet(self, packet_type, dest_mac, data=None, ttl=MAX_HOPS):
        try:
            if dest_mac == "BROADCAST":
                dest_mac = "FF:FF:FF:FF:FF:FF"
            header = struct.pack(
                '!B6s6sB',
                packet_type,
                self.mac_to_bytes(self.mac_address),
                self.mac_to_bytes(dest_mac),
                int(ttl)
            )
            packet = header + (data.encode('utf-8') if data else b'')
            self.socket.sendto(packet, (BROADCAST_ADDR, UDP_PORT))
        except Exception as e:
            safe_print(f"Error mengirim paket: {e}")

    # ---------- HELLO ----------
    def send_hello(self):
        data = json.dumps({
            'node_id': self.node_id,
            'seq_num': self.get_next_sequence_num(),
            'timestamp': time.time(),
            'mac_address': self.mac_address,
            'raspberry_id': RASPBERRY_PI_ID,
            'type': 'raspberry_pi_hello'
        })
        self.send_packet(PKT_HELLO, "BROADCAST", data)

    def send_hello_periodic(self):
        while True:
            self.send_hello()
            time.sleep(HELLO_INTERVAL)

    # ---------- AODV: RREQ / RREP ----------
    def send_rreq(self, dest_id):
        rreq_id = self.get_next_sequence_num()
        data = json.dumps({
            'origin_id': self.node_id,
            'dest_id': int(dest_id),
            'rreq_id': rreq_id,
            'hop_count': 0,
            'timestamp': time.time()
        })
        self.seen_rreq.add((self.node_id, rreq_id))
        self.send_packet(PKT_RREQ, "BROADCAST", data)
        safe_print(f"RREQ dikirim: origin={self.node_id} dest={dest_id} rreq_id={rreq_id}", banner=True)

    def send_rrep(self, origin_id, dest_id, rreq_id, hop_count, next_hop_to_origin):
        """
        RREP unicast ke origin mengikuti reverse_route.
        """
        payload = json.dumps({
            'origin_id': int(origin_id),
            'dest_id': int(dest_id),
            'rreq_id': int(rreq_id),
            'hop_count': int(hop_count),
            'timestamp': time.time()
        })
        nh_mac = self.node_id_to_mac(next_hop_to_origin)
        if nh_mac:
            self.send_packet(PKT_RREP, nh_mac, payload)
            safe_print(f"RREP dikirim ke origin={origin_id} via next_hop={next_hop_to_origin}", banner=True)

    def route_valid(self, info):
        return info and (time.time() - info.get('last_update', 0) <= ROUTE_TIMEOUT)

    def discover_route(self, dest_id, wait_sec=2.0, retries=2):
        """
        Discover route ke dest_id. Mengirim RREQ dan menunggu RREP.
        """
        dest_id = int(dest_id)
        if dest_id in self.routing_table and self.route_valid(self.routing_table[dest_id]):
            return True

        for _ in range(retries + 1):
            self.send_rreq(dest_id)
            end = time.time() + wait_sec
            with self._rrep_cv:
                while time.time() < end:
                    if dest_id in self.routing_table and self.route_valid(self.routing_table[dest_id]):
                        return True
                    self._rrep_cv.wait(timeout=0.2)
        return dest_id in self.routing_table and self.route_valid(self.routing_table[dest_id])

    # ---------- DATA / ACK ----------
    def send_data(self, destination_id, message):
        destination_id = int(destination_id)

        # 1) coba AODV discovery dulu
        ok = self.discover_route(destination_id)
        route_info = self.routing_table.get(destination_id) if ok else None

        # fallback: kalau tidak ada route, coba direct send (MAC harus known)
        direct_mac = self.node_id_to_mac(destination_id)

        if (not ok or not route_info) and not direct_mac:
            safe_print(f"Tidak ada rute dan MAC destination tidak dikenal (dest={destination_id})", banner=True)
            return

        # packet_id kompatibel: int (untuk ESP32), tapi kita simpan juga stringnya untuk dict
        pid = random.getrandbits(32) or 1
        packet_id_str = str(pid)

        # tentukan path
        if route_info and 'path' in route_info and isinstance(route_info['path'], list):
            path = route_info['path']
        else:
            path = [self.node_id, destination_id]

        payload = {
            'packet_id': pid,
            'payload': message,
            'source': self.node_id,
            'destination': destination_id,
            'timestamp': time.time(),
            'raspberry_id': RASPBERRY_PI_ID,
            'type': 'raspberry_pi_data',
            'path': path,
            'route': path
        }

        t0 = payload['timestamp']
        self.e2e_pending[packet_id_str] = {
            'dest': destination_id,
            't0': t0,
            'route': path,
            'hops': max(0, len(path) - 1)
        }
        self.e2e_sent_log[destination_id].append((t0, packet_id_str))

        # next hop: jika route ada, kirim ke next_hop; jika tidak, direct ke dest
        if route_info and 'next_hop' in route_info:
            next_hop = int(route_info['next_hop'])
            nh_mac = self.node_id_to_mac(next_hop)
            if nh_mac:
                self.send_packet(PKT_DATA, nh_mac, json.dumps(payload))
                safe_print(f"DATA dikirim ke dest={destination_id} via next_hop={next_hop} pid={pid}", banner=True)
                return

        # fallback direct
        self.send_packet(PKT_DATA, self.denormalize_mac(direct_mac), json.dumps(payload))
        safe_print(f"DATA (direct) dikirim ke dest={destination_id} pid={pid}", banner=True)

    def send_ack(self, dest_id, packet_id, sent_ts, route=None, hop_metrics=None):
        """
        Kirim ACK balik untuk hitung end-to-end di source (sama seperti versi lama).
        """
        try:
            payload = {
                'packet_id': packet_id,
                'sent_ts': sent_ts,
                'ack_ts': time.time(),
                'source': self.node_id,       # ACK sender (destination dari DATA)
                'destination': int(dest_id),  # ACK receiver (source dari DATA)
                'route': route or [],
                'hop_metrics': hop_metrics or [],
                'raspberry_id': RASPBERRY_PI_ID,
                'type': 'ack'
            }

            # route balik: kalau ada route ke dest_id gunakan next_hop, kalau tidak direct
            if int(dest_id) in self.routing_table and self.route_valid(self.routing_table[int(dest_id)]):
                nh = int(self.routing_table[int(dest_id)]['next_hop'])
                nh_mac = self.node_id_to_mac(nh)
                if nh_mac:
                    self.send_packet(PKT_ACK, nh_mac, json.dumps(payload))
                    return

            dest_mac = self.denormalize_mac(self.node_id_to_mac(dest_id))
            if dest_mac:
                self.send_packet(PKT_ACK, dest_mac, json.dumps(payload))
        except Exception as e:
            safe_print(f"Error mengirim ACK: {e}")

    def _e2e_window_pdr(self, dest_id, window_sec=E2E_WINDOW_SEC):
        now = time.time()
        sent_dq = self.e2e_sent_log.get(dest_id, deque())
        ack_dq = self.e2e_ack_log.get(dest_id, deque())

        while sent_dq and (now - sent_dq[0][0] > window_sec):
            sent_dq.popleft()
        while ack_dq and (now - ack_dq[0][0] > window_sec):
            ack_dq.popleft()

        sent = len(sent_dq)
        ack = len(ack_dq)
        pdr = (ack / sent * 100.0) if sent > 0 else 0.0
        return max(0.0, min(100.0, pdr))

    # ---------- RX ----------
    def receive_packets(self):
        while True:
            try:
                raw, _ = self.socket.recvfrom(2048)
                if len(raw) < 14:
                    continue

                packet_type = struct.unpack('!B', raw[0:1])[0]
                source_mac = self.bytes_to_mac(raw[1:7])
                dest_mac = self.bytes_to_mac(raw[7:13])
                ttl = struct.unpack('!B', raw[13:14])[0]

                if ttl <= 0:
                    continue

                # hanya terima jika broadcast atau ke saya
                if self.normalize_mac(dest_mac) not in (self.mac_address, "FFFFFFFFFFFF"):
                    continue

                payload = raw[14:].decode('utf-8', errors='ignore') if len(raw) > 14 else None
                source_id = self.mac_to_node_id(source_mac)

                if packet_type == PKT_HELLO:
                    self.process_hello(source_id, payload)
                elif packet_type == PKT_RREQ:
                    self.process_rreq(source_id, payload, ttl)
                elif packet_type == PKT_RREP:
                    self.process_rrep(source_id, payload, ttl)
                elif packet_type == PKT_DATA:
                    self.process_data(source_id, payload, ttl)
                elif packet_type == PKT_ACK:
                    self.process_ack(source_id, payload, ttl)
                elif packet_type == PKT_RERR:
                    self.process_rerr(source_id, payload)

            except socket.timeout:
                continue
            except Exception as e:
                safe_print(f"Error menerima paket: {e}")

    # ---------- Handlers ----------
    def process_hello(self, source_id, payload):
        # HELLO hanya untuk deteksi; metrik 3 item direkam di ACK end-to-end.
        try:
            if not payload:
                return
            d = json.loads(payload)
            nid = d.get('node_id', -1)
            if nid == -1:
                return
            # optional: bisa update neighbor list / last seen
        except Exception:
            return

    def process_rreq(self, source_id, payload, ttl):
        try:
            if not payload:
                return
            d = json.loads(payload)
            origin_id = int(d.get('origin_id', -1))
            dest_id = int(d.get('dest_id', -1))
            rreq_id = int(d.get('rreq_id', -1))
            hop_count = int(d.get('hop_count', 0))

            if origin_id == -1 or dest_id == -1 or rreq_id == -1 or source_id == -1:
                return

            key = (origin_id, rreq_id)
            if key in self.seen_rreq:
                return
            self.seen_rreq.add(key)

            # simpan reverse route untuk balasan RREP
            self.reverse_route[origin_id] = {
                'next_hop': source_id,
                'hop_count': hop_count + 1,
                'last_update': time.time()
            }

            # jika saya tujuan -> kirim RREP balik
            if dest_id == self.node_id:
                rr = self.reverse_route.get(origin_id)
                if rr and (time.time() - rr['last_update'] <= ROUTE_TIMEOUT):
                    self.send_rrep(origin_id, dest_id, rreq_id, 0, rr['next_hop'])
                return

            # jika saya punya route ke dest, bisa balas RREP (optional)
            if dest_id in self.routing_table and self.route_valid(self.routing_table[dest_id]):
                rr = self.reverse_route.get(origin_id)
                if rr and (time.time() - rr['last_update'] <= ROUTE_TIMEOUT):
                    # hop_count yang dikirim = hop_count dari saya ke dest
                    self.send_rrep(origin_id, dest_id, rreq_id, int(self.routing_table[dest_id]['hop_count']), rr['next_hop'])
                return

            # forward RREQ
            if ttl - 1 <= 0:
                return
            d['hop_count'] = hop_count + 1
            self.send_packet(PKT_RREQ, "BROADCAST", json.dumps(d), ttl=ttl - 1)

        except Exception as e:
            safe_print(f"Error memproses RREQ: {e}")

    def process_rrep(self, source_id, payload, ttl):
        try:
            if not payload:
                return
            d = json.loads(payload)
            origin_id = int(d.get('origin_id', -1))
            dest_id = int(d.get('dest_id', -1))
            rreq_id = int(d.get('rreq_id', -1))
            hop_count = int(d.get('hop_count', 0))

            if origin_id == -1 or dest_id == -1 or source_id == -1:
                return

            # update forward route ke dest_id via source_id
            new_hops = hop_count + 1
            cur = self.routing_table.get(dest_id)
            if (cur is None) or (not self.route_valid(cur)) or (new_hops < cur.get('hop_count', 9999)):
                # path belum bisa dihitung akurat tanpa membawa path; minimal simpan [self, source, dest]
                self.routing_table[dest_id] = {
                    'next_hop': source_id,
                    'hop_count': new_hops,
                    'seq_num': rreq_id,
                    'last_update': time.time(),
                    'path': [self.node_id, source_id, dest_id]
                }

            # jika saya origin, bangunkan waiter
            if origin_id == self.node_id:
                with self._rrep_cv:
                    self._last_rrep_time = time.time()
                    self._rrep_cv.notify_all()
                return

            # forward RREP balik ke origin mengikuti reverse_route
            rr = self.reverse_route.get(origin_id)
            if not rr or (time.time() - rr['last_update'] > ROUTE_TIMEOUT):
                return
            if ttl - 1 <= 0:
                return
            next_hop = int(rr['next_hop'])
            nh_mac = self.node_id_to_mac(next_hop)
            if not nh_mac:
                return
            d['hop_count'] = new_hops
            self.send_packet(PKT_RREP, self.denormalize_mac(nh_mac), json.dumps(d), ttl=ttl - 1)

        except Exception as e:
            safe_print(f"Error memproses RREP: {e}")

    def process_data(self, source_id, payload, ttl):
        """
        Jika DATA untuk saya (destination==self.node_id): kirim ACK balik.
        Jika bukan: forward ke next hop jika ada route.
        """
        try:
            if not payload:
                return
            d = json.loads(payload)
            dest = int(d.get('destination', -1))
            src = int(d.get('source', -1))
            pid = d.get('packet_id', None)
            sent_ts = d.get('timestamp', None)
            path = d.get('path') or d.get('route') or []

            # terima data jika saya dest
            if dest == self.node_id and pid is not None:
                # optional: tambahkan hop metrics (minimal RSSI lokal)
                hop_metrics = d.get('hop_metrics', [])
                if not isinstance(hop_metrics, list):
                    hop_metrics = []

                rssi = self.get_actual_rssi()
                if rssi is not None:
                    hop_metrics.append({'u': source_id, 'v': self.node_id, 'rssi': rssi})

                d['hop_metrics'] = hop_metrics

                # kirim ACK ke source (src)
                self.send_ack(dest_id=src, packet_id=pid, sent_ts=sent_ts, route=path, hop_metrics=hop_metrics)
                return

            # forward jika bukan tujuan
            if ttl - 1 <= 0:
                return

            # cari route ke dest
            if dest in self.routing_table and self.route_valid(self.routing_table[dest]):
                nh = int(self.routing_table[dest]['next_hop'])
                nh_mac = self.node_id_to_mac(nh)
                if nh_mac:
                    self.send_packet(PKT_DATA, self.denormalize_mac(nh_mac), json.dumps(d), ttl=ttl - 1)

        except Exception as e:
            safe_print(f"Error memproses DATA: {e}")

    def process_ack(self, source_id, payload, ttl):
        """
        Hitung e2e delay, rssi, pdr(window) lalu simpan ke DB (aodv_metrics).
        """
        try:
            if not payload:
                return
            d = json.loads(payload)
            packet_id_raw = d.get('packet_id', None)
            if packet_id_raw is None:
                return

            packet_id = str(packet_id_raw)
            if packet_id in self.e2e_seen_acks:
                return
            self.e2e_seen_acks.add(packet_id)

            sent_ts = d.get('sent_ts', None)
            ack_ts = d.get('ack_ts', time.time())
            route = d.get('route') or []
            hop_metrics = d.get('hop_metrics') or []

            pending = self.e2e_pending.pop(packet_id, None)
            if pending:
                dest_id = pending['dest']
                t0 = pending['t0']
                route = route or pending.get('route', route)
                hops = pending.get('hops', max(0, len(route) - 1))
            else:
                # fallback
                dest_id = int(d.get('source', -1))  # ACK sender = destination dari DATA
                t0 = sent_ts if sent_ts is not None else None
                hops = max(0, len(route) - 1)

            # e2e delay
            e2e_delay_ms = None
            if t0 is not None:
                try:
                    e2e_delay_ms = max(0.0, (time.time() - float(t0)) * 1000.0)
                except Exception:
                    e2e_delay_ms = None

            # RSSI: ambil dari hop_metrics jika ada, kalau tidak fallback ke RSSI lokal
            rssi_val = None
            rssi_list = []
            if isinstance(hop_metrics, list):
                for hm in hop_metrics:
                    if isinstance(hm, dict) and hm.get('rssi') is not None:
                        try:
                            rssi_list.append(float(hm['rssi']))
                        except Exception:
                            pass
            if rssi_list:
                rssi_val = sum(rssi_list) / len(rssi_list)
            else:
                rssi_val = self.get_actual_rssi()

            # update ack log untuk PDR window
            if dest_id is not None and dest_id != -1:
                self.e2e_ack_log[dest_id].append((time.time(), packet_id))

            pdr_window = self._e2e_window_pdr(dest_id) if dest_id is not None and dest_id != -1 else None

            route_str = ' '.join(map(str, route)) if isinstance(route, list) else None

            safe_print("E2E ACK diterima (AODV)", banner=True)
            safe_print(f"   PacketID: {packet_id} | Dest: {dest_id}")
            if pdr_window is not None:
                safe_print(f"   PDR(window {E2E_WINDOW_SEC}s): {pdr_window:.1f}%")
            if e2e_delay_ms is not None:
                safe_print(f"   E2E Delay: {e2e_delay_ms:.1f} ms")
            if rssi_val is not None:
                safe_print(f"   RSSI: {rssi_val:.1f} dBm")
            if route_str:
                safe_print(f"   Route: {route_str}")

            # simpan ke DB
            if self.db_handler and dest_id is not None and dest_id != -1:
                self.db_handler.insert_metric(
                    packet_id=packet_id,
                    source_node=self.node_id,
                    destination_node=dest_id,
                    rssi_dbm=rssi_val,
                    pdr_pct=pdr_window,
                    e2e_delay_ms=e2e_delay_ms,
                    route=route_str,
                    hops=hops
                )

        except Exception as e:
            safe_print(f"Error memproses ACK: {e}")

    def process_rerr(self, source_id, payload):
        # minimal handler: hapus route jika ada
        try:
            if not payload:
                return
            d = json.loads(payload)
            unreachable = int(d.get('unreachable_node', -1))
            if unreachable != -1 and unreachable in self.routing_table:
                del self.routing_table[unreachable]
        except Exception:
            return

    # ---------- Debug status ----------
    def print_status(self):
        safe_print(f"STATUS Node {self.node_id} (AODV)", banner=True)
        now = time.time()
        safe_print("Routing table:")
        for dest, info in self.routing_table.items():
            st = "Aktif" if self.route_valid(info) else "Kadaluarsa"
            path = info.get('path', [])
            safe_print(f"  {st} dest={dest} next_hop={info.get('next_hop')} hops={info.get('hop_count')} path={path}")

        safe_print(f"End-to-End PDR window {E2E_WINDOW_SEC}s:")
        for dest in sorted(self.e2e_sent_log.keys()):
            safe_print(f"  dest={dest} pdr={self._e2e_window_pdr(dest):.1f}% sent={len(self.e2e_sent_log[dest])} ack={len(self.e2e_ack_log[dest])}")


# ================== MAIN ==================
if __name__ == "__main__":
    # Default: node 4 seperti program lama (ubah sesuai kebutuhan)
    NODE_ID = 4

    db_cfg = {
        "host": "192.168.11.100",
        "database": "statistik_jaringan",
        "user": "piuser",
        "password": "passwordku"
    }

    safe_print(f"Memulai Raspberry Pi AODV-only di Node {NODE_ID}", banner=True)
    safe_print(f"ID Unik: {RASPBERRY_PI_ID}")

    node = AODVNode(NODE_ID, db_cfg=db_cfg)
    time.sleep(3)

    try:
        counter = 0
        while True:
            msg = f"Pesan dari Raspberry Pi {RASPBERRY_PI_ID} - #{counter}"
            node.send_data(SINK_NODE_ID, msg)

            if counter % 3 == 0:
                node.print_status()

            counter += 1
            time.sleep(8)

    except KeyboardInterrupt:
        safe_print("\nRaspberry Pi dihentikan", banner=True)
        node.print_status()
        if node.db_handler:
            node.db_handler.close_connection()
