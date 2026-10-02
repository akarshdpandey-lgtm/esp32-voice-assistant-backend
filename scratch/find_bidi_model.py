import asyncio
import os
import dotenv
from google import genai
from google.genai import types

dotenv.load_dotenv("backend/.env")
api_key = os.getenv("GEMINI_API_KEY")
client = genai.Client(api_key=api_key)

models_to_test = [
    "gemini-2.0-flash-exp",
    "gemini-2.0-flash-realtime-exp",
    "gemini-2.0-flash-live-exp",
    "gemini-2.5-flash-native-audio-preview-09-2025",
    "gemini-2.5-flash-native-audio-latest",
    "gemini-3.1-flash-live-preview",
    "gemini-3.5-live-translate-preview",
    "gemini-2.0-flash"
]

async def test_bidi(model_name):
    print(f"Testing model for bidiGenerateContent: {model_name}...")
    config = types.LiveConnectConfig(response_modalities=["AUDIO"])
    try:
        async with client.aio.live.connect(model=model_name, config=config) as session:
            print(f"===> BIDI SUCCESS! {model_name} IS SUPPORTED FOR LIVE API!")
            return model_name
    except Exception as e:
        print(f"FAILED {model_name}: {e}")
        return None

async def main():
    working = []
    for m in models_to_test:
        res = await test_bidi(m)
        if res:
            working.append(res)
    print("\nALL WORKING BIDI LIVE MODELS:", working)

if __name__ == "__main__":
    asyncio.run(main())
