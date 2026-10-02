import asyncio
import os
import openai
from dotenv import load_dotenv

load_dotenv("backend/.env")

api_key = os.getenv("OPENAI_API_KEY")
client = openai.AsyncOpenAI(api_key=api_key)

candidates = [
    "gpt-4o-realtime-preview-2024-12-17",
    "gpt-4o-realtime-preview-2024-10-01",
    "gpt-4o-mini-realtime-preview",
    "gpt-4o-mini-realtime-preview-2024-12-17",
    "gpt-realtime",
    "gpt-realtime-mini"
]

async def test_model(model_name):
    print(f"Testing model: {model_name}...")
    try:
        async with client.realtime.connect(model=model_name) as connection:
            await connection.send({
                "type": "session.update",
                "session": {
                    "turn_detection": {"type": "server_vad"},
                    "input_audio_format": "pcm16",
                    "output_audio_format": "pcm16"
                }
            })
            async for event in connection:
                event_type = getattr(event, "type", None) or (event.get("type") if isinstance(event, dict) else None)
                print(f"[{model_name}] Received event: {event_type}")
                if event_type in ("session.created", "session.updated"):
                    print(f"--> SUCCESS! {model_name} IS SUPPORTED & WORKING.")
                    return True
                elif event_type == "error":
                    print(f"--> ERROR for {model_name}: {event}")
                    return False
    except Exception as e:
        print(f"--> EXCEPTION for {model_name}: {e}")
        return False
    return False

async def main():
    working = []
    for model in candidates:
        res = await test_model(model)
        if res:
            working.append(model)
            break
    print(f"\nFINAL WORKING MODEL: {working}")

if __name__ == "__main__":
    asyncio.run(main())
