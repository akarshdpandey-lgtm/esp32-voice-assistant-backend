#ifndef CONFIG_H
#define CONFIG_H

// ==================================================
// WIFI CONFIGURATION
// ==================================================

#define WIFI_SSID "Airtel_vije_3810"
#define WIFI_PASSWORD "Air@87059"

// ==================================================
// WEBSOCKET BACKEND CONFIGURATION
// ==================================================

// Local PC connection:
// ws://192.168.1.7:8000/ws/audio

// Cloud connection ke liye baad mein:
// USE_SSL true
// SERVER_HOST = cloud domain
// SERVER_PORT = 443

#define USE_SSL false

#define SERVER_HOST "192.168.1.7"
#define SERVER_PORT 8000
#define SERVER_PATH "/ws/audio"

// ==================================================
// INMP441 MICROPHONE (I2S INPUT)
// ==================================================

#define MIC_I2S_PORT I2S_NUM_0

#define MIC_BCLK_PIN 14
#define MIC_WS_PIN 15
#define MIC_DATA_PIN 32

#define MIC_SAMPLE_RATE 16000
#define MIC_BITS_PER_SAMPLE I2S_BITS_PER_SAMPLE_32BIT

// ==================================================
// MAX98357A SPEAKER (I2S OUTPUT)
// ==================================================

#define SPK_I2S_PORT I2S_NUM_1

#define SPK_BCLK_PIN 26
#define SPK_LRC_PIN 25
#define SPK_DIN_PIN 22

#define SPK_SAMPLE_RATE 24000
#define SPK_BITS_PER_SAMPLE I2S_BITS_PER_SAMPLE_16BIT

// ==================================================
// AUDIO BUFFER & STREAMING SETTINGS
// ==================================================

#define MIC_CHUNK_SAMPLES 320
#define WIFI_RETRY_INTERVAL_MS 5000

#endif // CONFIG_H