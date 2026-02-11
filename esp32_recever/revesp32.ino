/*
 * ESP32 Hybrid Mesh (ESP-NOW) + UDP to Raspberry Pi — FIXED (core 3.0.6)
 * - Arduino-ESP32 core v3.x (tested compile path)
 * - TTL real (drop/decrement)
 * - RREP uses reverse route to requester
 * - Routing seq uses packet seq (not local random)
 * - DATA dedup to avoid loops
 * - E2E ACK -> RPi via UDP (or relay via mesh if Wi-Fi down)
 */

#include <WiFi.h>
#include <WiFiUdp.h>
#include <ArduinoJson.h>
#include <esp_now.h>
#include <esp_wifi.h>
#include <time.h>

// ============================ CONFIG RPI ============================
const char* RPI_SSID      = "rangga_5G";
const char* RPI_PASSWORD  = "mary4m26272822";
const char* RPI_IP        = "192.168.1.100";
const int   RPI_UDP_PORT  = 5000;

// ============================ NTP (optional) ========================
const char* NTP_SERVER_1 = "pool.ntp.org";
const char* NTP_SERVER_2 = "time.google.com";
const long  GMT_OFFSET_SEC = 0;
const int   DST_OFFSET_SEC = 0;
const uint32_t NTP_SYNC_TIMEOUT_MS = 15000;

// ============================ PARAMS ================================
const uint32_t HELLO_INTERVAL_MS = 2000;
const uint32_t ROUTE_TIMEOUT_MS  = 10000;
const uint8_t  MAX_TTL           = 10;

// Packet types in header[0]
#define PKT_HELLO        0
#define PKT_RREQ         1
#define PKT_RREP         2
#define PKT_DATA         3
#define PKT_RERR         4
#define PKT_ACK          7

// Header 14 bytes: [0]=type, [1..6]=srcMAC, [7..12]=dstMAC, [13]=TTL
static const int HDR_LEN = 14;

// Node IDs: 0..3 (ESP32), 4 = RPi (sink)
String MAC_ADDRESSES[] = {
  "78:1C:3C:B9:78:54",  // Node 0
  "6C:C8:40:4D:C9:3C",  // Node 1
  "1C:69:20:B8:D9:60",  // Node 2
  "78:1C:3C:B7:DF:20",  // Node 3
  "D8:3A:DD:CD:F7:5D"   // RPi (reference only)
};

WiFiUDP udp;

String myMac;
int node_id = -1;

bool rpiAvailable = false;
uint32_t local_seq = 0;         // local seq for our own packets
uint32_t packet_counter = 0;    // packet_id generator

// ============================ UTIL =================================
String normalizeMac(String mac) {
  mac.replace(":", "");
  mac.toUpperCase();
  return mac;
}

void macToBytes(String mac, uint8_t* macBytes) {
  String cleanMac = normalizeMac(mac);
  for (int i = 0; i < 6; i++) {
    String byteStr = cleanMac.substring(i * 2, i * 2 + 2);
    macBytes[i] = strtoul(byteStr.c_str(), NULL, 16);
  }
}

String bytesToMac(const uint8_t* macBytes) {
  char macStr[18];
  sprintf(macStr, "%02X:%02X:%02X:%02X:%02X:%02X",
          macBytes[0], macBytes[1], macBytes[2],
          macBytes[3], macBytes[4], macBytes[5]);
  return String(macStr);
}

int macToNodeId(String mac) {
  String nm = normalizeMac(mac);
  for (int i = 0; i < 4; i++) {
    if (nm == normalizeMac(MAC_ADDRESSES[i])) return i;
  }
  return -1;
}

String nodeIdToMac(int id) {
  if (id >= 0 && id < 4) return MAC_ADDRESSES[id];
  return "";
}

uint32_t nextSeq() { return ++local_seq; }

bool syncTimeOnce() {
  configTime(GMT_OFFSET_SEC, DST_OFFSET_SEC, NTP_SERVER_1, NTP_SERVER_2);
  uint32_t start = millis();
  struct tm tm_info;
  while (millis() - start < NTP_SYNC_TIMEOUT_MS) {
    if (getLocalTime(&tm_info, 1000)) return true;
  }
  return false;
}

// ============================ ESP-NOW PEER ==========================
uint8_t BROADCAST_MAC[6] = {0xFF,0xFF,0xFF,0xFF,0xFF,0xFF};

bool ensurePeer(const uint8_t* mac) {
  if (esp_now_is_peer_exist(mac)) return true;

  esp_now_peer_info_t peer{};
  memcpy(peer.peer_addr, mac, 6);
  peer.encrypt = false;
  peer.channel = 0;            // follow current channel
  peer.ifidx   = WIFI_IF_STA;  // core 3.x

  return (esp_now_add_peer(&peer) == ESP_OK);
}

void buildHeader(uint8_t* header, uint8_t type, const String& dest_mac, uint8_t ttl) {
  header[0] = type;
  uint8_t srcBytes[6], dstBytes[6];
  macToBytes(myMac, srcBytes);
  macToBytes(dest_mac, dstBytes);
  memcpy(&header[1], srcBytes, 6);
  memcpy(&header[7], dstBytes, 6);
  header[13] = ttl;
}

bool sendEspNowRaw(const uint8_t* dstMac, const uint8_t* data, size_t len) {
  if (!ensurePeer(dstMac)) return false;
  return (esp_now_send(dstMac, data, len) == ESP_OK);
}

void sendEspNowPacket(uint8_t type, const String& dest_mac, const String& payload, uint8_t ttl = MAX_TTL) {
  size_t payload_len = payload.length();
  size_t total = HDR_LEN + payload_len;

  uint8_t* buf = (uint8_t*)malloc(total);
  if (!buf) return;

  buildHeader(buf, type, dest_mac, ttl);
  if (payload_len) memcpy(buf + HDR_LEN, payload.c_str(), payload_len);

  uint8_t dest[6]; macToBytes(dest_mac, dest);
  sendEspNowRaw(dest, buf, total);
  free(buf);
}

// ============================ UDP (header + payload) ================
void sendUdpHeaderPacket(uint8_t type, const String& dest_mac, const String& payload, uint8_t ttl = MAX_TTL) {
  uint8_t header[HDR_LEN];
  buildHeader(header, type, dest_mac, ttl);

  udp.beginPacket(RPI_IP, RPI_UDP_PORT);
  udp.write(header, HDR_LEN);
  if (payload.length()) udp.write((const uint8_t*)payload.c_str(), payload.length());
  udp.endPacket();
}

void sendUdpToRpi_DataJson(const String& jsonPayload) {
  sendUdpHeaderPacket(PKT_DATA, "FF:FF:FF:FF:FF:FF", jsonPayload, MAX_TTL);
}
void sendUdpToRpi_AckJson(const String& jsonPayload) {
  sendUdpHeaderPacket(PKT_ACK, "FF:FF:FF:FF:FF:FF", jsonPayload, MAX_TTL);
}

// ============================ ROUTING TABLE =========================
struct RouteEntry {
  int next_hop;
  int hop_count;
  uint32_t seq_num;
  uint32_t last_update_ms;
};

RouteEntry rt[4];

void rtInit() {
  for (int i=0;i<4;i++) rt[i] = {-1, 0, 0, 0};
}

void rtCleanup() {
  uint32_t now = millis();
  for (int i=0;i<4;i++) {
    if (rt[i].seq_num != 0 && (now - rt[i].last_update_ms > ROUTE_TIMEOUT_MS)) {
      rt[i] = {-1, 0, 0, 0};
    }
  }
}

bool rtHas(int dest) {
  return (dest>=0 && dest<4 && rt[dest].seq_num != 0 && rt[dest].next_hop >= 0);
}

void rtUpdate(int dest, int nextHop, int hopCount, uint32_t seq) {
  if (dest < 0 || dest >= 4) return;
  uint32_t now = millis();

  bool should = false;
  if (rt[dest].seq_num == 0) should = true;
  else if (seq > rt[dest].seq_num) should = true;
  else if (seq == rt[dest].seq_num && hopCount < rt[dest].hop_count) should = true;

  if (should) {
    rt[dest].next_hop = nextHop;
    rt[dest].hop_count = hopCount;
    rt[dest].seq_num = seq;
    rt[dest].last_update_ms = now;
  }
}

// ============================ REVERSE ROUTE for RREP ================
struct RreqReverse {
  int source_id;
  uint32_t rreq_id;
  int prev_hop;
  uint32_t ts;
};
RreqReverse rrev[30];
int rrev_count = 0;

bool reverseSeen(int source_id, uint32_t rreq_id) {
  uint32_t now = millis();
  for (int i=0;i<rrev_count;i++) {
    if (now - rrev[i].ts > 30000) {
      for (int j=i;j<rrev_count-1;j++) rrev[j]=rrev[j+1];
      rrev_count--; i--;
    }
  }
  for (int i=0;i<rrev_count;i++) {
    if (rrev[i].source_id==source_id && rrev[i].rreq_id==rreq_id) return true;
  }
  return false;
}

void reverseStore(int source_id, uint32_t rreq_id, int prev_hop) {
  if (reverseSeen(source_id, rreq_id)) return;
  if (rrev_count < 30) {
    rrev[rrev_count++] = {source_id, rreq_id, prev_hop, millis()};
  }
}

int reverseLookupPrevHop(int source_id, uint32_t rreq_id) {
  for (int i=0;i<rrev_count;i++) {
    if (rrev[i].source_id==source_id && rrev[i].rreq_id==rreq_id) return rrev[i].prev_hop;
  }
  return -1;
}

// ============================ ACK DEDUP =============================
struct AckSeen {
  uint32_t packet_id;
  int ack_from;
  uint32_t ts;
};
AckSeen ack_seen[40];
int ack_seen_count=0;

bool isAckSeen(uint32_t pid, int ack_from) {
  uint32_t now = millis();
  for (int i=0;i<ack_seen_count;i++) {
    if (now - ack_seen[i].ts > 30000) {
      for (int j=i;j<ack_seen_count-1;j++) ack_seen[j]=ack_seen[j+1];
      ack_seen_count--; i--;
    }
  }
  for (int i=0;i<ack_seen_count;i++) {
    if (ack_seen[i].packet_id==pid && ack_seen[i].ack_from==ack_from) return true;
  }
  if (ack_seen_count < 40) ack_seen[ack_seen_count++] = {pid, ack_from, now};
  return false;
}

// ============================ DATA DEDUP ============================
struct DataSeen {
  uint32_t packet_id;
  int source;
  uint32_t ts;
};
DataSeen data_seen[60];
int data_seen_count=0;

bool isDataSeen(uint32_t pid, int src) {
  uint32_t now = millis();
  for (int i=0;i<data_seen_count;i++) {
    if (now - data_seen[i].ts > 30000) {
      for (int j=i;j<data_seen_count-1;j++) data_seen[j]=data_seen[j+1];
      data_seen_count--; i--;
    }
  }
  for (int i=0;i<data_seen_count;i++) {
    if (data_seen[i].packet_id==pid && data_seen[i].source==src) return true;
  }
  if (data_seen_count < 60) data_seen[data_seen_count++] = {pid, src, now};
  return false;
}

// ============================ HOP METRICS ===========================
static inline void appendHop(DynamicJsonDocument& doc, int u, int v, int8_t rssi, uint32_t now_ms) {
  uint32_t prev_ms = doc["timestamp_ms"] | now_ms;
  float delay_local = (now_ms >= prev_ms) ? (float)(now_ms - prev_ms) : 0.0f;

  JsonArray hops = doc["hop_metrics"].isNull() ? doc.createNestedArray("hop_metrics") : doc["hop_metrics"].as<JsonArray>();
  JsonObject hm = hops.createNestedObject();
  hm["u"] = u; hm["v"] = v; hm["rssi"] = (int)rssi; hm["delay_ms"] = delay_local;

  JsonArray path = doc["path"].isNull() ? doc.createNestedArray("path") : doc["path"].as<JsonArray>();
  path.add(v);

  doc["prev_hop"] = v;
  doc["timestamp_ms"] = now_ms;
}

// ============================ SENDERS ===============================
void sendHello() {
  DynamicJsonDocument doc(320);
  doc["type"] = "esp32_hello";
  doc["node_id"] = node_id;
  doc["seq_num"] = nextSeq();
  doc["timestamp"] = (long)time(NULL);
  doc["timestamp_ms"] = (uint32_t)millis();
  doc["mac_address"] = myMac;
  JsonArray path = doc.createNestedArray("path"); path.add(node_id);
  doc["prev_hop"] = node_id;
  doc.createNestedArray("hop_metrics");

  String out; serializeJson(doc, out);
  sendEspNowPacket(PKT_HELLO, "FF:FF:FF:FF:FF:FF", out, MAX_TTL);

  if (rpiAvailable) {
    sendUdpHeaderPacket(PKT_HELLO, "FF:FF:FF:FF:FF:FF", out, MAX_TTL);
  }
}

void sendRreq(int dest_id) {
  DynamicJsonDocument doc(256);
  doc["type"] = "aodv_rreq";
  doc["source_id"] = node_id;
  doc["dest_id"]   = dest_id;
  uint32_t rreq_id = nextSeq();
  doc["rreq_id"]   = rreq_id;
  doc["seq_num"]   = rreq_id;
  doc["timestamp"] = (long)time(NULL);
  doc["timestamp_ms"] = (uint32_t)millis();

  JsonArray path = doc.createNestedArray("path"); path.add(node_id);
  doc["prev_hop"] = node_id;
  doc.createNestedArray("hop_metrics");

  String out; serializeJson(doc, out);
  sendEspNowPacket(PKT_RREQ, "FF:FF:FF:FF:FF:FF", out, MAX_TTL);
}

void sendRrepBackToRequester(int requester_id, uint32_t rreq_id, int dest_id, uint32_t dest_seq, int hop_count_to_dest) {
  int prevHop = reverseLookupPrevHop(requester_id, rreq_id);
  if (prevHop < 0) return;

  DynamicJsonDocument doc(256);
  doc["type"] = "aodv_rrep";
  doc["requester_id"] = requester_id;
  doc["dest_id"] = dest_id;
  doc["rreq_id"] = rreq_id;
  doc["dest_seq"] = dest_seq;
  doc["hop_count"] = hop_count_to_dest;
  doc["timestamp"] = (long)time(NULL);
  doc["timestamp_ms"] = (uint32_t)millis();

  JsonArray path = doc.createNestedArray("path"); path.add(node_id);
  doc["prev_hop"] = node_id;
  doc.createNestedArray("hop_metrics");

  String out; serializeJson(doc, out);

  String nhMac = nodeIdToMac(prevHop);
  if (nhMac.length()) sendEspNowPacket(PKT_RREP, nhMac, out, MAX_TTL);
}

void sendRerr(int unreachable_node) {
  DynamicJsonDocument doc(160);
  doc["type"] = "aodv_rerr";
  doc["unreachable_node"] = unreachable_node;
  doc["seq_num"] = nextSeq();
  doc["timestamp"] = (long)time(NULL);
  doc["timestamp_ms"] = (uint32_t)millis();

  JsonArray path = doc.createNestedArray("path"); path.add(node_id);
  doc["prev_hop"] = node_id;
  doc.createNestedArray("hop_metrics");

  String out; serializeJson(doc, out);
  sendEspNowPacket(PKT_RERR, "FF:FF:FF:FF:FF:FF", out, MAX_TTL);
}

void sendAckE2E(uint32_t packet_id, int orig_source, int orig_destination, long sent_ts_sec) {
  DynamicJsonDocument ack(256);
  ack["type"] = "esp32_ack";
  ack["packet_id"] = packet_id;
  ack["ack_from"] = node_id;
  ack["destination"] = 4;
  ack["orig_source"] = orig_source;
  ack["orig_destination"] = orig_destination;
  ack["sent_ts"] = (long)sent_ts_sec;
  ack["ack_ts"] = (long)time(NULL);
  ack["ack_ts_ms"] = (uint32_t)millis();

  String out; serializeJson(ack, out);

  if (rpiAvailable) sendUdpToRpi_AckJson(out);
  else sendEspNowPacket(PKT_ACK, "FF:FF:FF:FF:FF:FF", out, MAX_TTL);
}

void sendData(int dest_id, const String& message) {
  DynamicJsonDocument doc(512);
  doc["type"] = "esp32_data";
  doc["source"] = node_id;
  doc["destination"] = dest_id;
  doc["timestamp"] = (long)time(NULL);
  doc["timestamp_ms"] = (uint32_t)millis();
  doc["packet_id"] = (++packet_counter);
  doc["payload"] = message;

  JsonArray path = doc.createNestedArray("path"); path.add(node_id);
  doc["prev_hop"] = node_id;
  doc.createNestedArray("hop_metrics");

  String out; serializeJson(doc, out);

  if (dest_id == 4) {
    if (rpiAvailable) sendUdpToRpi_DataJson(out);
    else sendEspNowPacket(PKT_DATA, "FF:FF:FF:FF:FF:FF", out, MAX_TTL);
    return;
  }

  if (!rtHas(dest_id)) {
    sendRreq(dest_id);
    return;
  }

  String nhMac = nodeIdToMac(rt[dest_id].next_hop);
  if (nhMac.length()) sendEspNowPacket(PKT_DATA, nhMac, out, MAX_TTL);
}

// ============================ PROCESSORS ============================
void processHello(const String& srcMac, const String& payload) {
  DynamicJsonDocument doc(384);
  if (deserializeJson(doc, payload)) return;

  int sid = doc["node_id"] | -1;
  uint32_t seq = doc["seq_num"] | 0;

  if (sid >= 0 && sid < 4) {
    rtUpdate(sid, sid, 1, seq);
  }
}

void processRreq(const String& payload, int prev_hop_id, uint8_t ttl_left) {
  DynamicJsonDocument doc(512);
  if (deserializeJson(doc, payload)) return;

  int source_id = doc["source_id"] | -1;
  int dest_id   = doc["dest_id"] | -1;
  uint32_t rreq_id = doc["rreq_id"] | (uint32_t)(doc["seq_num"] | 0);

  if (source_id < 0 || dest_id < 0) return;
  if (reverseSeen(source_id, rreq_id)) return;

  if (prev_hop_id >= 0) reverseStore(source_id, rreq_id, prev_hop_id);
  if (prev_hop_id >= 0) rtUpdate(source_id, prev_hop_id, 1, rreq_id);

  if (dest_id == node_id) {
    sendRrepBackToRequester(source_id, rreq_id, dest_id, rreq_id, 0);
    return;
  }

  if (rtHas(dest_id)) {
    int hop_to_dest = rt[dest_id].hop_count;
    sendRrepBackToRequester(source_id, rreq_id, dest_id, rt[dest_id].seq_num, hop_to_dest);
    return;
  }

  if (ttl_left == 0) return;
  sendEspNowPacket(PKT_RREQ, "FF:FF:FF:FF:FF:FF", payload, ttl_left - 1);
}

void processRrep(const String& payload, int prev_hop_id, uint8_t ttl_left) {
  DynamicJsonDocument doc(512);
  if (deserializeJson(doc, payload)) return;

  int requester_id = doc["requester_id"] | -1;
  int dest_id      = doc["dest_id"] | -1;
  uint32_t rreq_id  = doc["rreq_id"] | 0;
  uint32_t dest_seq = doc["dest_seq"] | 0;
  int hop_count     = doc["hop_count"] | 0;

  if (requester_id < 0 || dest_id < 0 || rreq_id == 0) return;

  if (prev_hop_id >= 0) {
    rtUpdate(dest_id, prev_hop_id, hop_count + 1, dest_seq);
  }

  if (requester_id == node_id) return;

  int backPrev = reverseLookupPrevHop(requester_id, rreq_id);
  if (backPrev < 0) return;

  if (ttl_left == 0) return;

  String nhMac = nodeIdToMac(backPrev);
  if (nhMac.length()) sendEspNowPacket(PKT_RREP, nhMac, payload, ttl_left - 1);
}

void processRerr(const String& payload) {
  DynamicJsonDocument doc(256);
  if (deserializeJson(doc, payload)) return;

  int unreach = doc["unreachable_node"] | -1;
  if (unreach < 0 || unreach >= 4) return;

  rt[unreach] = {-1,0,0,0};
}

void processAck(const String& payload, uint8_t ttl_left) {
  DynamicJsonDocument doc(384);
  if (deserializeJson(doc, payload)) return;

  uint32_t pid = doc["packet_id"] | 0;
  int ack_from = doc["ack_from"] | -1;
  int dest = doc["destination"] | -1;

  if (pid == 0 || ack_from < 0) return;
  if (isAckSeen(pid, ack_from)) return;

  if (dest == 4 && rpiAvailable) {
    String out; serializeJson(doc, out);
    sendUdpToRpi_AckJson(out);
    return;
  }

  if (ttl_left == 0) return;
  sendEspNowPacket(PKT_ACK, "FF:FF:FF:FF:FF:FF", payload, ttl_left - 1);
}

void processData(const String& payload, int prev_hop_id, int8_t rx_rssi, uint8_t ttl_left) {
  DynamicJsonDocument doc(1024);
  if (deserializeJson(doc, payload)) return;

  int dest_id = doc["destination"] | -1;
  int source_id = doc["source"] | -1;
  uint32_t pid = doc["packet_id"] | 0;

  if (source_id < 0 || dest_id < 0) return;

  if (pid != 0 && isDataSeen(pid, source_id)) return;

  uint32_t now_ms = millis();
  if (prev_hop_id >= 0) {
    if (doc["path"].isNull()) { JsonArray p = doc.createNestedArray("path"); p.add(source_id); }
    if (doc["hop_metrics"].isNull()) { doc.createNestedArray("hop_metrics"); }
    if (doc["timestamp_ms"].isNull()) { doc["timestamp_ms"] = now_ms; }
    appendHop(doc, prev_hop_id, node_id, rx_rssi, now_ms);
  }

  if (dest_id == node_id) {
    if (pid != 0) {
      long sent_ts = doc["timestamp"] | (long)time(NULL);
      sendAckE2E(pid, source_id, dest_id, sent_ts);
    }
    return;
  }

  if (dest_id == 4) {
    doc["via"] = node_id;
    doc["timestamp"] = (long)time(NULL);
    String out; serializeJson(doc, out);

    if (rpiAvailable) {
      sendUdpToRpi_DataJson(out);
    } else {
      if (ttl_left == 0) return;
      sendEspNowPacket(PKT_DATA, "FF:FF:FF:FF:FF:FF", out, ttl_left - 1);
    }
    return;
  }

  if (!rtHas(dest_id)) {
    sendRreq(dest_id);
    return;
  }

  String nhMac = nodeIdToMac(rt[dest_id].next_hop);
  if (!nhMac.length()) return;

  if (ttl_left == 0) return;

  String out; serializeJson(doc, out);
  sendEspNowPacket(PKT_DATA, nhMac, out, ttl_left - 1);
}

// ============================ RECV CALLBACK =========================
void onEspNowRecv(const esp_now_recv_info_t* info, const uint8_t* incomingData, int len) {
  if (!info || !incomingData || len < HDR_LEN) return;

  uint8_t type = incomingData[0];
  String srcMac = bytesToMac(&incomingData[1]);
  uint8_t ttl = incomingData[13];

  String payload;
  if (len > HDR_LEN) {
    payload.reserve(len - HDR_LEN);
    for (int i=HDR_LEN;i<len;i++) payload += (char)incomingData[i];
  }

  int prev_hop_id = macToNodeId(srcMac);

  int8_t rx_rssi = 0;
#if defined(ESP_IDF_VERSION_MAJOR) && (ESP_IDF_VERSION_MAJOR >= 5)
  // Pada core 3.x (IDF 5.x), rx_ctrl biasanya pointer
  if (info && info->rx_ctrl) rx_rssi = info->rx_ctrl->rssi;
#endif

  if (ttl == 0 && type != PKT_HELLO) return;

  switch (type) {
    case PKT_HELLO: processHello(srcMac, payload); break;
    case PKT_RREQ:  processRreq(payload, prev_hop_id, ttl); break;
    case PKT_RREP:  processRrep(payload, prev_hop_id, ttl); break;
    case PKT_DATA:  processData(payload, prev_hop_id, rx_rssi, ttl); break;
    case PKT_RERR:  processRerr(payload); break;
    case PKT_ACK:   processAck(payload, ttl); break;
    default: break;
  }
}

// ============================ UDP POLL ==============================
void pollUdpFromRpi() {
  int psize = udp.parsePacket();
  if (psize <= 0) return;

  static uint8_t buf[1400];
  int n = udp.read(buf, sizeof(buf));
  if (n < HDR_LEN) return;

  uint8_t type = buf[0];
  uint8_t ttl = buf[13];

  String payload;
  if (n > HDR_LEN) {
    payload.reserve(n - HDR_LEN);
    for (int i=HDR_LEN;i<n;i++) payload += (char)buf[i];
  }

  int prev_hop_id = -1;
  int8_t rx_rssi = 0;

  if (ttl == 0 && type != PKT_HELLO) return;

  switch (type) {
    case PKT_DATA: processData(payload, prev_hop_id, rx_rssi, ttl); break;
    case PKT_ACK:  processAck(payload, ttl); break;
    default: break;
  }
}

// ============================ WIFI / RPI AVAIL ======================
void wifiTryConnect() {
  WiFi.begin(RPI_SSID, RPI_PASSWORD);
  uint32_t t0 = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - t0 < 10000) {
    delay(300);
  }
  if (WiFi.status() == WL_CONNECTED) {
    rpiAvailable = true;
    udp.begin(RPI_UDP_PORT);
    syncTimeOnce();
  } else {
    rpiAvailable = false;
  }
}

// ============================ SETUP / LOOP ==========================
uint32_t lastHello = 0;
uint32_t lastHouse = 0;
uint32_t lastRpiChk = 0;
uint32_t lastWiFiTry = 0;

void setup() {
  Serial.begin(115200);
  delay(200);

  // Must be STA for ESP-NOW + esp_wifi_get_mac
  WiFi.mode(WIFI_STA);
  delay(50);

  // === FIX CORE 3.0.6: read MAC via esp_wifi_get_mac (NOT esp_read_mac) ===
  uint8_t mac[6];
  esp_err_t err = esp_wifi_get_mac(WIFI_IF_STA, mac);
  if (err != ESP_OK) {
    Serial.println("FATAL: esp_wifi_get_mac gagal. Cek WiFi.mode(WIFI_STA) + core.");
    while(true) delay(1000);
  }
  myMac = bytesToMac(mac);

  // map node_id
  node_id = -1;
  for (int i = 0; i < 4; i++) {
    if (normalizeMac(myMac) == normalizeMac(MAC_ADDRESSES[i])) {
      node_id = i;
      break;
    }
  }
  if (node_id < 0) {
    Serial.println("FATAL: MAC tidak terdaftar di MAC_ADDRESSES[0..3]. Perbaiki mapping.");
    Serial.print("MAC device: "); Serial.println(myMac);
    while(true) delay(1000);
  }

  // ESP-NOW init
  if (esp_now_init() != ESP_OK) {
    Serial.println("FATAL: ESP-NOW init gagal.");
    while(true) delay(1000);
  }
  esp_now_register_recv_cb(onEspNowRecv);
  ensurePeer(BROADCAST_MAC);

  rtInit();

  // Try connect to RPi AP (optional)
  wifiTryConnect();

  Serial.println("=== START ===");
  Serial.print("Node ID: "); Serial.println(node_id);
  Serial.print("MAC: "); Serial.println(myMac);
  Serial.print("RPi available: "); Serial.println(rpiAvailable ? "YES" : "NO");
}

void loop() {
  uint32_t now = millis();

  if (now - lastHello >= HELLO_INTERVAL_MS) {
    sendHello();
    lastHello = now;
  }

  if (now - lastHouse >= 1000) {
    rtCleanup();
    lastHouse = now;
  }

  // Periodic check Wi-Fi -> RPi
  if (now - lastRpiChk >= 3000) {
    bool connected = (WiFi.status() == WL_CONNECTED);
    if (connected && !rpiAvailable) {
      rpiAvailable = true;
      udp.begin(RPI_UDP_PORT);
    } else if (!connected && rpiAvailable) {
      rpiAvailable = false;
    }
    lastRpiChk = now;
  }

  if (!rpiAvailable && (now - lastWiFiTry >= 10000)) {
    WiFi.disconnect();
    wifiTryConnect();
    lastWiFiTry = now;
  }

  if (rpiAvailable) pollUdpFromRpi();

  delay(5);
}
