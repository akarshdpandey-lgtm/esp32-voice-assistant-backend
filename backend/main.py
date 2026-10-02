import logging
import json
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from backend.config import OPENROUTER_API_KEY, OPENROUTER_MODEL, HOST, PORT, TEST_MODE
from backend.realtime import OpenRouterVoiceSessionManager

# Configure logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("backend.main")

app = FastAPI(title="ESP32 OpenRouter Voice Assistant Backend")


@app.get("/health")
async def health_check():
    openrouter_configured = bool(OPENROUTER_API_KEY and not OPENROUTER_API_KEY.startswith("your_"))
    active_test_mode = TEST_MODE or not openrouter_configured
    return JSONResponse({
        "status": "healthy",
        "openrouter_configured": openrouter_configured,
        "model": OPENROUTER_MODEL,
        "test_mode": active_test_mode
    })


@app.websocket("/ws/audio")
async def websocket_audio_endpoint(websocket: WebSocket):
    await websocket.accept()
    logger.info(f"[WS] ESP32 connected from {websocket.client.host}:{websocket.client.port}")

    session_manager = None

    async def send_audio_to_esp32(pcm_bytes: bytes):
        try:
            await websocket.send_bytes(pcm_bytes)
        except Exception as e:
            logger.error(f"[WS] Error sending binary audio to ESP32: {e}")

    async def send_control_to_esp32(msg: dict):
        try:
            await websocket.send_text(json.dumps(msg))
        except Exception as e:
            logger.error(f"[WS] Error sending control JSON to ESP32: {e}")

    try:
        session_manager = OpenRouterVoiceSessionManager(
            send_to_esp32_audio=send_audio_to_esp32,
            send_to_esp32_control=send_control_to_esp32
        )

        while True:
            try:
                message = await websocket.receive()
            except RuntimeError as re:
                if "disconnect" in str(re).lower():
                    logger.info("[WS] ESP32 disconnected.")
                    break
                raise re

            if message.get("type") == "websocket.disconnect":
                logger.info("[WS] Received disconnect signal.")
                break

            if "text" in message and message["text"]:
                try:
                    data = json.loads(message["text"])
                    msg_type = data.get("type")
                    logger.info(f"[WS] Received text message: {data}")

                    if msg_type == "session_start":
                        device_id = data.get("device_id", "UNKNOWN")
                        logger.info(f"[WS] Starting session for device: {device_id}")
                        await session_manager.start()

                except json.JSONDecodeError:
                    logger.warning("[WS] Invalid JSON received from client.")

            elif "bytes" in message and message["bytes"]:
                raw_bytes = message["bytes"]
                logger.debug(
                    f"[AUDIO] Received PCM from ESP32: {len(raw_bytes)} bytes"
                )
                if session_manager:
                    await session_manager.send_audio_chunk(raw_bytes)

    except WebSocketDisconnect:
        logger.info("[WS] ESP32 client disconnected.")
    except Exception as e:
        logger.error(f"[WS] Unexpected WebSocket error: {e}")
    finally:
        if session_manager:
            await session_manager.close()
        logger.info("[WS] WebSocket session cleaned up.")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("backend.main:app", host=HOST, port=PORT, reload=True)
