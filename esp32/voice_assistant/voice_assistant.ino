#include <Arduino.h>
#include <WiFi.h>
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
        lastAudioRxTime = millis();
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
// WIFI
// ==================================================
void connectWiFi() {

  Serial.println();
  Serial.println("[WIFI] Connecting...");

  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);

  unsigned long start = millis();

  while (WiFi.status() != WL_CONNECTED) {

    delay(500);
    Serial.print(".");

    if (millis() - start > 20000) {
      Serial.println();
      Serial.println("[WIFI] Connection timeout");
      return;
    }
  }

  Serial.println();
  Serial.println("[WIFI] CONNECTED");

  Serial.print("[WIFI] ESP32 IP: ");
  Serial.println(WiFi.localIP());

  Serial.print("[WIFI] RSSI: ");
  Serial.println(WiFi.RSSI());
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

    .dma_buf_count = 16,

    .dma_buf_len = 256,

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

  // Auto-safety: If no audio received for >1.2s, automatically unmute mic
  if (isPlaying && (millis() - lastAudioRxTime > 1200)) {
    isPlaying = false;
    Serial.println("[SPK] Auto-recovered from playback lock (Mic listening...)");
  }

  if (!wsConnected) {
    return;
  }

  size_t bytesRead = 0;

  esp_err_t result = i2s_read(
    MIC_I2S_PORT,
    rawSamples,
    sizeof(rawSamples),
    &bytesRead,
    15
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
  // BARGE-IN: VOICE INTERRUPTION DURING PLAYBACK
  // -----------------------------------------------
  if (isPlaying) {
    // When speaker is playing, normal acoustic sound picked up by mic is ~150-350 RMS.
    // When the user speaks near mic to interrupt, RMS spikes (>550 RMS).
    if (rms > 550.0) {
      Serial.printf("[INTERRUPT] Voice detected (RMS=%.1f)! Cutting off speaker...\n", rms);
      isPlaying = false;
      lastAudioRxTime = 0;
      i2s_zero_dma_buffer(SPK_I2S_PORT);
      webSocket.sendTXT("{\"type\":\"interrupt\"}");
      // Fall through to send this speech chunk so the new question isn't lost
    } else {
      // Normal speaker playback sound; drop to avoid echo feedback
      return;
    }
  } else if (millis() - lastAudioRxTime < 250) {
    // Settle window after playback ends
    return;
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
  // WIFI
  // -----------------------------------------------

  connectWiFi();

  if (WiFi.status() != WL_CONNECTED) {

    Serial.println("[ERROR] WiFi failed");

    return;
  }

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

  // -----------------------------------------------
  // BACKEND
  // -----------------------------------------------

  connectBackend();

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

  // WiFi recovery
  if (WiFi.status() != WL_CONNECTED) {

    wsConnected = false;

    Serial.println("[WIFI] Lost connection");

    connectWiFi();

    delay(500);
  }

  // Microphone
  processMicrophoneAudio();

  delay(1);
}