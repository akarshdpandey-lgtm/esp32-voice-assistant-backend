import os
import sys
import httpx
from dotenv import load_dotenv

# Load backend/.env
ENV_FILE = os.path.join(os.path.dirname(__file__), "backend", ".env")
load_dotenv(ENV_FILE)

OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "deepseek/deepseek-v4-flash")


def test_openrouter_connection():
    if not OPENROUTER_API_KEY or OPENROUTER_API_KEY.startswith("your_"):
        print("[ERROR] OPENROUTER_API_KEY is not set or is using placeholder in backend/.env")
        sys.exit(1)

    url = "https://openrouter.ai/api/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://esp32-voice-assistant.local",
        "X-Title": "ESP32 Voice Assistant"
    }

    payload = {
        "model": OPENROUTER_MODEL,
        "messages": [
            {
                "role": "system",
                "content": "You are a helpful voice assistant."
            },
            {
                "role": "user",
                "content": "Reply with exactly: OpenRouter connection successful."
            }
        ]
    }

    print("========================================")
    print("      TESTING OPENROUTER CONNECTION")
    print("========================================")
    print(f"Target Model: {OPENROUTER_MODEL}")
    print("Sending request to OpenRouter...")

    try:
        with httpx.Client(timeout=20.0) as client:
            response = client.post(url, headers=headers, json=payload)

        print(f"HTTP Status: {response.status_code}")
        print(f"Model used: {OPENROUTER_MODEL}")

        if response.status_code == 200:
            data = response.json()
            content = data["choices"][0]["message"]["content"]
            print(f"Response text: {content.strip()}")
            print("========================================")
            print("[SUCCESS] OpenRouter API is operational.")
            print("========================================")
        else:
            print(f"[ERROR] Request failed with HTTP {response.status_code}")
            print(f"Response body: {response.text[:300]}")
            sys.exit(1)

    except Exception as e:
        print(f"[ERROR] Exception occurred: {e}")
        sys.exit(1)


if __name__ == "__main__":
    test_openrouter_connection()
