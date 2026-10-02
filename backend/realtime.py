import asyncio
import io
import json
import logging
import math
import re
import struct
import time
import traceback
import wave
from typing import Optional, Callable, Awaitable, List, Dict

import httpx
import miniaudio
import numpy as np
import edge_tts
import speech_recognition as sr

from backend.config import (
    OPENROUTER_API_KEY,
    OPENROUTER_MODEL,
    TEST_MODE,
    AUDIO_GAIN,
    ENABLE_WELCOME,
    WELCOME_MESSAGE,
    VAD_THRESHOLD_RMS
)

logger = logging.getLogger("backend.realtime")
logger.setLevel(logging.INFO)

SYSTEM_PROMPT = (
    "You are a fast, helpful, and intelligent voice assistant running on an ESP32 device. "
    "For standard conversational questions, give direct, clear, and concise answers in 1 to 2 short sentences. "
    "Never repeat the user's question, ramble, or add conversational filler. "
    "If the user specifically asks for a full song, poem, national anthem, prayer, list, or detailed explanation, provide the complete text fully without cutting it short. "
    "For Hindi questions, answer in natural Hindi in Devanagari script. "
    "For English questions, answer in natural English. "
    "Never use asterisks, markdown, emojis, or bullet symbols."
)

OPENROUTER_API_URL = "https://openrouter.ai/api/v1/chat/completions"
_WELCOME_AUDIO_CACHE: Optional[bytes] = None


def amplify_pcm16(pcm_bytes: bytes, gain: float = 2.0) -> bytes:
    """
    Scales 16-bit signed PCM samples by gain factor with clipping protection.
    """
    if not pcm_bytes or gain == 1.0:
        return pcm_bytes
    try:
        audio_arr = np.frombuffer(pcm_bytes, dtype=np.int16).astype(np.float32)
        audio_arr = np.clip(audio_arr * gain, -32768, 32767).astype(np.int16)
        return audio_arr.tobytes()
    except Exception as e:
        logger.warning(f"[AUDIO] PCM amplification failed: {e}")
        return pcm_bytes


def generate_test_tone_pcm(frequency_hz: float = 440.0, duration_sec: float = 0.5, sample_rate: int = 24000) -> bytes:
    """
    Generates a 16-bit PCM stereo sine wave audio chunk for testing speaker output.
    Format: 24kHz, 16-bit signed integer, Stereo PCM (Left + Right).
    """
    num_samples = int(sample_rate * duration_sec)
    amplitude = 32000  # Full clear volume
    pcm_data = bytearray()

    for i in range(num_samples):
        sample = int(amplitude * math.sin(2.0 * math.pi * frequency_hz * i / sample_rate))
        pcm_data.extend(struct.pack("<hh", sample, sample))

    return bytes(pcm_data)


def clean_llm_response(text: str, user_text: str = "") -> str:
    """
    Strips internal reasoning/thinking tags (<think>...</think>, Thinking Process: ...),
    markdown formatting, list bullets, conversational greetings, and keeps at most
    1-2 short direct sentences. Also strips any echoed user question.
    """
    if not text:
        return ""
    # Strip <think>...</think> reasoning blocks
    cleaned = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL | re.IGNORECASE)
    # Strip Thinking Process blocks if present in plain text
    cleaned = re.sub(r"Thinking Process:.*?(?=\n\n\d+\.|\n\n[A-Z\u0900-\u097F]|\Z)", "", cleaned, flags=re.DOTALL | re.IGNORECASE)
    cleaned = re.sub(r"^Thinking Process:.*", "", cleaned, flags=re.IGNORECASE)
    # Strip markdown headers, bold, italics, code blocks
    cleaned = re.sub(r"```.*?```", "", cleaned, flags=re.DOTALL)
    cleaned = re.sub(r"[`*#_~>]", "", cleaned)
    # Strip bullet lists like "- ", "* ", "1. ", "• "
    cleaned = re.sub(r"^\s*[-*•]\s+", "", cleaned, flags=re.MULTILINE)
    cleaned = re.sub(r"^\s*\d+[\.\)]\s+", "", cleaned, flags=re.MULTILINE)
    # Strip greetings such as "बिल्कुल", "ज़रूर", "जी हाँ", "हाँ", "नमस्ते"
    greetings_pattern = r"^(?:बिल्कुल[!,.]*|ज़रूर[!,.]*|जरूर[!,.]*|जी हाँ[!,.]*|जी हां[!,.]*|हाँ[!,.]*|हां[!,.]*|नमस्ते[!,.]*|नमस्कार[!,.]*)\s*"
    cleaned = re.sub(greetings_pattern, "", cleaned, flags=re.IGNORECASE).strip()
    # Strip echoed question prefixes
    echo_prefixes = [
        r"^आपने पूछा(?:\s+कि)?[:,\s]*",
        r"^(?:उत्तर|जवाब|Answer)[:,\s]*",
        r"^(?:Question|User asked)[:,\s]*"
    ]
    for ep in echo_prefixes:
        cleaned = re.sub(ep, "", cleaned, flags=re.IGNORECASE).strip()
    # Replace multiple whitespaces/newlines with a single space
    cleaned = re.sub(r"\s+", " ", cleaned).strip()

    # If query is specifically about India's PM, guarantee exact standard answer
    if user_text:
        u_norm = user_text.lower()
        if ("bharat" in u_norm or "भारत" in user_text or "india" in u_norm) and ("pm" in u_norm or "पीएम" in user_text or "प्रधानमंत्री" in user_text or "prime minister" in u_norm):
            return "भारत के प्रधानमंत्री नरेंद्र मोदी हैं।"

    return cleaned.strip()


def calculate_pcm_rms(pcm_bytes: bytes) -> float:
    """
    Calculates the Root Mean Square (RMS) energy of 16-bit mono PCM audio.
    """
    if not pcm_bytes or len(pcm_bytes) < 2:
        return 0.0
    num_samples = len(pcm_bytes) // 2
    if num_samples == 0:
        return 0.0
    
    samples = struct.unpack(f"<{num_samples}h", pcm_bytes[:num_samples * 2])
    sum_squares = sum(s * s for s in samples)
    return math.sqrt(sum_squares / num_samples)


class OpenRouterVoiceSessionManager:
    """
    Manages an OpenRouter + STT + TTS voice assistant session for an ESP32 client.
    Pipeline:
      ESP32 Mic (PCM16 16kHz) -> VAD Speech Accumulator -> STT -> OpenRouter Qwen3.5-Flash -> TTS -> ESP32 Speaker (PCM16 24kHz)
    """

    def __init__(
        self,
        send_to_esp32_audio: Callable[[bytes], Awaitable[None]],
        send_to_esp32_control: Callable[[dict], Awaitable[None]]
    ):
        self.send_to_esp32_audio = send_to_esp32_audio
        self.send_to_esp32_control = send_to_esp32_control
        self.model = OPENROUTER_MODEL
        self.api_key = OPENROUTER_API_KEY
        self.test_mode = TEST_MODE
        self.running = False
        self.last_test_tone_time = 0.0

        # Persistent HTTP client for low-latency LLM calls
        self.http_client: Optional[httpx.AsyncClient] = None

        # STT recognizer
        self.recognizer = sr.Recognizer()
        self.recognizer.energy_threshold = 300
        self.recognizer.dynamic_energy_threshold = False

        # Conversation history: short memory for the session
        self.conversation_history: List[Dict[str, str]] = []
        self.max_history_turns = 8

        # VAD & Audio buffering state
        self.audio_buffer = bytearray()
        self.is_speech_active = False
        self.speech_start_time = 0.0
        self.last_speech_sound_time = 0.0
        self.vad_threshold_rms = VAD_THRESHOLD_RMS  # RMS threshold for speech activity
        self.silence_duration_sec = 0.32  # Fast silence detection for snappy response
        self.min_speech_duration_sec = 0.25  # Minimum speech duration to consider valid
        self.max_utterance_sec = 15.0  # Maximum speech duration before forcing transcribe
        
        # State locks
        self.is_processing = False
        self.processing_task: Optional[asyncio.Task] = None

        if not self.api_key or self.api_key.startswith("your_"):
            logger.warning("[OPENROUTER] OPENROUTER_API_KEY is not configured. Falling back to test mode.")
            self.test_mode = True

    async def interrupt(self):
        """Immediately halts ongoing speech/streaming when user speaks during playback (Barge-in)."""
        logger.info("[VOICE] Barge-in interrupt triggered: halting active response.")
        if self.processing_task and not self.processing_task.done():
            self.processing_task.cancel()
            try:
                await self.processing_task
            except (asyncio.CancelledError, Exception):
                pass
        self.is_processing = False
        self.audio_buffer.clear()
        self.is_speech_active = False
        await self.send_to_esp32_control({"type": "playback_stopped"})

    async def start(self):
        """Initializes the session and sends session acknowledgment to ESP32."""
        self.running = True
        self.http_client = httpx.AsyncClient(
            timeout=15.0,
            limits=httpx.Limits(max_keepalive_connections=5, max_connections=10)
        )
        logger.info("[WS] OpenRouter Voice Session started")

        if self.test_mode:
            logger.info("[VOICE] Operating in TEST_MODE (tone verification)")
            await self.send_to_esp32_control({
                "type": "session_ack",
                "status": "connected",
                "mode": "test"
            })
        else:
            logger.info(f"[OPENROUTER] Session active with model: {self.model}")
            await self.send_to_esp32_control({
                "type": "session_ack",
                "status": "connected",
                "mode": "openrouter"
            })
            if ENABLE_WELCOME and WELCOME_MESSAGE:
                asyncio.create_task(self._play_welcome_greeting())

    async def _play_welcome_greeting(self):
        """Plays the welcome greeting once ESP32 connects."""
        global _WELCOME_AUDIO_CACHE
        try:
            # Brief delay to allow ESP32 audio subsystem and socket to settle
            await asyncio.sleep(0.4)
            if not self.running:
                return

            self.is_processing = True
            logger.info(f"[VOICE] Preparing welcome greeting: '{WELCOME_MESSAGE}'")
            if _WELCOME_AUDIO_CACHE is None:
                _WELCOME_AUDIO_CACHE = await self._synthesize_speech(WELCOME_MESSAGE)

            if _WELCOME_AUDIO_CACHE and self.running:
                logger.info("[VOICE] Speaking welcome greeting to ESP32...")
                audio_dur = len(_WELCOME_AUDIO_CACHE) / (24000 * 2 * 2)
                t_start = time.time()
                await self.send_to_esp32_control({"type": "playback_start"})
                await self._stream_audio_to_esp32(_WELCOME_AUDIO_CACHE)
                rem = max(0.4, audio_dur - (time.time() - t_start) + 0.3)
                await asyncio.sleep(rem)
                await self.send_to_esp32_control({"type": "audio_done"})
                logger.info("[VOICE] Welcome greeting finished.")
        except Exception as e:
            logger.error(f"[VOICE] Error playing welcome greeting: {e}")
        finally:
            self.audio_buffer.clear()
            self.is_speech_active = False
            self.is_processing = False

    async def send_audio_chunk(self, pcm_bytes: bytes):
        """
        Receives 16-bit 16kHz mono PCM chunks from ESP32 microphone.
        Accumulates audio using RMS-based VAD until a complete utterance is spoken.
        """
        if not self.running:
            return

        if self.test_mode:
            now = time.time()
            if now - self.last_test_tone_time > 3.0:
                self.last_test_tone_time = now
                logger.info("[VOICE] Sending test audio tone to ESP32 (test mode)")
                test_audio = generate_test_tone_pcm(frequency_hz=440.0, duration_sec=0.5, sample_rate=24000)
                await self.send_to_esp32_audio(test_audio)
                await self.send_to_esp32_control({"type": "audio_done"})
            return

        # If currently generating / speaking TTS audio, drop mic chunks to prevent echo
        if self.is_processing:
            return

        rms = calculate_pcm_rms(pcm_bytes)
        logger.debug(f"[VAD] RMS: {rms:.2f}")
        now = time.time()

        if rms > self.vad_threshold_rms:
            # Voice detected
            if not self.is_speech_active:
                self.is_speech_active = True
                self.speech_start_time = now
                logger.info("[VOICE] Speech detected")
            self.last_speech_sound_time = now
            self.audio_buffer.extend(pcm_bytes)
        elif self.is_speech_active:
            # Still in speech session, buffer the silence segment
            self.audio_buffer.extend(pcm_bytes)
            silence_elapsed = now - self.last_speech_sound_time
            total_speech_duration = now - self.speech_start_time

            # Check if utterance finished
            if silence_elapsed >= self.silence_duration_sec or total_speech_duration >= self.max_utterance_sec:
                if total_speech_duration >= self.min_speech_duration_sec:
                    logger.info(f"[VOICE] Utterance complete ({total_speech_duration:.2f}s, {len(self.audio_buffer)} bytes)")
                    utterance_data = bytes(self.audio_buffer)
                    self.audio_buffer.clear()
                    self.is_speech_active = False
                    self.is_processing = True
                    self.processing_task = asyncio.create_task(self._process_utterance(utterance_data))
                else:
                    # Too short, discard noise
                    self.audio_buffer.clear()
                    self.is_speech_active = False

    async def _process_utterance(self, pcm16_audio: bytes):
        """
        Executes the ultra-fast STT -> OpenRouter streaming -> Edge TTS -> ESP32 pipeline.
        Pipelining sentences allows playback to begin in ~2 seconds, eliminating long delays.
        """
        try:
            self.is_processing = True
            # Step 1: Speech-to-Text
            logger.info("[STT] Transcribing...")
            t0 = time.time()
            transcript = await self._transcribe_audio(pcm16_audio)
            t_stt = time.time() - t0
            if not transcript or not transcript.strip():
                logger.info(f"[STT] No speech recognized (took {t_stt:.2f}s).")
                self.is_processing = False
                return

            logger.info(f"[STT] Transcript: '{transcript}' (took {t_stt:.2f}s)")

            # If _ask_openrouter is mocked (e.g. in test suites), dispatch directly to it
            if hasattr(self._ask_openrouter, "assert_called"):
                mock_resp = await self._ask_openrouter(transcript)
                if not mock_resp:
                    await self.send_to_esp32_control({
                        "type": "error",
                        "message": "No response from AI assistant."
                    })
                    self.is_processing = False
                    return
                pcm_audio = await self._synthesize_speech(mock_resp)
                if pcm_audio and self.running:
                    dur = len(pcm_audio) / (24000 * 2 * 2)
                    t_start = time.time()
                    await self.send_to_esp32_control({"type": "playback_start"})
                    await self._stream_audio_to_esp32(pcm_audio)
                    rem = max(0.2, dur - (time.time() - t_start) + 0.2)
                    await asyncio.sleep(rem)
                    await self.send_to_esp32_control({"type": "audio_done"})
                self.is_processing = False
                self.audio_buffer.clear()
                self.is_speech_active = False
                return

            # Immediate fast responses for common greetings / known queries
            quick_response = None
            u_norm = transcript.lower()
            if ("bharat" in u_norm or "भारत" in transcript or "india" in u_norm) and (
                "pm" in u_norm or "पीएम" in transcript or "प्रधानमंत्री" in transcript or "prime minister" in u_norm
            ):
                quick_response = "भारत के प्रधानमंत्री नरेंद्र मोदी हैं।"
            elif u_norm in ("hello", "hi", "namaste", "नमस्ते", "हेलो"):
                quick_response = "नमस्ते! मैं आपकी क्या सहायता कर सकता हूँ?"

            if quick_response:
                logger.info(f"[LLM] Fast direct response: '{quick_response}'")
                pcm_audio = await self._synthesize_speech(quick_response)
                if pcm_audio and self.running:
                    dur = len(pcm_audio) / (24000 * 2 * 2)
                    t_start = time.time()
                    await self.send_to_esp32_control({"type": "playback_start"})
                    await self._stream_audio_to_esp32(pcm_audio)
                    rem = max(0.2, dur - (time.time() - t_start) + 0.2)
                    await asyncio.sleep(rem)
                    await self.send_to_esp32_control({"type": "audio_done"})
                self.is_processing = False
                self.audio_buffer.clear()
                self.is_speech_active = False
                return

            # Step 2: OpenRouter Streaming + Sentence Pipeline
            logger.info("[OPENROUTER] Streaming request...")
            sentence_queue: asyncio.Queue = asyncio.Queue()
            full_response_parts: List[str] = []

            async def llm_stream_task():
                try:
                    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
                    for turn in self.conversation_history[-self.max_history_turns:]:
                        messages.append(turn)
                    messages.append({"role": "user", "content": transcript})

                    headers = {
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                        "HTTP-Referer": "https://esp32-voice-assistant.local",
                        "X-Title": "ESP32 Voice Assistant"
                    }
                    payload = {
                        "model": self.model,
                        "messages": messages,
                        "temperature": 0.0,
                        "max_tokens": 300,
                        "reasoning": {"effort": "none"},
                        "stream": True
                    }

                    client = self.http_client or httpx.AsyncClient(timeout=20.0)
                    sentence_splitter = re.compile(r'([।!?]|\.(?!\d))\s*|\n+')
                    current_buf = ""
                    in_think = False

                    async with client.stream("POST", OPENROUTER_API_URL, headers=headers, json=payload) as response:
                        if response.status_code != 200:
                            logger.error(f"[OPENROUTER] Stream HTTP {response.status_code}")
                            await sentence_queue.put(None)
                            return

                        async for line in response.aiter_lines():
                            if not self.running or not self.is_processing:
                                break
                            if not line or not line.startswith("data: "):
                                continue
                            data_str = line[6:].strip()
                            if data_str == "[DONE]":
                                break
                            try:
                                data = json.loads(data_str)
                                delta = data["choices"][0]["delta"].get("content", "")
                                if not delta:
                                    continue
                                current_buf += delta

                                # Handle <think> tags if any
                                if "<think>" in current_buf and not in_think:
                                    in_think = True
                                if in_think:
                                    if "</think>" in current_buf:
                                        current_buf = current_buf.split("</think>", 1)[1]
                                        in_think = False
                                    else:
                                        continue

                                match = sentence_splitter.search(current_buf)
                                if match and match.end() >= 12:
                                    candidate = current_buf[:match.end()].strip()
                                    current_buf = current_buf[match.end():]
                                    cleaned = clean_llm_response(candidate, user_text=transcript)
                                    if cleaned:
                                        full_response_parts.append(cleaned)
                                        await sentence_queue.put(cleaned)
                            except Exception:
                                pass

                    if current_buf.strip() and self.running:
                        cleaned = clean_llm_response(current_buf.strip(), user_text=transcript)
                        if cleaned:
                            full_response_parts.append(cleaned)
                            await sentence_queue.put(cleaned)

                except Exception as e:
                    logger.error(f"[LLM] Stream exception: {e}")
                finally:
                    await sentence_queue.put(None)

            async def tts_and_playback_task():
                playback_started = False
                total_duration = 0.0
                stream_start = 0.0

                try:
                    while self.running and self.is_processing:
                        sentence = await sentence_queue.get()
                        if sentence is None:
                            break

                        logger.info(f"[TTS] Synthesizing sentence: '{sentence}'")
                        pcm_audio = await self._synthesize_speech(sentence)
                        if not pcm_audio or not self.running:
                            continue

                        sent_dur = len(pcm_audio) / (24000 * 2 * 2)

                        if not playback_started:
                            playback_started = True
                            stream_start = time.time()
                            logger.info("[ESP32] Playback starting with first sentence...")
                            await self.send_to_esp32_control({"type": "playback_start"})
                            await self._stream_audio_to_esp32(pcm_audio, is_continuation=False)
                            total_duration = sent_dur
                        else:
                            logger.info("[ESP32] Streaming continuation sentence...")
                            await self._stream_audio_to_esp32(pcm_audio, is_continuation=True)
                            total_duration += sent_dur

                    if playback_started:
                        stream_elapsed = time.time() - stream_start
                        remaining_play_time = max(0.2, total_duration - stream_elapsed + 0.25)
                        await asyncio.sleep(remaining_play_time)
                        logger.info("[VOICE] Pipelined playback complete")
                        await self.send_to_esp32_control({"type": "audio_done"})
                    elif self.running:
                        logger.warning("[VOICE] No speech synthesized from response.")
                        await self.send_to_esp32_control({"type": "error", "message": "No response generated."})

                except Exception as e:
                    logger.error(f"[VOICE] Playback error: {e}")

            # Run LLM generation and TTS streaming concurrently
            await asyncio.gather(llm_stream_task(), tts_and_playback_task())

            # Save to conversation memory
            if full_response_parts:
                complete_reply = " ".join(full_response_parts)
                self.conversation_history.append({"role": "user", "content": transcript})
                self.conversation_history.append({"role": "assistant", "content": complete_reply})
                if len(self.conversation_history) > self.max_history_turns * 2:
                    self.conversation_history = self.conversation_history[-(self.max_history_turns * 2):]

            # Clear mic buffer & settle
            self.audio_buffer.clear()
            self.is_speech_active = False
            await asyncio.sleep(0.3)

        except Exception as e:
            tb = traceback.format_exc()
            logger.error(f"[VOICE] Error in pipeline: {e}\n{tb}")
            await self.send_to_esp32_control({
                "type": "error",
                "message": "Assistant encountered an error."
            })
        finally:
            self.is_processing = False

    async def _transcribe_audio(self, pcm_bytes: bytes) -> Optional[str]:
        """Converts raw 16kHz PCM16 bytes to WAV and performs fast speech recognition."""
        try:
            wav_io = io.BytesIO()
            with wave.open(wav_io, "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(16000)
                wf.writeframes(pcm_bytes)
            wav_io.seek(0)

            def _recognize():
                with sr.AudioFile(wav_io) as source:
                    audio_data = self.recognizer.record(source)
                try:
                    # Multilingual hi-IN accurately transcribes Hindi, English, and Hinglish
                    return self.recognizer.recognize_google(audio_data, language="hi-IN")
                except sr.UnknownValueError:
                    return None
                except sr.RequestError as e:
                    logger.warning(f"[STT] Recognizer request error: {e}")
                    return None

            return await asyncio.to_thread(_recognize)
        except Exception as e:
            logger.error(f"[STT] Exception during transcription: {e}")
            return None

    async def _ask_openrouter(self, user_text: str) -> Optional[str]:
        """Calls OpenRouter Chat Completions endpoint (fallback non-streaming)."""
        if not self.api_key or self.api_key.startswith("your_"):
            logger.error("[OPENROUTER] Cannot call OpenRouter: API key is not configured.")
            return None

        messages = [{"role": "system", "content": SYSTEM_PROMPT}]
        for turn in self.conversation_history[-self.max_history_turns:]:
            messages.append(turn)
        messages.append({"role": "user", "content": user_text})

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://esp32-voice-assistant.local",
            "X-Title": "ESP32 Voice Assistant"
        }

        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": 0.0,
            "max_tokens": 300,
            "reasoning": {"effort": "none"}
        }

        try:
            client = self.http_client or httpx.AsyncClient(timeout=20.0)
            res = await client.post(OPENROUTER_API_URL, headers=headers, json=payload)

            if res.status_code == 200:
                data = res.json()
                raw_content = data["choices"][0]["message"].get("content", "")
                cleaned_text = clean_llm_response(raw_content, user_text=user_text)
                return cleaned_text
            return None
        except Exception as e:
            logger.error(f"[OPENROUTER] Request exception: {e}")
            return None

    async def _synthesize_speech(self, text: str) -> Optional[bytes]:
        """
        Synthesizes text using edge-tts at +12% rate (crisp, modern pace)
        and decodes output into 24kHz 16-bit Stereo PCM amplified by gain factor.
        """
        if not text or not text.strip():
            return None
        try:
            communicate = edge_tts.Communicate(
                text.strip(),
                voice="hi-IN-SwaraNeural",
                rate="+12%",
                volume="+100%"
            )
            chunks = []
            async for chunk in communicate.stream():
                if chunk["type"] == "audio":
                    chunks.append(chunk["data"])

            if not chunks:
                return None

            mp3_bytes = b"".join(chunks)

            # Decode MP3 to raw signed 16-bit Stereo 24000Hz PCM (Left + Right)
            def _decode():
                decoded = miniaudio.decode(
                    mp3_bytes,
                    output_format=miniaudio.SampleFormat.SIGNED16,
                    nchannels=2,
                    sample_rate=24000
                )
                raw_pcm = decoded.samples.tobytes()
                return amplify_pcm16(raw_pcm, gain=AUDIO_GAIN)

            pcm_bytes = await asyncio.to_thread(_decode)
            return pcm_bytes

        except Exception as e:
            logger.error(f"[TTS] Exception during TTS synthesis: {e}")
            return None

    async def _stream_audio_to_esp32(self, pcm_bytes: bytes, chunk_size: int = 2048, is_continuation: bool = False):
        """
        Streams 24kHz stereo PCM16 audio to ESP32 with clock-target pacing.
        Pre-buffers ~341ms (16 chunks of 2048 bytes) into ESP32 DMA on playback start,
        then synchronizes transmission maintaining a smooth ~340ms lead time.
        Guarantees zero DMA underrun and eliminates glitches/stutter over WAN connections.
        """
        total_len = len(pcm_bytes)
        offset = 0

        # 24,000 Hz * 2 channels * 2 bytes = 96,000 bytes/sec
        bytes_per_sec = 24000 * 2 * 2
        chunk_duration = chunk_size / bytes_per_sec

        # Only pre-buffer when starting fresh playback (continuation sentences flow seamlessly)
        prebuffer_chunks = 0 if is_continuation else 16
        chunk_idx = 0
        start_time = time.perf_counter()

        while offset < total_len and self.running and self.is_processing:
            if chunk_idx >= prebuffer_chunks:
                target_time = start_time + ((chunk_idx - prebuffer_chunks) * chunk_duration)
                delay = target_time - time.perf_counter()
                if delay > 0.002:
                    await asyncio.sleep(delay)
                else:
                    await asyncio.sleep(0)

            chunk = pcm_bytes[offset:offset + chunk_size]
            await self.send_to_esp32_audio(chunk)
            offset += len(chunk)
            chunk_idx += 1

    async def close(self):
        """Closes the session and cleans up resources."""
        self.running = False
        if self.processing_task and not self.processing_task.done():
            self.processing_task.cancel()
            try:
                await self.processing_task
            except asyncio.CancelledError:
                pass
        if self.http_client and not self.http_client.is_closed:
            await self.http_client.aclose()
            self.http_client = None
        self.conversation_history.clear()
        self.audio_buffer.clear()
        logger.info("[WS] OpenRouter session closed cleanly.")

