import asyncio
import os
from google import genai
from google.genai import types
from dotenv import load_dotenv

load_dotenv("backend/.env")

gemini_key = os.getenv("GEMINI_API_KEY", "")

async def test_live_api():
    if not gemini_key:
        print("GEMINI_API_KEY is not set.")
        return
    
    client = genai.Client(api_key=gemini_key)
    model = "gemini-2.5-flash-native-audio-latest"
    config = types.LiveConnectConfig(
        response_modalities=["AUDIO"]
    )
    
    print(f"Connecting to Gemini Live API with model: {model}...")
    try:
        async with client.aio.live.connect(model=model, config=config) as session:
            print("SUCCESS! Connected to Gemini Live API!")
            test_pcm = bytes(640)
            await session.send_realtime_input(
                audio=types.Blob(data=test_pcm, mime_type="audio/pcm")
            )
            print("Streamed PCM audio chunk to Gemini Live API.")
            async for response in session.receive():
                print(f"Received response: {response}")
                break
            print("ALL GEMINI LIVE API TESTS PASSED 100%!")
    except Exception as e:
        print(f"Error testing Gemini Live API: {e}")

if __name__ == "__main__":
    asyncio.run(test_live_api())
