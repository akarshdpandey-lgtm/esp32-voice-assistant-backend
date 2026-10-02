import os
from dotenv import load_dotenv

# Always load the .env located beside this config.py
ENV_FILE = os.path.join(os.path.dirname(__file__), ".env")
load_dotenv(ENV_FILE)

OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
OPENROUTER_MODEL = os.getenv(
    "OPENROUTER_MODEL",
    "deepseek/deepseek-v4-flash"
)
HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "8000"))
TEST_MODE = os.getenv("TEST_MODE", "false").lower() in ("true", "1", "yes")
AUDIO_GAIN = float(os.getenv("AUDIO_GAIN", "2.0"))
ENABLE_WELCOME = os.getenv("ENABLE_WELCOME", "true").lower() in ("true", "1", "yes")
WELCOME_MESSAGE = os.getenv("WELCOME_MESSAGE", "नमस्ते! आपका स्वागत है।")
VAD_THRESHOLD_RMS = float(os.getenv("VAD_THRESHOLD_RMS", "250.0"))
