#include <Arduino.h>
#include <WiFi.h>
#include <WebServer.h>
#include <DNSServer.h>
#include <Preferences.h>
#include <WebSocketsClient.h>
#include <driver/i2s.h>
#include "config.h"

// ==================================================
// WEBSOCKET
// ==================================================
WebSocketsClient webSocket;

// ==================================================
// MICROPHONE
// ==================================================
int32_t rawSamples[MIC_CHUNK_SAMPLES];
int16_t pcmSamples[MIC_CHUNK_SAMPLES];

float dcOffset = 0.0f;

// ==================================================
// SPEAKER
// ==================================================
bool speakerReady = false;
bool isPlaying = false;
unsigned long lastAudioRxTime = 0;

// ==================================================
// STATE
// ==================================================
bool wsConnected = false;
unsigned long lastMicPrint = 0;
unsigned long lastWSAttempt = 0;

// Wi-Fi setup portal (kept separate from the audio pipeline)
WebServer portalServer(80);
DNSServer dnsServer;
Preferences wifiPrefs;
const char* SETUP_AP_NAME = "VoiceAssistant-Setup";
const char* SETUP_AP_PASSWORD = "voice123"; // 8+ characters; change if desired
const byte DNS_PORT = 53;
bool backendStarted = false;
bool wifiConnectPending = false;
unsigned long wifiAttemptStarted = 0;
unsigned long lastReconnectAttempt = 0;
String pendingSSID;
String pendingPassword;
String savedSSID;
String savedPassword;


// ==================================================
// WEBSOCKET EVENT
// ==================================================
void webSocketEvent(
  WStype_t type,
  uint8_t * payload,
  size_t length
) {

  switch (type) {

    case WStype_DISCONNECTED:
      wsConnected = false;
      Serial.println("[WS] Disconnected");
      break;

    case WStype_CONNECTED:
      wsConnected = true;

      Serial.println("[WS] ========================================");
      Serial.println("[WS] CONNECTED TO BACKEND");
      Serial.println("[WS] ========================================");

      // Tell backend that ESP32 is ready
      webSocket.sendTXT("{\"type\":\"session_start\",\"device_id\":\"esp32\"}");
      break;

    case WStype_TEXT: {
      char textMsg[128];
      size_t copyLen = length < 127 ? length : 127;
      memcpy(textMsg, payload, copyLen);
      textMsg[copyLen] = '\0';

      Serial.print("[WS] RX: ");
      Serial.println(textMsg);

      // Check for playback control messages (matches with or without spaces)
      if (strstr(textMsg, "playback_start") != NULL) {
        isPlaying = true;
        lastAudioRxTime = millis();
        Serial.println("[SPK] Status: Speaking (Mic muted)");
      } else if (strstr(textMsg, "audio_done") != NULL) {
        isPlaying = false;
        lastAudioRxTime = 0;
        Serial.println("[SPK] Status: Done speaking (Mic listening...)");
      } else if (strstr(textMsg, "playback_stopped") != NULL) {
        isPlaying = false;
        i2s_zero_dma_buffer(SPK_I2S_PORT);
        lastAudioRxTime = 0;
        Serial.println("[SPK] Status: Playback halted by interrupt (Mic listening...)");
      }
      break;
    }

    case WStype_BIN:
    case WStype_FRAGMENT_BIN_START:
    case WStype_FRAGMENT:
    case WStype_FRAGMENT_FIN:
      if (!isPlaying) {
        Serial.printf("[SPK] Audio playback started (%u bytes)\n", (unsigned int)length);
      }
      isPlaying = true;
      lastAudioRxTime = millis();

      if (speakerReady && length > 0) {
        size_t bytesWritten = 0;
        i2s_write(
          SPK_I2S_PORT,
          payload,
          length,
          &bytesWritten,
          portMAX_DELAY
        );
      }
      break;

    case WStype_ERROR:
      Serial.println("[WS] ERROR");
      break;

    case WStype_PING:
      Serial.println("[WS] PING");
      break;

    case WStype_PONG:
      Serial.println("[WS] PONG");
      break;

    default:
      Serial.printf("[WS] Event type: %d, length: %u\n", (int)type, (unsigned int)length);
      break;
  }
}

// ==================================================
// WIFI SETUP PORTAL + API
// ==================================================
const char WIFI_PORTAL_HTML[] PROGMEM = R"HTML(
<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Voice Assistant Wi-Fi Setup</title><style>
*{box-sizing:border-box}body{margin:0;background:#f3f6fb;color:#182230;font-family:Arial,sans-serif}.wrap{max-width:560px;margin:28px auto;padding:16px}.card{background:white;border:1px solid #e0e7ef;border-radius:18px;padding:22px;box-shadow:0 8px 30px #1020400d}h1{font-size:24px;margin:0 0 8px}p{color:#617083;line-height:1.5}.status{padding:12px;border-radius:10px;background:#eef5ff;margin:16px 0;font-size:14px}.btn{border:0;border-radius:10px;padding:12px 15px;font-weight:700;cursor:pointer;background:#1e88e5;color:white}.btn.secondary{background:#e8eef5;color:#172333}.row{display:flex;gap:8px;align-items:center}.row .btn{white-space:nowrap}.networks{display:grid;gap:8px;margin:14px 0}.network{border:1px solid #dbe3ed;border-radius:10px;padding:13px;display:flex;justify-content:space-between;align-items:center;cursor:pointer}.network.selected{border-color:#1e88e5;background:#eef6ff}label{display:block;font-weight:700;font-size:14px;margin:16px 0 7px}input{width:100%;padding:13px;border:1px solid #cbd5e1;border-radius:10px;font-size:16px}small{color:#68778a}.full{width:100%;margin-top:14px}.msg{margin-top:14px;font-size:14px;white-space:pre-wrap}.muted{font-size:13px}</style></head><body><div class="wrap"><div class="card"><h1>Voice Assistant</h1><p>Connect your ESP32 to your home or phone's 2.4 GHz Wi-Fi. Your credentials are saved on the device.</p><div class="status" id="status">Checking device status…</div><div class="row"><button class="btn" onclick="scan()">Scan Wi-Fi networks</button><button class="btn secondary" onclick="status()">Refresh</button></div><div id="networks" class="networks"></div><label for="ssid">Wi-Fi name (SSID)</label><input id="ssid" autocomplete="off" placeholder="Select a network or type its name"><label for="password">Wi-Fi password</label><input id="password" type="password" autocomplete="current-password" placeholder="Enter Wi-Fi password"><small>For most ESP32 boards, use a 2.4 GHz network. Password is stored in ESP32 NVS.</small><button class="btn full" onclick="connect()">Save &amp; Connect</button><div id="msg" class="msg"></div><p class="muted">Setup hotspot: <b>VoiceAssistant-Setup</b> · Password: <b>voice123</b> · Open <b>http://192.168.4.1</b></p></div></div>
<script>
let selected='';const el=id=>document.getElementById(id);async function status(){try{let r=await fetch('/api/status');let d=await r.json();el('status').textContent=d.connected?('Connected to '+d.ssid+' · IP '+d.ip+' · Signal '+d.rssi+' dBm'):('Not connected to Wi-Fi yet. Setup hotspot is active.');if(d.ssid&&!el('ssid').value)el('ssid').value=d.ssid;}catch(e){el('status').textContent='Cannot reach ESP32. Connect to VoiceAssistant-Setup and open http://192.168.4.1';}}
async function scan(){el('networks').innerHTML='<small>Scanning nearby networks…</small>';try{let r=await fetch('/api/networks');let d=await r.json();el('networks').innerHTML='';if(!d.networks||!d.networks.length){el('networks').textContent='No networks found. Try scanning again.';return;}d.networks.forEach(n=>{let b=document.createElement('div');b.className='network'+(selected===n.ssid?' selected':'');let name=document.createElement('span');name.textContent=n.ssid||'(hidden network)';let detail=document.createElement('small');detail.textContent=n.rssi+' dBm · '+n.auth;b.append(name,detail);b.onclick=()=>{selected=n.ssid;el('ssid').value=n.ssid;document.querySelectorAll('.network').forEach(x=>x.classList.remove('selected'));b.classList.add('selected');};el('networks').appendChild(b);});}catch(e){el('networks').textContent='Wi-Fi scan failed. Refresh and try again.';}}
async function connect(){let ssid=el('ssid').value.trim(),password=el('password').value;if(!ssid){el('msg').textContent='Please select or enter a Wi-Fi name.';return;}el('msg').textContent='Saving credentials and trying to connect…';try{let body=new URLSearchParams({ssid,password});let r=await fetch('/api/connect',{method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded'},body});let d=await r.json();el('msg').textContent=d.message||'Connection attempt started.';setTimeout(status,2500);setTimeout(status,7000);}catch(e){el('msg').textContent='Request failed. Stay connected to the setup hotspot and retry.';}}
status();scan();setInterval(status,5000);
</script></body></html>
)HTML";

String jsonEscape(const String& input) {
  String out; out.reserve(input.length()+8);
  for (size_t i=0;i<input.length();i++) { char c=input[i]; if(c=='"'||c=='\\'){out+='\\';out+=c;} else if(c=='\n') out+="\\n"; else if(c=='\r') out+="\\r"; else if((uint8_t)c>=32) out+=c; }
  return out;
}

void beginStationConnection(const String& ssid, const String& password) {
  pendingSSID = ssid; pendingPassword = password;
  WiFi.disconnect(false, false);
  WiFi.begin(pendingSSID.c_str(), pendingPassword.c_str());
  wifiConnectPending = true;
  wifiAttemptStarted = millis();
  Serial.printf("[WIFI] Attempting connection to: %s\n", pendingSSID.c_str());
}

void handlePortalRoot() { portalServer.send_P(200, "text/html", WIFI_PORTAL_HTML); }

void handleWiFiStatus() {
  bool connected = WiFi.status() == WL_CONNECTED;
  String ssid = connected ? WiFi.SSID() : savedSSID;
  String body = "{\"connected\":" + String(connected ? "true" : "false") +
    ",\"ssid\":\"" + jsonEscape(ssid) + "\",\"ip\":\"" +
    (connected ? WiFi.localIP().toString() : String("192.168.4.1")) +
    "\",\"rssi\":" + String(connected ? WiFi.RSSI() : 0) + "}";
  portalServer.send(200, "application/json", body);
}

void handleWiFiScan() {
  int count = WiFi.scanNetworks(false, true);
  String body = "{\"networks\":[";
  bool first = true;
  for (int i=0; i<count; i++) {
    String name = WiFi.SSID(i);
    if (name.length() == 0) continue;
    if (!first) body += ",";
    first = false;
    wifi_auth_mode_t auth = WiFi.encryptionType(i);
    String authName = auth == WIFI_AUTH_OPEN ? "Open" : "Secured";
    body += "{\"ssid\":\"" + jsonEscape(name) + "\",\"rssi\":" + String(WiFi.RSSI(i)) + ",\"auth\":\"" + authName + "\"}";
  }
  body += "]}";
  WiFi.scanDelete();
  portalServer.send(200, "application/json", body);
}

void handleWiFiConnect() {
  String ssid = portalServer.arg("ssid");
  String password = portalServer.arg("password");
  ssid.trim();
  if (ssid.length() == 0) { portalServer.send(400, "application/json", "{\"ok\":false,\"message\":\"Wi-Fi name is required.\"}"); return; }
  wifiPrefs.begin("wifi-config", false);
  wifiPrefs.putString("ssid", ssid);
  wifiPrefs.putString("password", password);
  wifiPrefs.end();
  savedSSID = ssid; savedPassword = password;
  beginStationConnection(ssid, password);
  portalServer.send(200, "application/json", "{\"ok\":true,\"message\":\"Credentials saved. Connecting now; keep this setup page open for a few seconds.\"}");
}

void startWiFiPortal() {
  wifiPrefs.begin("wifi-config", true);
  savedSSID = wifiPrefs.getString("ssid", "");
  savedPassword = wifiPrefs.getString("password", "");
  wifiPrefs.end();
  if (savedSSID.length() == 0) { savedSSID = WIFI_SSID; savedPassword = WIFI_PASSWORD; }

  WiFi.mode(WIFI_AP_STA);
  WiFi.setAutoReconnect(true);
  bool apStarted = WiFi.softAP(SETUP_AP_NAME, SETUP_AP_PASSWORD);
  Serial.printf("[PORTAL] Setup hotspot %s\n", apStarted ? "started" : "FAILED");
  Serial.print("[PORTAL] Connect phone to: "); Serial.println(SETUP_AP_NAME);
  Serial.println("[PORTAL] Hotspot password: voice123");
  Serial.println("[PORTAL] Open http://192.168.4.1");
  dnsServer.start(DNS_PORT, "*", WiFi.softAPIP());

  portalServer.on("/", HTTP_GET, handlePortalRoot);
  portalServer.on("/generate_204", HTTP_GET, handlePortalRoot);
  portalServer.on("/hotspot-detect.html", HTTP_GET, handlePortalRoot);
  portalServer.on("/connecttest.txt", HTTP_GET, handlePortalRoot);
  portalServer.on("/api/status", HTTP_GET, handleWiFiStatus);
  portalServer.on("/api/networks", HTTP_GET, handleWiFiScan);
  portalServer.on("/api/connect", HTTP_POST, handleWiFiConnect);
  portalServer.onNotFound(handlePortalRoot);
  portalServer.begin();
  Serial.println("[PORTAL] Web setup page ready at http://192.168.4.1");

  if (savedSSID.length() > 0) beginStationConnection(savedSSID, savedPassword);
}

void connectBackend(); // defined in the existing WebSocket section below

void serviceWiFi() {
  dnsServer.processNextRequest();
  portalServer.handleClient();

  if (wifiConnectPending) {
    if (WiFi.status() == WL_CONNECTED) {
      wifiConnectPending = false;
      Serial.println("[WIFI] CONNECTED");
      Serial.print("[WIFI] SSID: "); Serial.println(WiFi.SSID());
      Serial.print("[WIFI] ESP32 IP: "); Serial.println(WiFi.localIP());
      Serial.print("[WIFI] RSSI: "); Serial.println(WiFi.RSSI());
      if (!backendStarted) { connectBackend(); backendStarted = true; }
    } else if (millis() - wifiAttemptStarted > 20000) {
      wifiConnectPending = false;
      Serial.println("[WIFI] Connection timed out. Use setup page to select another network.");
    }
  }

  if (WiFi.status() != WL_CONNECTED && !wifiConnectPending && savedSSID.length() > 0 && millis() - lastReconnectAttempt > 15000) {
    lastReconnectAttempt = millis();
    beginStationConnection(savedSSID, savedPassword);
  }
}

// ==================================================
// MICROPHONE I2S
// ==================================================
void initMicI2S() {

  Serial.println("[MIC] Initializing I2S...");

  i2s_config_t micConfig = {

    .mode = (i2s_mode_t)(
      I2S_MODE_MASTER |
      I2S_MODE_RX
    ),

    .sample_rate = MIC_SAMPLE_RATE,

    .bits_per_sample = I2S_BITS_PER_SAMPLE_32BIT,

    .channel_format = I2S_CHANNEL_FMT_ONLY_LEFT,

    .communication_format = I2S_COMM_FORMAT_STAND_I2S,

    .intr_alloc_flags = ESP_INTR_FLAG_LEVEL1,

    .dma_buf_count = 8,

    .dma_buf_len = 64,

    .use_apll = false,

    .tx_desc_auto_clear = false,

    .fixed_mclk = 0
  };

  i2s_pin_config_t micPins = {

    .mck_io_num = I2S_PIN_NO_CHANGE,

    .bck_io_num = MIC_BCLK_PIN,

    .ws_io_num = MIC_WS_PIN,

    .data_out_num = I2S_PIN_NO_CHANGE,

    .data_in_num = MIC_DATA_PIN
  };

  esp_err_t result;

  result = i2s_driver_install(
    MIC_I2S_PORT,
    &micConfig,
    0,
    NULL
  );

  if (result != ESP_OK) {

    Serial.print("[MIC] Driver ERROR: ");
    Serial.println(result);

    return;
  }

  result = i2s_set_pin(
    MIC_I2S_PORT,
    &micPins
  );

  if (result != ESP_OK) {

    Serial.print("[MIC] Pin ERROR: ");
    Serial.println(result);

    return;
  }

  i2s_zero_dma_buffer(MIC_I2S_PORT);

  Serial.println("[MIC] I2S READY");
}

// ==================================================
// SPEAKER I2S
// ==================================================
void initSpeakerI2S() {

  Serial.println("[SPK] Initializing MAX98357A...");

  i2s_config_t spkConfig = {

    .mode = (i2s_mode_t)(
      I2S_MODE_MASTER |
      I2S_MODE_TX
    ),

    .sample_rate = SPK_SAMPLE_RATE,

    .bits_per_sample = I2S_BITS_PER_SAMPLE_16BIT,

    .channel_format = I2S_CHANNEL_FMT_RIGHT_LEFT,

    .communication_format = I2S_COMM_FORMAT_STAND_I2S,

    .intr_alloc_flags = ESP_INTR_FLAG_LEVEL1,

    .dma_buf_count = 32,

    .dma_buf_len = 512,

    .use_apll = false,

    .tx_desc_auto_clear = true,

    .fixed_mclk = 0
  };

  i2s_pin_config_t spkPins = {

    .mck_io_num = I2S_PIN_NO_CHANGE,

    .bck_io_num = SPK_BCLK_PIN,

    .ws_io_num = SPK_LRC_PIN,

    .data_out_num = SPK_DIN_PIN,

    .data_in_num = I2S_PIN_NO_CHANGE
  };

  esp_err_t result;

  result = i2s_driver_install(
    SPK_I2S_PORT,
    &spkConfig,
    0,
    NULL
  );

  if (result != ESP_OK) {

    Serial.print("[SPK] Driver ERROR: ");
    Serial.println(result);

    return;
  }

  result = i2s_set_pin(
    SPK_I2S_PORT,
    &spkPins
  );

  if (result != ESP_OK) {

    Serial.print("[SPK] Pin ERROR: ");
    Serial.println(result);

    return;
  }

  i2s_zero_dma_buffer(SPK_I2S_PORT);

  speakerReady = true;

  Serial.println("[SPK] MAX98357A READY");
}

// ==================================================
// SPEAKER TEST TONE
// ==================================================
void playSpeakerTest() {

  if (!speakerReady) {
    Serial.println("[SPK] Speaker not ready");
    return;
  }

  Serial.println("[SPK] Playing test tone...");

  const int sampleRate = SPK_SAMPLE_RATE;
  const int frequency = 700;

  const int durationMs = 500;
  const int totalSamples =
    (sampleRate * durationMs) / 1000;

  int16_t buffer[256];

  for (int i = 0; i < totalSamples; i += 128) {

    int count = min(128, totalSamples - i);

    for (int j = 0; j < count; j++) {

      float t =
        (float)(i + j) /
        (float)sampleRate;

      int16_t sample =
        (int16_t)(
          sin(2.0f * PI * frequency * t)
          * 10000
        );

      // Stereo
      buffer[j * 2] = sample;
      buffer[j * 2 + 1] = sample;
    }

    size_t written = 0;

    i2s_write(
      SPK_I2S_PORT,
      buffer,
      count * 2 * sizeof(int16_t),
      &written,
      portMAX_DELAY
    );
  }

  Serial.println("[SPK] Test finished");
}

// ==================================================
// MICROPHONE PROCESSING
// ==================================================
void processMicrophoneAudio() {

  // Auto-safety: If no audio received for >1.5s while marked playing, auto-recover
  if (isPlaying && (millis() - lastAudioRxTime > 1500)) {
    isPlaying = false;
    Serial.println("[SPK] Auto-recovered from playback lock (Mic listening...)");
  }

  // CRITICAL FIX: While speaker is playing, or in the 350ms acoustic echo settle window,
  // DO NOT touch mic I2S. Returning immediately gives 100% CPU to webSocket.loop()
  // for smooth speaker DMA playback without stutter or clicks, and stops self-interrupts!
  if (!wsConnected || isPlaying || (lastAudioRxTime != 0 && (millis() - lastAudioRxTime < 100))) {
    return;
  }

  size_t bytesRead = 0;

  esp_err_t result = i2s_read(
    MIC_I2S_PORT,
    rawSamples,
    sizeof(rawSamples),
    &bytesRead,
    10
  );

  if (result != ESP_OK || bytesRead == 0) {
    return;
  }

  size_t sampleCount =
    bytesRead / sizeof(int32_t);

  if (sampleCount > MIC_CHUNK_SAMPLES) {
    sampleCount = MIC_CHUNK_SAMPLES;
  }

  // -----------------------------------------------
  // Convert INMP441 32-bit data -> PCM16
  // -----------------------------------------------

  double sumSquares = 0.0;

  for (size_t i = 0; i < sampleCount; i++) {

    int32_t sample32 = rawSamples[i];

    // DC removal
    dcOffset =
      (dcOffset * 0.995f) +
      ((float)sample32 * 0.005f);

    int32_t cleanSample =
      sample32 - (int32_t)dcOffset;

    // INMP441 -> PCM16
    int32_t shifted =
      cleanSample >> 14;

    if (shifted > 32767)
      shifted = 32767;

    if (shifted < -32768)
      shifted = -32768;

    int16_t pcm =
      (int16_t)shifted;

    pcmSamples[i] = pcm;

    sumSquares +=
      ((double)pcm * (double)pcm);
  }

  double rms = 0.0;

  if (sampleCount > 0) {
    rms = sqrt(sumSquares / sampleCount);
  }

  // -----------------------------------------------
  // DEBUG
  // -----------------------------------------------

  if (millis() - lastMicPrint >= 1000) {

    lastMicPrint = millis();

    Serial.printf(
      "[MIC] bytes=%d samples=%d | "
      "raw0=%ld raw1=%ld raw2=%ld raw3=%ld | "
      "RMS=%.2f\n",

      (int)bytesRead,

      (int)sampleCount,

      (long)rawSamples[0],
      (long)rawSamples[1],
      (long)rawSamples[2],
      (long)rawSamples[3],

      rms
    );
  }

  // -----------------------------------------------
  // SEND PCM TO BACKEND
  // -----------------------------------------------

  webSocket.sendBIN(
    (uint8_t *)pcmSamples,
    sampleCount * sizeof(int16_t)
  );
}

// ==================================================
// WEBSOCKET CONNECTION
// ==================================================
void connectBackend() {

  Serial.println();
  Serial.println("[WS] Connecting to backend...");

  Serial.print("[WS] Server: ");
  Serial.print(SERVER_HOST);
  Serial.print(":");
  Serial.println(SERVER_PORT);

  Serial.print("[WS] Path: ");
  Serial.println(SERVER_PATH);

#if defined(USE_SSL) && USE_SSL
  Serial.println("[WS] Mode: Secure SSL (Cloud wss://)");
  webSocket.beginSSL(
    SERVER_HOST,
    SERVER_PORT,
    SERVER_PATH
  );
#else
  Serial.println("[WS] Mode: Plain TCP (Local ws://)");
  webSocket.begin(
    SERVER_HOST,
    SERVER_PORT,
    SERVER_PATH
  );
#endif

  webSocket.onEvent(webSocketEvent);

  webSocket.setReconnectInterval(3000);

  // Give server a little time
  delay(500);
}

// ==================================================
// SETUP
// ==================================================
void setup() {

  Serial.begin(115200);

  delay(1500);

  Serial.println();
  Serial.println("========================================");
  Serial.println(" ESP32 VOICE ASSISTANT");
  Serial.println("========================================");

  // -----------------------------------------------
  // WIFI + PHONE SETUP PORTAL
  // -----------------------------------------------
  startWiFiPortal();

  // -----------------------------------------------
  // SPEAKER
  // -----------------------------------------------

  initSpeakerI2S();

  delay(300);

  playSpeakerTest();

  delay(500);

  // -----------------------------------------------
  // MICROPHONE
  // -----------------------------------------------

  initMicI2S();

  delay(500);

  // Backend starts once Wi-Fi connects inside serviceWiFi().

  Serial.println();
  Serial.println("========================================");
  Serial.println(" SYSTEM READY");
  Serial.println("========================================");
  Serial.println();
}

// ==================================================
// LOOP
// ==================================================
void loop() {

  // WebSocket processing
  webSocket.loop();

  // Keep setup portal alive and manage station reconnects without blocking audio.
  serviceWiFi();
  if (WiFi.status() != WL_CONNECTED) wsConnected = false;

  // Microphone
  processMicrophoneAudio();

  delay(1);
}