import socket
import struct
import time
import threading
import random
import math
import json
import re
import platform
import psutil
import numpy as np
from collections import deque, defaultdict
import logging
import mysql.connector
from mysql.connector import Error
import subprocess

# ================== KONSTAN ==================
BROADCAST_ADDR = "255.255.255.255"
UDP_PORT = 5000
HELLO_INTERVAL = 2
ROUTE_TIMEOUT = 10
MAX_HOPS = 10
OPTIMIZATION_INTERVAL = 15
SINK_NODE_ID = 0  # <<< DB hanya merekam rute ke sink

# (Opsional) Batas bawah latensi untuk redam noise pengukuran
MIN_DELAY_MS = 10        # gunakan None bila ingin raw tanpa batas bawah

PKT_HELLO = 0
PKT_RREQ = 1
PKT_RREQ_METRIC = 5
PKT_RREP = 2
PKT_DATA = 3
PKT_DATA_METRIC = 6
PKT_RERR = 4

PKT_ACK = 7  # End-to-end ACK

# ===== End-to-End metrics windowing =====
E2E_WINDOW_SEC = 60      # window untuk hitung PDR end-to-end
ACK_TIMEOUT_SEC = 5      # (opsional) timeout monitoring ACK
MAC_ADDRESSES = {
    0: "78:1C:3C:B9:78:54",  # Sink node (ESP32)
    1: "6C:C8:40:4D:C9:3C",
    2: "1C:69:20:B8:D9:60",
    3: "78:1C:3C:B7:DF:20",
}

RASPBERRY_PI_ID = "RPI-" + platform.node()

logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s - %(message)s', datefmt='%H:%M:%S')
logger = logging.getLogger(__name__)
print_lock = threading.Lock()

# ================== PSO-GA PARAM ==================
PSO_POPULATION_SIZE = 30
PSO_MAX_ITERATIONS = 50
PSO_CONVERGENCE_THRESHOLD = 0.001
PSO_NO_IMPROVEMENT_LIMIT = 10
PSO_C1 = 1.5
PSO_C2 = 1.5
PSO_W = 0.7
GA_CROSSOVER_RATE = 0.8
GA_MUTATION_RATE = 0.05


def safe_print(message, banner=False):
    with print_lock:
        if banner:
            print("\n" + "="*60)
            print(f"{message:^60}")
            print("="*60)
        else:
            print(message)

# ================== DATABASE ==================


class DatabaseHandler:
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
                return True
            return True
        except Error:
            try:
                self.connect()
                return True
            except Error as e:
                safe_print(f"Failed to reconnect to database: {e}")
                return False

    def create_table(self):
        q = """
        CREATE TABLE IF NOT EXISTS rute_optimal_psoga (
            id INT AUTO_INCREMENT PRIMARY KEY,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
            source_node INT NOT NULL,
            destination_node INT NOT NULL,
            best_route TEXT NOT NULL,
            fitness FLOAT NOT NULL,
            avg_rssi FLOAT,
            avg_latency FLOAT,
            avg_pdr FLOAT,
            iterations INT,
            raspberry_id VARCHAR(50)
        )
        """
        try:
            c = self.connection.cursor()
            c.execute(q)
            self.connection.commit()
            safe_print("Tabel rute_optimal_psoga siap digunakan")
            self.create_table_e2e()
        except Error as e:
            safe_print(f"Error creating table: {e}")

    def insert_optimized_route(self, source_node, destination_node, best_route,
                               fitness, avg_rssi, avg_latency, avg_pdr, iterations):
        # Guard: simpan hanya untuk tujuan sink
        if destination_node != SINK_NODE_ID:
            return None

        if not self.check_connection():
            safe_print(
                "Tidak dapat terhubung ke database, melewatkan penyimpanan")
            return None

        q = """
        INSERT INTO rute_optimal_psoga
        (source_node, destination_node, best_route, fitness,
         avg_rssi, avg_latency, avg_pdr, iterations, raspberry_id)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        """
        try:
            c = self.connection.cursor()
            c.execute(q, (source_node, destination_node, best_route, fitness,
                          avg_rssi, avg_latency, avg_pdr, iterations, RASPBERRY_PI_ID))
            self.connection.commit()
            safe_print("Hasil optimasi berhasil disimpan ke database")
            return c.lastrowid
        except Error as e:
            safe_print(f"Error inserting data: {e}")
            return None
    # ===== END-TO-END METRICS TABLE =====

    def create_table_e2e(self):
        q = """
        CREATE TABLE IF NOT EXISTS e2e_metrics_psoga (
            id INT AUTO_INCREMENT PRIMARY KEY,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
            packet_id VARCHAR(80) NOT NULL,
            source_node INT NOT NULL,
            destination_node INT NOT NULL,
            route TEXT,
            hops INT,
            e2e_delay_ms FLOAT,
            e2e_rssi_min FLOAT,
            e2e_rssi_avg FLOAT,
            success TINYINT(1) NOT NULL,
            window_pdr FLOAT,
            raspberry_id VARCHAR(50)
        )
        """
        try:
            c = self.connection.cursor()
            c.execute(q)
            self.connection.commit()
            safe_print("Tabel e2e_metrics_psoga siap digunakan")
        except Error as e:
            safe_print(f"Error creating e2e table: {e}")

    def insert_e2e_metric(self, packet_id, source_node, destination_node, route, hops,
                          e2e_delay_ms, e2e_rssi_min, e2e_rssi_avg, success, window_pdr):
        if not self.check_connection():
            return None
        q = """
        INSERT INTO e2e_metrics_psoga
        (packet_id, source_node, destination_node, route, hops, e2e_delay_ms,
         e2e_rssi_min, e2e_rssi_avg, success, window_pdr, raspberry_id)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """
        try:
            c = self.connection.cursor()
            c.execute(q, (packet_id, source_node, destination_node, route, hops,
                          e2e_delay_ms, e2e_rssi_min, e2e_rssi_avg, int(success), window_pdr, RASPBERRY_PI_ID))
            self.connection.commit()
            return c.lastrowid
        except Error as e:
            safe_print(f"Error inserting e2e metric: {e}")
            return None

    def close_connection(self):
        if self.connection and self.connection.is_connected():
            self.connection.close()
            safe_print("Koneksi database ditutup")

# ================== PSO-GA ==================


class PSOGAOptimizer:
    def __init__(self, network_topology, node_id, node=None):
        self.network_topology = network_topology
        self.node_id = node_id
        self.node = node
        self.population = []
        self.global_best = None
        self.global_best_fitness = float('-inf')
        self.convergence_count = 0
        self.iteration_count = 0

    def _get_edge_metric(self, u, v, key, default):
        # Prioritas: edge (u->v) di RPi -> fallback ke node v -> default
        if self.node and (u, v) in self.node.edge_metrics:
            em = self.node.edge_metrics[(u, v)]
            dq = em.get(key)
            if dq:
                return sum(dq)/len(dq)
        m = self.network_topology.get(v, {}).get('metrics', {})
        return m.get(key, default)

    def initialize_population(self, destination_id):
        self.population = []
        available_nodes = [n for n in self.network_topology.keys()
                           if n != self.node_id and self.network_topology[n]['active']]
        if not available_nodes:
            safe_print(
                "Tidak ada node yang tersedia untuk inisialisasi populasi")
            return False

        for _ in range(PSO_POPULATION_SIZE):
            max_hops = min(4, len(available_nodes)+1)
            hop_count = random.randint(2, max_hops)
            path = [self.node_id]
            inter = [n for n in available_nodes if n != destination_id]
            if inter:
                for _ in range(hop_count-2):
                    nxt = random.choice(inter)
                    if nxt not in path:
                        path.append(nxt)
            if destination_id not in path:
                path.append(destination_id)
            if len(path) < 2:
                path = [self.node_id, destination_id]

            route = {'path': path, 'next_hop': path[1] if len(
                path) > 1 else destination_id}
            fitness = self.calculate_fitness(route, destination_id)
            self.population.append({
                'position': route,
                'velocity': [random.uniform(0, 1) for _ in range(len(path)-1)],
                'best_position': route,
                'best_fitness': fitness,
                'fitness': fitness
            })
            if fitness > self.global_best_fitness:
                self.global_best_fitness = fitness
                self.global_best = route.copy()
        return True

    def calculate_fitness(self, route, destination_id):
        if 'path' not in route or len(route['path']) < 2:
            return float('-inf')
        path = route['path']
        if path[0] != self.node_id or path[-1] != destination_id:
            return float('-inf')
        if len(path) != len(set(path)):
            return float('-inf')

        total, hops = 0.0, len(path) - 1
        for i in range(hops):
            u, v = path[i], path[i+1]
            rssi = self._get_edge_metric(u, v, 'rssi', -90)
            delay = self._get_edge_metric(u, v, 'delay', 100)
            pdr = self._get_edge_metric(u, v, 'pdr', 50)

            norm_rssi = max(0.0, min(1.0, (rssi + 110.0) / 40.0))
            norm_latency = max(0.0, 1.0 - (delay / 1000.0 / 0.1))
            norm_pdr = pdr / 100.0

            hop_fit = 0.5*norm_rssi + 0.3*norm_latency + 0.2*norm_pdr
            total += hop_fit

        avg = total / hops if hops > 0 else 0.0
        penalty = 1.0 / (1.0 + math.log(1 + hops))
        return avg * penalty

    def calculate_route_metrics(self, route, destination_id):
        if 'path' not in route or len(route['path']) < 2:
            return None, None, None
        path = route['path']
        srssi = sdelay = spdr = 0.0
        hops = len(path) - 1
        for i in range(hops):
            u, v = path[i], path[i+1]
            srssi += self._get_edge_metric(u, v, 'rssi', -80)
            sdelay += self._get_edge_metric(u, v, 'delay', 100)
            spdr += self._get_edge_metric(u, v, 'pdr', 50)
        if hops > 0:
            return srssi/hops, sdelay/hops, spdr/hops
        return None, None, None

    def update_particles(self, destination_id):
        for p in self.population:
            new_v = []
            for i, v in enumerate(p['velocity']):
                r1, r2 = random.random(), random.random()
                cognitive = PSO_C1 * r1 * (p['best_fitness'] - p['fitness'])
                social = PSO_C2 * r2 * \
                    (self.global_best_fitness - p['fitness'])
                new_v.append(PSO_W * v + cognitive + social)
            p['velocity'] = new_v

            available = [n for n in self.network_topology.keys()
                         if n != self.node_id and self.network_topology[n]['active']]
            if not available:
                continue

            cur = p['position']['path'].copy()
            new_path = [self.node_id]
            for i, v in enumerate(p['velocity']):
                if i >= len(cur)-1:
                    break
                idx = int(abs(v) * len(available)) % len(available)
                nxt = available[idx]
                if nxt not in new_path and nxt != destination_id:
                    new_path.append(nxt)
            if destination_id not in new_path:
                new_path.append(destination_id)

            route = {'path': new_path, 'next_hop': new_path[1] if len(
                new_path) > 1 else destination_id}
            fit = self.calculate_fitness(route, destination_id)
            p['position'] = route
            p['fitness'] = fit
            if fit > p['best_fitness']:
                p['best_fitness'] = fit
                p['best_position'] = route.copy()
            if fit > self.global_best_fitness:
                self.global_best_fitness = fit
                self.global_best = route.copy()
                self.convergence_count = 0

    def mutate(self, particle, destination_id):
        available = [n for n in self.network_topology.keys()
                     if n != self.node_id and self.network_topology[n]['active']]
        if not available:
            return
        cur = particle['position']['path'].copy()
        if len(cur) < 2:
            return
        typ = random.choice(['add', 'remove', 'replace'])
        if typ == 'add' and len(cur) < MAX_HOPS and len(available) > 0:
            pos = random.randint(1, len(cur)-1)
            nn = random.choice(available)
            if nn not in cur and nn != destination_id:
                cur.insert(pos, nn)
        elif typ == 'remove' and len(cur) > 2:
            pos = random.randint(1, len(cur)-2)
            cur.pop(pos)
        elif typ == 'replace' and len(available) > 0 and len(cur) > 2:
            pos = random.randint(1, len(cur)-2)
            nn = random.choice(available)
            if nn not in cur and nn != destination_id:
                cur[pos] = nn

        seen, uniq = set(), []
        for n in cur:
            if n not in seen:
                seen.add(n)
                uniq.append(n)
        if uniq[0] != self.node_id:
            uniq.insert(0, self.node_id)
        if uniq[-1] != destination_id:
            uniq.append(destination_id)

        route = {'path': uniq, 'next_hop': uniq[1] if len(
            uniq) > 1 else destination_id}
        fit = self.calculate_fitness(route, destination_id)
        particle['position'] = route
        particle['fitness'] = fit
        if fit > particle['best_fitness']:
            particle['best_fitness'] = fit
            particle['best_position'] = route.copy()

    def roulette_wheel_selection(self):
        total_fit = sum(max(p['fitness'], 0.0) for p in self.population)
        if total_fit <= 0:
            return random.choice(self.population)
        pick = random.uniform(0, total_fit)
        acc = 0.0
        for p in self.population:
            acc += max(p['fitness'], 0.0)
            if acc >= pick:
                return p
        return self.population[-1]

    def arithmetic_crossover(self, parent1, parent2, destination_id):
        alpha = random.uniform(0.25, 0.75)
        mid1 = parent1['position']['path'][1:-1]
        mid2 = parent2['position']['path'][1:-1]
        union_nodes = list(set(mid1 + mid2))

        key_map = {}
        for n in union_nodes:
            pos1 = mid1.index(
                n) / len(mid1) if n in mid1 and len(mid1) > 0 else 1.0
            pos2 = mid2.index(
                n) / len(mid2) if n in mid2 and len(mid2) > 0 else 1.0
            key_map[n] = alpha * pos1 + (1 - alpha) * pos2

        sorted_nodes = [n for n, _ in sorted(
            key_map.items(), key=lambda x: x[1])]
        child_mid = sorted_nodes[:max(1, int(alpha * len(sorted_nodes)))]

        new_path = [self.node_id] + child_mid + [destination_id]
        new_path = list(dict.fromkeys(new_path))
        route = {'path': new_path, 'next_hop': new_path[1] if len(
            new_path) > 1 else destination_id}
        fit = self.calculate_fitness(route, destination_id)
        return {'position': route, 'velocity': [], 'best_position': route, 'best_fitness': fit, 'fitness': fit}

    def apply_genetic_operations(self, destination_id):
        if len(self.population) < 2:
            return

        elite_count = max(1, int(0.1 * len(self.population)))
        elites = sorted(self.population, key=lambda x: x['fitness'], reverse=True)[
            :elite_count]
        next_gen = elites.copy()

        while len(next_gen) < len(self.population):
            p1 = self.roulette_wheel_selection()
            p2 = self.roulette_wheel_selection()
            if random.random() < GA_CROSSOVER_RATE:
                child = self.arithmetic_crossover(p1, p2, destination_id)
            else:
                child = random.choice([p1, p2])
            next_gen.append(child)

        for i in range(elite_count, len(next_gen)):
            if random.random() < GA_MUTATION_RATE:
                self.mutate(next_gen[i], destination_id)

        self.population = next_gen
        for p in self.population:
            if p['fitness'] > self.global_best_fitness:
                self.global_best_fitness = p['fitness']
                self.global_best = p['position'].copy()
                self.convergence_count = 0

    def validate_route(self, route, destination_id):
        if not route or 'path' not in route:
            return False
        path = route['path']
        if len(path) != len(set(path)):
            return False
        if destination_id not in path:
            return False
        if path[0] != self.node_id:
            return False
        return True

    def find_alternative_route(self, destination_id):
        available = [n for n in self.network_topology.keys()
                     if n != self.node_id and self.network_topology[n]['active']]
        if not available:
            return None
        best, bestfit = None, float('-inf')
        for nh in available:
            path = [self.node_id, nh] + \
                ([destination_id] if nh != destination_id else [])
            route = {'path': path, 'next_hop': nh}
            fit = self.calculate_fitness(route, destination_id)
            if fit > bestfit and self.validate_route(route, destination_id):
                bestfit, best = fit, route
        return best

    def optimize_route(self, destination_id):
        safe_print(
            f"Memulai optimasi rute ke Node {destination_id} dengan PSO-GA", banner=True)
        if not self.initialize_population(destination_id):
            return None
        self.iteration_count = 0
        self.convergence_count = 0
        prev = self.global_best_fitness

        while (self.iteration_count < PSO_MAX_ITERATIONS and
               self.convergence_count < PSO_NO_IMPROVEMENT_LIMIT):
            self.iteration_count += 1
            self.update_particles(destination_id)
            self.apply_genetic_operations(destination_id)
            if abs(self.global_best_fitness - prev) < PSO_CONVERGENCE_THRESHOLD:
                self.convergence_count += 1
            else:
                self.convergence_count = 0
            prev = self.global_best_fitness
            safe_print(
                f"Iterasi {self.iteration_count}: Fitness terbaik = {self.global_best_fitness:.4f}")

        safe_print(
            f"Optimasi selesai setelah {self.iteration_count} iterasi", banner=True)

        avg_rssi, avg_latency, avg_pdr = self.calculate_route_metrics(
            self.global_best, destination_id)
        safe_print("HASIL OPTIMASI RUTE", banner=True)
        safe_print(
            f"BestRoute = {' '.join(map(str, self.global_best['path']))}")
        safe_print(f"Fitness = {self.global_best_fitness:.2f}")
        if avg_rssi is not None:
            safe_print(f"AvgRSSI = {avg_rssi:.2f}")
            safe_print(f"AvgLatency = {avg_latency:.2f}")
            safe_print(f"AvgPDR = {avg_pdr:.2f}")
        else:
            safe_print("AvgRSSI = N/A")
            safe_print("AvgLatency = N/A")
            safe_print("AvgPDR = N/A")
        safe_print(f"Iterations = {self.iteration_count}")
        safe_print("=" * 60)

        if not self.validate_route(self.global_best, destination_id):
            safe_print("Peringatan: Rute tidak valid, mencari alternatif...")
            return self.find_alternative_route(destination_id)
        return self.global_best

# ================== NODE AODV ==================


class AODVNode:
    def __init__(self, node_id):
        self.node_id = node_id
        self.mac_address = self.normalize_mac(
            MAC_ADDRESSES.get(node_id, self.generate_mac()))
        self.sequence_num = 0
        self.routing_table = {}
        self.pending_requests = {}
        self.metrics = defaultdict(lambda: {'rssi': deque(maxlen=20),
                                            'delay': deque(maxlen=20),
                                            'pdr': deque(maxlen=20),
                                            'last_update': 0})
        self.hello_times = {}

        # ===== FIX PDR HELLO: simpan timestamp HELLO diterima per node =====
        self.hello_rx_times = defaultdict(lambda: deque(maxlen=200))

        self.message_history = deque(maxlen=50)
        self.network_topology = {}
        self.pso_ga_optimizer = PSOGAOptimizer(
            self.network_topology, self.node_id, self)
        self.route_errors = {}
        self.packet_stats = defaultdict(
            lambda: {'sent': 0, 'received': 0, 'last_seq': -1})

        # ===== END-TO-END METRICS (DATA) =====
        # End-to-end hanya valid jika destination mengirim ACK balik (PKT_ACK).
        self.e2e_pending = {}  # packet_id -> {dest, t0, route, hops}
        # dest -> deque[(t_sent, packet_id)]
        self.e2e_sent_log = defaultdict(lambda: deque(maxlen=5000))
        # dest -> deque[(t_ack, packet_id, delay_ms, rssi_min, rssi_avg, route, hops)]
        self.e2e_ack_log = defaultdict(lambda: deque(maxlen=5000))
        self.e2e_seen_acks = set()  # hindari double count

        self.edge_metrics = defaultdict(lambda: {
            'rssi': deque(maxlen=20),
            'delay': deque(maxlen=20),
            'pdr': deque(maxlen=20),
            'last_update': 0
        })

        # Socket
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.socket.settimeout(0.1)
        self.socket.bind(('0.0.0.0', UDP_PORT))

        # Threads
        self.recv_thread = threading.Thread(
            target=self.receive_packets, daemon=True)
        self.recv_thread.start()
        self.hello_thread = threading.Thread(
            target=self.send_hello_periodic, daemon=True)
        self.hello_thread.start()
        self.optimization_thread = threading.Thread(
            target=self.periodic_optimization, daemon=True)
        self.optimization_thread.start()

        safe_print(f"Raspberry Pi di Node {node_id}", banner=True)
        safe_print(f"MAC: {self.mac_address}")
        safe_print(f"ID Unik Raspberry Pi: {RASPBERRY_PI_ID}")
        safe_print(f"MAC addresses yang dikenal: {MAC_ADDRESSES}")

        self.db_handler = DatabaseHandler(
            host="192.168.11.100",
            database="statistik_jaringan",
            user="piuser",
            password="passwordku"
        )

    def record_edge_metric(self, u, v, rssi, delay, pdr):
        em = self.edge_metrics[(u, v)]
        em['rssi'].append(rssi)
        em['delay'].append(delay)
        em['pdr'].append(pdr)
        em['last_update'] = time.time()

    # ---------- Util ----------
    def is_valid_mac(self, mac_address):
        return bool(re.compile(r'^([0-9A-Fa-f]{2}[:-]){5}([0-9A-Fa-f]{2})|([0-9A-Fa-f]{12})$').match(mac_address))

    def normalize_mac(self, mac_address):
        if not self.is_valid_mac(mac_address):
            return self.generate_mac()
        return mac_address.replace(':', '').replace('-', '').upper()

    def generate_mac(self):
        return ''.join([f"{random.randint(0, 255):02X}" for _ in range(6)])

    def get_next_sequence_num(self):
        self.sequence_num += 1
        return self.sequence_num

    def mac_to_node_id(self, mac_address):
        nm = self.normalize_mac(mac_address)
        for nid, mac in MAC_ADDRESSES.items():
            if self.normalize_mac(mac) == nm:
                return nid
        return -1

    def node_id_to_mac(self, node_id):
        mac = MAC_ADDRESSES.get(node_id, "")
        if not self.is_valid_mac(mac):
            return ""
        return self.normalize_mac(mac)

    def mac_to_bytes(self, mac_address):
        return bytes.fromhex(self.normalize_mac(mac_address))

    def bytes_to_mac(self, mac_bytes):
        return ':'.join(f'{b:02X}' for b in mac_bytes)

    # ---------- Metrik aktual ----------
    def get_actual_rssi(self):
        try:
            res = subprocess.check_output(['iwconfig', 'wlan0'], text=True)
            m = re.search(r'Signal level=(-?\d+) dBm', res)
            if m:
                return float(m.group(1))
        except:
            pass
        return -80

    def calculate_actual_delay(self, packet_timestamp):
        # timestamp invalid (mis. 0 / belum NTP sync di ESP32) -> jangan dihitung sebagai delay
        if packet_timestamp is None or packet_timestamp < 1000000000:
            return (MIN_DELAY_MS if MIN_DELAY_MS is not None else 0.0)

        val = (time.time() - packet_timestamp) * 1000.0

        # kalau clock drift bikin negatif, clamp ke 0 / MIN_DELAY_MS
        if val < 0:
            val = 0.0

        if MIN_DELAY_MS is not None:
            return max(MIN_DELAY_MS, val)
        return val

    # ===== PDR lama (sent/received) cocok untuk DATA/RREQ/RREP unicast =====
    def calculate_actual_pdr(self, source_id):
        if source_id not in self.packet_stats:
            return 95.0
        s = self.packet_stats[source_id]
        if s['sent'] == 0:
            return 0.0
        return max(0, min(100, (s['received']/s['sent'])*100))

    # ===== PDR baru untuk HELLO broadcast: window-based =====
    def calculate_hello_pdr(self, source_id, window_sec=ROUTE_TIMEOUT):
        now = time.time()
        dq = self.hello_rx_times[source_id]
        while dq and (now - dq[0] > window_sec):
            dq.popleft()
        expected = max(1, int(window_sec / HELLO_INTERVAL))  # contoh: 10/2=5
        got = len(dq)
        return max(0.0, min(100.0, (got/expected)*100.0))

    def get_network_metrics(self, target_node_id=None):
        return {
            'rssi': self.get_actual_rssi(),
            'delay': self.calculate_delay(target_node_id),
            'pdr': self.calculate_pdr(target_node_id),
            'timestamp': time.time(),
            'raspberry_id': RASPBERRY_PI_ID,
            'node_id': self.node_id
        }

    # Fallback simulasi (tidak dipakai untuk actual delay di HELLO/DATA)
    def calculate_delay(self, target_node_id=None):
        base = 20
        if target_node_id is not None:
            base += abs(self.node_id - target_node_id) * 15
        d = base + random.uniform(-5, 15)
        return round(max(10, min(200, d)), 1)

    def calculate_pdr(self, target_node_id=None):
        base = 95.0
        if target_node_id is not None:
            base -= abs(self.node_id - target_node_id) * 3
        p = base + random.uniform(-8, 5)
        return round(max(60, min(100, p)), 1)

    def update_metrics(self, source_id, rssi, delay, pdr):
        self.metrics[source_id]['rssi'].append(rssi)
        self.metrics[source_id]['delay'].append(delay)
        self.metrics[source_id]['pdr'].append(pdr)
        self.metrics[source_id]['last_update'] = time.time()
        if source_id not in self.network_topology:
            self.network_topology[source_id] = {'active': True}
        self.network_topology[source_id]['metrics'] = {
            'rssi': rssi, 'delay': delay, 'pdr': pdr}
        self.network_topology[source_id]['last_update'] = time.time()

    def get_avg_metrics(self, node_id):
        if node_id not in self.metrics or not self.metrics[node_id]['rssi']:
            return None
        m = self.metrics[node_id]
        return {'rssi': round(sum(m['rssi'])/len(m['rssi']), 1),
                'delay': round(sum(m['delay'])/len(m['delay']), 1),
                'pdr': round(sum(m['pdr'])/len(m['pdr']), 1),
                'samples': len(m['rssi'])}

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
                ttl
            )
            packet = header + (data.encode('utf-8') if data else b'')
            self.socket.sendto(packet, (BROADCAST_ADDR, UDP_PORT))

            # sent counter (tetap seperti versi Anda)
            dest_id = self.mac_to_node_id(dest_mac)
            if dest_id != -1 and packet_type in [PKT_DATA, PKT_RREQ, PKT_RREP]:
                self.packet_stats[dest_id]['sent'] += 1
        except Exception as e:
            safe_print(f"Error mengirim paket: {e}")

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
        safe_print(
            f"HELLO dikirim dari Raspberry Pi {RASPBERRY_PI_ID}", banner=True)

    def send_hello_periodic(self):
        while True:
            self.send_hello()
            time.sleep(HELLO_INTERVAL)

    def periodic_optimization(self):
        while True:
            time.sleep(OPTIMIZATION_INTERVAL)
            self.optimize_all_routes(save_to_db=False)

    def optimize_all_routes(self, save_to_db=False):
        current = time.time()
        active_nodes = [nid for nid, t in self.hello_times.items()
                        if current - t < ROUTE_TIMEOUT and nid != self.node_id]
        safe_print(
            f"Memulai optimasi periodik untuk {len(active_nodes)} node aktif", banner=True)
        for nid in active_nodes:
            self.find_optimal_route(nid, save_to_db=save_to_db)

    def send_rreq(self, dest_id):
        try:
            data = json.dumps({'source_id': self.node_id, 'dest_id': dest_id,
                               'seq_num': self.get_next_sequence_num(), 'timestamp': time.time()})
            self.send_packet(PKT_RREQ, "BROADCAST", data)
            safe_print(
                f"RREQ dikirim untuk mencari rute ke Node {dest_id}", banner=True)
        except Exception as e:
            safe_print(f"Error mengirim RREQ: {e}")

    def send_rrep(self, dest_id, target_id, seq_num):
        try:
            data = json.dumps({'source_id': self.node_id, 'dest_id': dest_id, 'target_id': target_id,
                               'seq_num': seq_num, 'hop_count': 1, 'timestamp': time.time()})
            dest_mac = self.node_id_to_mac(dest_id)
            if dest_mac:
                self.send_packet(PKT_RREP, dest_mac, data)
                safe_print(
                    f"RREP dikirim ke Node {dest_id} untuk rute ke Node {target_id}", banner=True)
            else:
                safe_print(f"Tidak tahu MAC address untuk Node {dest_id}")
        except Exception as e:
            safe_print(f"Error mengirim RREP: {e}")

    def send_rerr(self, unreachable_node):
        try:
            data = json.dumps({'unreachable_node': unreachable_node,
                               'seq_num': self.get_next_sequence_num(), 'timestamp': time.time()})
            self.send_packet(PKT_RERR, "BROADCAST", data)
            safe_print(
                f"RERR dikirim: Node {unreachable_node} tidak dapat dijangkau", banner=True)
        except Exception as e:
            safe_print(f"Error mengirim RERR: {e}")

    def find_optimal_route(self, destination_id, save_to_db=True):
        now = time.time()
        for nid in list(self.network_topology.keys()):
            self.network_topology[nid]['active'] = now - \
                self.network_topology[nid].get(
                    'last_update', 0) <= ROUTE_TIMEOUT

        optimal = self.pso_ga_optimizer.optimize_route(destination_id)
        if optimal:
            self.routing_table[destination_id] = {
                'next_hop': optimal['next_hop'],
                'hop_count': len(optimal['path']) - 1,
                'seq_num': self.get_next_sequence_num(),
                'last_update': time.time(),
                'path': optimal['path']
            }
            safe_print(
                f"Rute optimal ditemukan: {optimal['path']}", banner=True)

            if save_to_db and destination_id == SINK_NODE_ID and hasattr(self, 'db_handler'):
                avg_rssi, avg_latency, avg_pdr = self.pso_ga_optimizer.calculate_route_metrics(
                    optimal, destination_id)
                best_route_str = ' '.join(map(str, optimal['path']))
                self.db_handler.insert_optimized_route(
                    source_node=self.node_id, destination_node=destination_id, best_route=best_route_str,
                    fitness=self.pso_ga_optimizer.global_best_fitness, avg_rssi=avg_rssi,
                    avg_latency=avg_latency, avg_pdr=avg_pdr, iterations=self.pso_ga_optimizer.iteration_count
                )
            return True
        return False

    def send_data(self, destination_id, message):
        safe_print(f"Mengirim data ke node {destination_id}", banner=True)
        safe_print(
            f"Melakukan optimasi rute ke Node {destination_id} sebelum mengirim data...")
        ok = self.find_optimal_route(destination_id, save_to_db=True)
        if not ok:
            safe_print(
                f"Tidak dapat menemukan rute optimal ke Node {destination_id}")
            return

        route_info = self.routing_table[destination_id]
        if not self.validate_route(route_info, destination_id):
            safe_print(
                f"Rute ke Node {destination_id} tidak valid, mencari rute baru...")
            if not self.find_optimal_route(destination_id, save_to_db=True):
                safe_print(
                    f"Tidak dapat menemukan rute ke Node {destination_id}")
                return

        # ===== END-TO-END: buat packet_id dan catat waktu kirim =====
        pid = random.getrandbits(32)
        if pid == 0:
            pid = 1

        packet_id = str(pid)  # untuk DB/log (varchar)
        route_path = route_info.get('path', [self.node_id, destination_id])

        payload = {
            'packet_id': pid,                 # <-- INT untuk ESP32
            'payload': message,
            'source': self.node_id,
            'destination': destination_id,
            'timestamp': time.time(),         # sent timestamp (detik)
            'raspberry_id': RASPBERRY_PI_ID,
            'type': 'raspberry_pi_data',
            # gunakan key "path" (dipakai ESP32). Simpan juga "route" untuk kompatibilitas.
            'path': route_path,
            'route': route_path
        }
        t0 = payload['timestamp']
        self.e2e_pending[packet_id] = {
            'dest': destination_id,
            't0': t0,
            'route': route_path,
            'hops': max(0, len(route_path) - 1)
        }
        self.e2e_sent_log[destination_id].append((t0, packet_id))

        dest_mac = self.node_id_to_mac(destination_id)
        if dest_mac:
            self.send_packet(PKT_DATA, dest_mac, json.dumps(payload))
            self.message_history.append(
                {'timestamp': time.time(), 'destination': destination_id, 'message': message})
            safe_print(
                f"Data dikirim ke node {destination_id}\n   Pesan: {message}")
        else:
            safe_print(f"Tidak tahu MAC address untuk node {destination_id}")
            safe_print(f"Mengirim RERR untuk Node {destination_id}")
            self.send_rerr(destination_id)

    def validate_route(self, route_info, destination_id):
        if 'path' not in route_info:
            return False
        path = route_info['path']
        if len(path) != len(set(path)):
            return False
        if destination_id not in path:
            return False
        if path[0] != self.node_id:
            return False
        return True

    # ---------- RX ----------
    def receive_packets(self):
        while True:
            try:
                data, addr = self.socket.recvfrom(1024)
                if len(data) < 14:
                    continue
                packet_type = struct.unpack('!B', data[0:1])[0]
                source_mac_bytes = data[1:7]
                dest_mac_bytes = data[7:13]
                ttl = struct.unpack('!B', data[13:14])[0]
                source_mac = self.bytes_to_mac(source_mac_bytes)
                dest_mac = self.bytes_to_mac(dest_mac_bytes)

                if ttl <= 0:
                    continue
                if self.normalize_mac(dest_mac) not in (self.mac_address, "FFFFFFFFFFFF"):
                    continue

                payload = data[14:].decode(
                    'utf-8', errors='ignore') if len(data) > 14 else None
                source_id = self.mac_to_node_id(source_mac)

                # received counter
                if source_id != -1 and packet_type in [PKT_DATA, PKT_HELLO, PKT_RREP]:
                    self.packet_stats[source_id]['received'] += 1

                if packet_type == PKT_HELLO:
                    self.process_hello(source_mac, payload)
                elif packet_type in [PKT_DATA, PKT_DATA_METRIC]:
                    self.process_data(source_mac, dest_mac,
                                      payload, packet_type)
                elif packet_type == PKT_RREQ:
                    self.process_rreq(source_mac, payload)
                elif packet_type == PKT_RREP:
                    self.process_rrep(source_mac, payload)
                elif packet_type == PKT_RERR:
                    self.process_rerr(source_mac, payload)
                elif packet_type == PKT_ACK:
                    self.process_ack(source_mac, dest_mac, payload)
                elif packet_type == PKT_RREQ_METRIC:
                    self.process_rreq_metric(source_mac, payload)

            except socket.timeout:
                continue
            except Exception as e:
                safe_print(f"Error menerima paket: {e}")

    # ===== END-TO-END ACK (PKT_ACK) =====
    def send_ack(self, dest_id, packet_id, sent_ts, route=None, hop_metrics=None):
        """Kirim ACK balik ke source untuk memungkinkan perhitungan end-to-end di source."""
        try:
            payload = {
                'packet_id': packet_id,
                'sent_ts': sent_ts,
                'ack_ts': time.time(),
                # ACK sender (destination dari DATA)
                'source': self.node_id,
                # ACK receiver (source dari DATA)
                'destination': dest_id,
                'route': route or [],
                'hop_metrics': hop_metrics or [],
                'raspberry_id': RASPBERRY_PI_ID,
                'type': 'ack'
            }
            dest_mac = self.node_id_to_mac(dest_id)
            if dest_mac:
                self.send_packet(PKT_ACK, dest_mac, json.dumps(payload))
        except Exception as e:
            safe_print(f"Error mengirim ACK: {e}")

    def _e2e_window_stats(self, dest_id, window_sec=E2E_WINDOW_SEC):
        """Hitung PDR e2e dan statistik delay dalam window terakhir."""
        now = time.time()
        sent_dq = self.e2e_sent_log.get(dest_id, deque())
        ack_dq = self.e2e_ack_log.get(dest_id, deque())

        # prune
        while sent_dq and (now - sent_dq[0][0] > window_sec):
            sent_dq.popleft()
        while ack_dq and (now - ack_dq[0][0] > window_sec):
            ack_dq.popleft()

        sent = len(sent_dq)
        ack = len(ack_dq)
        pdr = (ack / sent * 100.0) if sent > 0 else 0.0

        delays = [x[2] for x in ack_dq if x[2] is not None]
        avg_delay = (sum(delays) / len(delays)) if delays else None
        p95_delay = (float(np.percentile(delays, 95)) if delays else None)

        rssi_mins = [x[3] for x in ack_dq if x[3] is not None]
        rssi_avgs = [x[4] for x in ack_dq if x[4] is not None]
        avg_rssi_min = (sum(rssi_mins) / len(rssi_mins)) if rssi_mins else None
        avg_rssi_avg = (sum(rssi_avgs) / len(rssi_avgs)) if rssi_avgs else None

        return {
            'sent': sent,
            'ack': ack,
            'pdr': max(0.0, min(100.0, pdr)),
            'avg_delay_ms': avg_delay,
            'p95_delay_ms': p95_delay,
            'avg_rssi_min': avg_rssi_min,
            'avg_rssi_avg': avg_rssi_avg
        }

    def process_ack(self, source_mac, dest_mac, payload):
        """Terima ACK dari destination untuk menghitung metrik end-to-end."""
        try:
            if not payload:
                return
            d = json.loads(payload)
            packet_id_raw = d.get('packet_id')
            if packet_id_raw is None:
                return
            # <<< FIX: samakan tipe dengan e2e_pending (string)
            packet_id = str(packet_id_raw)
            if packet_id in self.e2e_seen_acks:
                return
            self.e2e_seen_acks.add(packet_id)
            sent_ts = d.get('sent_ts')
            ack_ts = d.get('ack_ts', time.time())
            route = d.get('route') or []
            hop_metrics = d.get('hop_metrics') or []

            pending = self.e2e_pending.pop(packet_id, None)
            if pending:
                dest_id = pending['dest']
                t0 = pending['t0']
                route = route or pending.get('route', route)
                hops = pending.get('hops', max(0, len(route)-1))
            else:
                dest_id = None
                hops = max(0, len(route)-1)
                t0 = sent_ts if sent_ts else None

            # e2e delay
            if t0 is not None:
                e2e_delay_ms = max(0.0, (time.time() - float(t0)) * 1000.0)
            elif sent_ts is not None:
                e2e_delay_ms = max(
                    0.0, (time.time() - float(sent_ts)) * 1000.0)
            else:
                e2e_delay_ms = None

            # e2e rssi dari hop_metrics (ambil min & avg)
            rssi_vals = []
            for hm in hop_metrics:
                if isinstance(hm, dict) and 'rssi' in hm and hm['rssi'] is not None:
                    try:
                        rssi_vals.append(float(hm['rssi']))
                    except:
                        pass
            e2e_rssi_min = min(rssi_vals) if rssi_vals else None
            e2e_rssi_avg = (sum(rssi_vals)/len(rssi_vals)
                            ) if rssi_vals else None

            # simpan ack log dan hitung window PDR
            if dest_id is not None:
                self.e2e_ack_log[dest_id].append((time.time(), packet_id, e2e_delay_ms, e2e_rssi_min, e2e_rssi_avg,
                                                  ' '.join(
                                                      map(str, route)) if route else None,
                                                  hops))
                win = self._e2e_window_stats(dest_id)
                window_pdr = win['pdr']
            else:
                window_pdr = None

            safe_print("E2E ACK diterima", banner=True)
            safe_print(f"   PacketID: {packet_id}")
            if dest_id is not None:
                safe_print(
                    f"   Dest: {dest_id} | WindowPDR({E2E_WINDOW_SEC}s): {window_pdr:.1f}%")
            if e2e_delay_ms is not None:
                safe_print(f"   E2E Delay: {e2e_delay_ms:.1f} ms")
            if e2e_rssi_min is not None:
                safe_print(
                    f"   E2E RSSI(min/avg): {e2e_rssi_min:.1f} / {e2e_rssi_avg:.1f} dBm")
            if route:
                safe_print(f"   Route: {' -> '.join(map(str, route))}")

            # simpan ke DB jika ada handler
            if dest_id is not None and hasattr(self, 'db_handler'):
                try:
                    self.db_handler.insert_e2e_metric(
                        packet_id=packet_id,
                        source_node=self.node_id,
                        destination_node=dest_id,
                        route=' '.join(map(str, route)) if route else None,
                        hops=hops,
                        e2e_delay_ms=e2e_delay_ms,
                        e2e_rssi_min=e2e_rssi_min,
                        e2e_rssi_avg=e2e_rssi_avg,
                        success=True,
                        window_pdr=window_pdr
                    )
                except Exception as _:
                    pass

        except Exception as e:
            safe_print(f"Error memproses ACK: {e}")

    def process_hello(self, source_mac, payload):
        try:
            if not payload:
                return
            data = json.loads(payload)
            source_id = data.get('node_id', -1)
            ts = data.get('timestamp', time.time())
            sent_ts = data.get('timestamp', ts)

            if source_id != -1:
                self.hello_times[source_id] = time.time()

                rssi = self.get_actual_rssi()
                delay = self.calculate_actual_delay(sent_ts)

                # ===== FIX: PDR untuk HELLO pakai window-based =====
                self.hello_rx_times[source_id].append(time.time())
                pdr = self.calculate_hello_pdr(source_id)

                self.update_metrics(source_id, rssi, delay, pdr)
                self.record_edge_metric(
                    source_id, self.node_id, rssi, delay, pdr)

                hop_metrics = data.get('hop_metrics', [])
                for hm in hop_metrics:
                    u = hm.get('u')
                    v = hm.get('v')
                    if u is not None and v is not None:
                        self.record_edge_metric(
                            int(u), int(v),
                            hm.get('rssi', rssi),
                            hm.get('delay', hm.get('delay_ms', delay)),
                            hm.get('pdr', pdr)
                        )

                # NOTE: HELLO tidak mengirim ACK end-to-end. ACK hanya untuk PKT_DATA.

                raspberry_id = data.get('raspberry_id', 'Unknown')
                if data.get('type') == 'raspberry_pi_hello':
                    safe_print(
                        f"HELLO diterima dari Raspberry Pi {raspberry_id} (Node {source_id})", banner=True)
                else:
                    safe_print(
                        f"HELLO diterima dari Node {source_id}", banner=True)

                safe_print(
                    f"   RSSI Aktual: {rssi} dBm | Delay Aktual: {delay:.1f} ms | PDR (HELLO): {pdr:.1f}%")

        except Exception as e:
            safe_print(f"Error memproses HELLO: {e}")

    def process_data(self, source_mac, dest_mac, payload, packet_type):
        try:
            if not payload:
                return
            data = json.loads(payload)
            if self.normalize_mac(dest_mac) == self.mac_address:
                source_id = data.get('source', -1)
                raspberry_id = data.get('raspberry_id', 'Unknown')
                message = data.get('payload', '')
                ts = data.get('timestamp', time.time())
                sent_ts = data.get('timestamp', ts)

                if source_id != -1:
                    rssi = self.get_actual_rssi()
                    delay = self.calculate_actual_delay(sent_ts)

                    # DATA: pakai PDR sent/received (sesuai versi Anda)
                    pdr = self.calculate_actual_pdr(source_id)

                    self.update_metrics(source_id, rssi, delay, pdr)
                    self.record_edge_metric(
                        source_id, self.node_id, rssi, delay, pdr)

                    hop_metrics = data.get('hop_metrics', [])
                    for hm in hop_metrics:
                        u = hm.get('u')
                        v = hm.get('v')
                        if u is not None and v is not None:
                            self.record_edge_metric(
                                int(u), int(v),
                                hm.get('rssi', rssi),
                                hm.get('delay', hm.get('delay_ms', delay)),
                                hm.get('pdr', pdr)
                            )

                safe_print(
                    f"DATA diterima oleh {'Raspberry Pi ' if raspberry_id != 'Unknown' else 'Node'} {raspberry_id}", banner=True)
                safe_print(f"   Pesan: {message}")
                safe_print(
                    f"   Metrik Aktual: RSSI={rssi}dBm, Delay={delay:.1f}ms, PDR={pdr:.1f}%")

        except Exception as e:
            safe_print(f"Error memproses DATA: {e}")

    def process_rreq(self, source_mac, payload):
        try:
            if not payload:
                return
            d = json.loads(payload)
            source_id = d.get('source_id', -1)
            dest_id = d.get('dest_id', -1)
            seq_num = d.get('seq_num', -1)
            safe_print(
                f"RREQ diterima dari Node {source_id} (via {source_mac})", banner=True)
            safe_print(f"   Mencari rute ke Node {dest_id}")
            safe_print(f"   Sequence Number: {seq_num}")
            if dest_id == self.node_id:
                safe_print(f"   Saya adalah tujuan! Mengirim RREP...")
                self.send_rrep(source_id, dest_id, seq_num)
            else:
                safe_print(f"   Meneruskan RREQ ke node lain...")
        except Exception as e:
            safe_print(f"Error memproses RREQ: {e}")

    def process_rrep(self, source_mac, payload):
        try:
            if not payload:
                return
            d = json.loads(payload)
            source_id = d.get('source_id', -1)
            dest_id = d.get('dest_id', -1)
            target_id = d.get('target_id', -1)
            seq_num = d.get('seq_num', -1)
            hop_count = d.get('hop_count', 0)
            safe_print(
                f"RREP diterima dari Node {source_id} (via {source_mac})", banner=True)
            safe_print(f"   Rute ditemukan ke Node {target_id}")
            safe_print(f"   Sequence Number: {seq_num}")
            safe_print(f"   Hop Count: {hop_count}")
            if target_id not in self.routing_table or self.routing_table[target_id]['hop_count'] > hop_count:
                self.routing_table[target_id] = {
                    'next_hop': source_id, 'hop_count': hop_count, 'seq_num': seq_num,
                    'last_update': time.time(), 'path': [self.node_id, source_id, target_id]
                }
                safe_print(
                    f"   Tabel routing diperbarui untuk Node {target_id}")
        except Exception as e:
            safe_print(f"Error memproses RREP: {e}")

    def process_rerr(self, source_mac, payload):
        try:
            if not payload:
                return
            d = json.loads(payload)
            unreachable_node = d.get('unreachable_node', -1)
            seq_num = d.get('seq_num', -1)
            safe_print(f"RERR diterima dari {source_mac}", banner=True)
            safe_print(f"   Node {unreachable_node} tidak dapat dijangkau")
            safe_print(f"   Sequence Number: {seq_num}")
            self.route_errors[unreachable_node] = {
                'timestamp': time.time(), 'source_mac': source_mac, 'seq_num': seq_num}
            if unreachable_node in self.routing_table:
                del self.routing_table[unreachable_node]
                safe_print(
                    f"   Rute ke Node {unreachable_node} dihapus dari tabel")
            if unreachable_node in self.network_topology:
                self.network_topology[unreachable_node]['active'] = False
                safe_print(
                    f"   Node {unreachable_node} ditandai sebagai tidak aktif")
                safe_print(
                    f"   Mencari rute alternatif ke Node {unreachable_node}...")
                self.find_optimal_route(unreachable_node, save_to_db=False)
        except Exception as e:
            safe_print(f"Error memproses RERR: {e}")

    def process_rreq_metric(self, source_mac, payload):
        try:
            if not payload:
                return
            d = json.loads(payload)
            source_id = d.get('source_id', -1)
            dest_id = d.get('dest_id', -1)
            metrics = d.get('metrics', {})
            safe_print(
                f"RREQ-METRIC diterima dari Node {source_id}", banner=True)
            safe_print(f"   Mencari rute optimal ke Node {dest_id}")
            if metrics:
                safe_print(f"     RSSI: {metrics.get('rssi', 'N/A')} dBm")
                safe_print(f"     Delay: {metrics.get('delay', 'N/A')} ms")
                safe_print(f"     PDR: {metrics.get('pdr', 'N/A')}%")
        except Exception as e:
            safe_print(f"Error memproses RREQ-METRIC: {e}")

    def print_network_status(self):
        safe_print("STATUS JARINGAN - Raspberry Pi " +
                   RASPBERRY_PI_ID, banner=True)
        current = time.time()
        safe_print(f"Node ID: {self.node_id} | MAC: {self.mac_address}")
        safe_print(f"Waktu: {time.strftime('%Y-%m-%d %H:%M:%S')}")
        safe_print(f"\nTabel Routing:")
        safe_print("-"*40)
        for dest, info in self.routing_table.items():
            status = "Aktif" if current - \
                info['last_update'] < ROUTE_TIMEOUT else "Kadaluarsa"
            path_str = " -> ".join(map(str, info.get('path', ['Unknown'])))
            safe_print(f"{status} Node {dest} -> Path: {path_str}")
        safe_print(f"\nDaftar Node Terdeteksi:")
        safe_print("-"*40)
        for nid, last in self.hello_times.items():
            if current - last < ROUTE_TIMEOUT:
                m = self.get_avg_metrics(nid)
                status = "Aktif" if m else "Tidak Aktif"
                safe_print(f"{status} Node {nid}: ")
                if m:
                    safe_print(f"   RSSI: {m['rssi']} dBm")
                    safe_print(f"   Delay: {m['delay']} ms")
                    safe_print(f"   PDR: {m['pdr']}%")
                    safe_print(f"   Sample: {m['samples']}")
                safe_print("")
        safe_print(f"History Pesan: {len(self.message_history)} pesan")
        safe_print(f"\nRERR yang Diterima:")
        safe_print("-"*40)
        for nid, err in self.route_errors.items():
            safe_print(
                f"Node {nid}: {current - err['timestamp']:.1f} detik yang lalu dari {err['source_mac']}")
        safe_print(f"\nStatistik Paket:")
        safe_print("-"*40)
        for nid, st in self.packet_stats.items():
            if st['sent'] > 0 or st['received'] > 0:
                pdr = (st['received']/st['sent']*100) if st['sent'] > 0 else 0
                safe_print(
                    f"Node {nid}: Sent={st['sent']}, Received={st['received']}, PDR={pdr:.1f}%")

        safe_print(f"\nEnd-to-End Metrics (ACK) - Window {E2E_WINDOW_SEC}s:")
        safe_print("-"*40)
        if hasattr(self, 'e2e_sent_log') and self.e2e_sent_log:
            # tampilkan hanya destination yang pernah kita kirimi DATA
            for dest_id in sorted(self.e2e_sent_log.keys()):
                st = self._e2e_window_stats(dest_id)
                ad = st['avg_delay_ms']
                p95 = st['p95_delay_ms']
                rmin = st['avg_rssi_min']
                ravg = st['avg_rssi_avg']
                pdrv = st.get('pdr')
                pdr_str = f"{pdrv:.1f}%" if pdrv is not None else "N/A"
                ad_str = f"{ad:.1f}ms" if ad is not None else "N/A"
                p95_str = f" P95={p95:.1f}ms" if p95 is not None else ""
                rssi_str = (f" | RSSI(min/avg)={rmin:.1f}/{ravg:.1f}dBm"
                            if rmin is not None and ravg is not None else "")
                safe_print(
                    f"Dest {dest_id}: Sent={st['sent']} Ack={st['ack']} PDR={pdr_str} | AvgDelay={ad_str}"
                    + p95_str + rssi_str
                )
        else:
            safe_print("Belum ada data end-to-end (menunggu ACK).")


# ================== MAIN ==================
if __name__ == "__main__":
    safe_print(f"Memulai Raspberry Pi di Node 4", banner=True)
    safe_print(f"ID Unik: {RASPBERRY_PI_ID}")
    node = AODVNode(4)
    time.sleep(3)

    try:
        counter = 0
        while True:
            safe_print(f"SIKLUS KE-{counter}", banner=True)

            msg = f"Pesan dari Raspberry Pi {RASPBERRY_PI_ID} - #{counter}"
            node.send_data(SINK_NODE_ID, msg)

            if counter % 5 == 0:
                rnd = random.choice([0, 1, 2, 3])
                if rnd != node.node_id:
                    safe_print(f"Simulasi route discovery ke Node {rnd}")
                    node.send_rreq(rnd)

            if counter % 7 == 0 and node.routing_table:
                rnd_dest = random.choice(list(node.routing_table.keys()))
                safe_print(f"Simulasi route error ke Node {rnd_dest}")
                node.send_rerr(rnd_dest)
                if rnd_dest in node.routing_table:
                    del node.routing_table[rnd_dest]

            if counter % 3 == 0:
                node.print_network_status()

            counter += 1
            time.sleep(8)
    except KeyboardInterrupt:
        safe_print("\nRaspberry Pi dihentikan")
        node.print_network_status()
        node.db_handler.close_connection()
