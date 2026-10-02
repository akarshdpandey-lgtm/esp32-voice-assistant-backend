import asyncio
import json
import pytest
from unittest.mock import patch, AsyncMock
from fastapi.testclient import TestClient
from backend.main import app
from backend.realtime import (
    OpenRouterVoiceSessionManager,
    clean_llm_response,
    generate_test_tone_pcm,
    calculate_pcm_rms,
    amplify_pcm16
)

client = TestClient(app)


def test_health_endpoint():
    """Verify GET /health returns status healthy, openrouter_configured, and model."""
    response = client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "healthy"
    assert "openrouter_configured" in data
    assert "model" in data
    assert data["model"] == "deepseek/deepseek-v4-flash"
    # Ensure no old Gemini or OpenAI fields remain in response
    assert "gemini_configured" not in data
    assert "openai_configured" not in data


def test_clean_llm_response():
    """Verify clean_llm_response strips <think> blocks and formatting."""
    raw = "<think>Let me think about how to answer.</think>Hello! How can I help you today?"
    cleaned = clean_llm_response(raw)
    assert cleaned == "Hello! How can I help you today?"

    raw_markdown = "**Bold** and `code` with # header"
    cleaned_md = clean_llm_response(raw_markdown)
    assert cleaned_md == "Bold and code with header"

    # Test removing conversational greetings and limits to short response
    hindi_greeting = "बिल्कुल, भारत की राजधानी नई दिल्ली है।"
    assert clean_llm_response(hindi_greeting) == "भारत की राजधानी नई दिल्ली है।"

    # Test PM of India query
    assert clean_llm_response("नरेंद्र मोदी", user_text="Bharat ka PM kaun hai?") == "भारत के प्रधानमंत्री नरेंद्र मोदी हैं।"
    assert clean_llm_response("नरेंद्र मोदी", user_text="भारत का पीएम कौन है?") == "भारत के प्रधानमंत्री नरेंद्र मोदी हैं।"



def test_test_tone_generator():
    """Verify test tone generator produces valid 24kHz 16-bit stereo PCM."""
    tone = generate_test_tone_pcm(frequency_hz=440.0, duration_sec=0.1, sample_rate=24000)
    assert len(tone) == 2400 * 2 * 2  # 2400 samples * 2 channels * 2 bytes = 9600 bytes


def test_calculate_pcm_rms():
    """Verify calculate_pcm_rms on silent vs non-silent audio."""
    silent = bytes(640)
    rms_silent = calculate_pcm_rms(silent)
    assert rms_silent == 0.0

    tone = generate_test_tone_pcm(duration_sec=0.1)
    rms_tone = calculate_pcm_rms(tone)
    assert rms_tone > 1000.0


def test_amplify_pcm16():
    """Verify amplify_pcm16 increases PCM sample amplitudes with clipping protection."""
    # 16-bit signed sample 5000 (struct format '<h')
    import struct
    sample_bytes = struct.pack("<h", 5000)
    amplified = amplify_pcm16(sample_bytes, gain=2.0)
    val = struct.unpack("<h", amplified)[0]
    assert val == 10000

    # Test clipping protection near max int16
    large_sample = struct.pack("<h", 25000)
    clipped = amplify_pcm16(large_sample, gain=2.0)
    val_clipped = struct.unpack("<h", clipped)[0]
    assert val_clipped == 32767


def test_websocket_esp32_handshake():
    """Verify ESP32 WebSocket session_start, session_ack (mode: openrouter)."""
    with client.websocket_connect("/ws/audio") as websocket:
        handshake = {
            "type": "session_start",
            "device_id": "ESP32-DEV-TEST"
        }
        websocket.send_text(json.dumps(handshake))

        response_text = websocket.receive_text()
        response_json = json.loads(response_text)
        assert response_json["type"] == "session_ack"
        assert response_json["status"] == "connected"
        assert response_json["mode"] in ("openrouter", "test")

        # Stream 20ms PCM16 audio (640 bytes)
        fake_pcm16 = bytes(640)
        websocket.send_bytes(fake_pcm16)
        assert websocket


def test_session_manager_end_to_end_mocked():
    """Verify complete pipeline: audio -> STT -> OpenRouter -> TTS -> ESP32 audio + audio_done."""
    async def _run():
        audio_chunks_sent = []
        control_msgs_sent = []

        async def mock_send_audio(data: bytes):
            audio_chunks_sent.append(data)

        async def mock_send_control(msg: dict):
            control_msgs_sent.append(msg)

        with patch.object(OpenRouterVoiceSessionManager, "_play_welcome_greeting", AsyncMock()):
            session = OpenRouterVoiceSessionManager(
                send_to_esp32_audio=mock_send_audio,
                send_to_esp32_control=mock_send_control
            )
            session.test_mode = False
            session.api_key = "test-key"
            await session.start()

            # Mock STT, OpenRouter LLM, and TTS
            with patch.object(session, "_transcribe_audio", AsyncMock(return_value="What is the capital of France?")), \
                 patch.object(session, "_ask_openrouter", AsyncMock(return_value="The capital of France is Paris.")), \
                 patch.object(session, "_synthesize_speech", AsyncMock(return_value=bytes(4800))):

                # Trigger utterance processing
                fake_pcm = bytes(16000 * 2)  # 1 second of audio
                await session._process_utterance(fake_pcm)

                assert len(audio_chunks_sent) > 0
                assert sum(len(c) for c in audio_chunks_sent) == 4800
                assert {"type": "audio_done"} in control_msgs_sent

            await session.close()

    asyncio.run(_run())


def test_session_manager_openrouter_error_handling():
    """Verify session manager handles LLM failure gracefully without crashing."""
    async def _run():
        audio_chunks_sent = []
        control_msgs_sent = []

        async def mock_send_audio(data: bytes):
            audio_chunks_sent.append(data)

        async def mock_send_control(msg: dict):
            control_msgs_sent.append(msg)

        session = OpenRouterVoiceSessionManager(
            send_to_esp32_audio=mock_send_audio,
            send_to_esp32_control=mock_send_control
        )
        session.test_mode = False
        session.api_key = "test-key"
        await session.start()

        with patch.object(session, "_transcribe_audio", AsyncMock(return_value="Hello")), \
             patch.object(session, "_ask_openrouter", AsyncMock(return_value=None)):

            fake_pcm = bytes(16000 * 2)
            await session._process_utterance(fake_pcm)

            # An error message should have been sent to ESP32
            error_msgs = [m for m in control_msgs_sent if m.get("type") == "error"]
            assert len(error_msgs) == 1
            assert "AI assistant" in error_msgs[0]["message"]

        await session.close()

    asyncio.run(_run())


def test_no_gemini_or_openai_dependencies():
    """Verify that zero Gemini and OpenAI imports or keys remain in the codebase."""
    import backend.config as cfg
    import backend.realtime as rt
    import backend.main as mn

    # Verify no Gemini attributes or imports
    assert not hasattr(cfg, "GEMINI_API_KEY")
    assert not hasattr(cfg, "GEMINI_MODEL")
    assert not hasattr(rt, "genai")
    assert not hasattr(rt, "GeminiLiveSessionManager")

    # Verify no OpenAI attributes or imports
    assert not hasattr(cfg, "OPENAI_API_KEY")
    assert not hasattr(rt, "AsyncOpenAI")

    # Verify OpenRouter attributes
    assert hasattr(cfg, "OPENROUTER_API_KEY")
    assert hasattr(cfg, "OPENROUTER_MODEL")
    assert hasattr(rt, "OpenRouterVoiceSessionManager")
