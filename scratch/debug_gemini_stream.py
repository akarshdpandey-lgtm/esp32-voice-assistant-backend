import asyncio
import os
import traceback
import dotenv
from google import genai
from google.genai import types

dotenv.load_dotenv("backend/.env")

gemini_key = os.getenv("GEMINI_API_KEY", "")
gemini_model = os.getenv("GEMINI_MODEL", "gemini-2.5-flash-native-audio-latest")

async def test_turn_complete():
    print(f"[GEMINI] Connecting to model: {gemini_model}")
    client = genai.Client(api_key=gemini_key)
    
    config = types.LiveConnectConfig(
        response_modalities=["AUDIO"],
        system_instruction=types.Content(
            parts=[types.Part.from_text(
                text="You are a concise voice assistant. Respond with a brief greeting in audio."
            )]
        )
    )
    
    try:
        async with client.aio.live.connect(model=gemini_model, config=config) as session:
            print("[GEMINI] Connected")
            print("[GEMINI] Session configured")
            
            async def receive_loop():
                try:
                    async for response in session.receive():
                        print(f"[GEMINI] Event received: {response}")
                        server_content = getattr(response, "server_content", None)
                        if server_content and getattr(server_content, "model_turn", None):
                            for part in server_content.model_turn.parts:
                                if getattr(part, "inline_data", None) and part.inline_data.data:
                                    print(f"[GEMINI] Audio response received: {len(part.inline_data.data)} bytes")
                except Exception as e:
                    print(f"[GEMINI] ERROR in receive loop: {e}")
                    traceback.print_exc()

            recv_task = asyncio.create_task(receive_loop())

            # Send a prompt or audio input
            print("[GEMINI] Sending text prompt and marking turn complete...")
            await session.send_client_content(
                turns=[types.Content(parts=[types.Part.from_text(text="Hello! Say hi to ESP32.")])],
                turn_complete=True
            )

            print("[GEMINI] Waiting for Gemini audio response...")
            await asyncio.sleep(5.0)
            recv_task.cancel()

    except Exception as e:
        print(f"[GEMINI] ERROR in connection: {e}")
        traceback.print_exc()

if __name__ == "__main__":
    asyncio.run(test_turn_complete())
