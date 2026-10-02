import asyncio
import json
import websockets

async def test_websocket():
    uri = "ws://127.0.0.1:8000/ws/audio"
    print(f"Connecting to {uri}...")
    async with websockets.connect(uri) as websocket:
        print("Connected to WebSocket endpoint.")
        
        # Send session_start
        start_msg = {
            "type": "session_start",
            "device_id": "ESP32-TEST-INTEGRATION"
        }
        await websocket.send(json.dumps(start_msg))
        print("Sent session_start.")
        
        # Receive session_ack
        response = await websocket.recv()
        print(f"Received from server: {response}")
        
        # Stream 20ms of microphone PCM16 audio (640 bytes)
        fake_pcm16 = bytes(640)
        await websocket.send(fake_pcm16)
        print("Streamed 640 bytes binary PCM audio to backend.")
        
        # In test mode, receive generated 24kHz PCM test tone audio response
        audio_response = await websocket.recv()
        print(f"Received binary response from server: {len(audio_response)} bytes audio payload!")
        assert isinstance(audio_response, bytes)
        assert len(audio_response) > 0
        print("SUCCESS! Local WebSocket integration test passed 100%.")

if __name__ == "__main__":
    asyncio.run(test_websocket())
