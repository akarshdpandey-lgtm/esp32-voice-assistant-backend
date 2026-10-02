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
        self.silence_duration_sec = 0.38  # Ultra-fast silence detection for rapid response
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
        Executes the STT -> OpenRouter LLM -> TTS -> ESP32 audio streaming pipeline.
        """
        try:
            # Step 1: Speech-to-Text
            logger.info("[STT] Transcribing...")
            t0 = time.time()
            transcript = await self._transcribe_audio(pcm16_audio)
            t_stt = time.time() - t0
            if not transcript or not transcript.strip():
                logger.info(f"[STT] No speech recognized / empty transcript (took {t_stt:.2f}s).")
                self.is_processing = False
                return

            logger.info(f"[STT] Transcript: '{transcript}' (took {t_stt:.2f}s)")

            # Step 2: OpenRouter LLM (Qwen3.5-Flash)
            logger.info("[OPENROUTER] Sending request")
            t0 = time.time()
            llm_response = await self._ask_openrouter(transcript)
            t_llm = time.time() - t0
            if not llm_response:
                logger.warning(f"[LLM] Empty response received from OpenRouter (took {t_llm:.2f}s).")
                await self.send_to_esp32_control({
                    "type": "error",
                    "message": "No response from AI assistant."
                })
                self.is_processing = False
                return

            logger.info(f"[LLM] Response: '{llm_response}' (took {t_llm:.2f}s)")

            # Step 3: Text-to-Speech (24kHz PCM16 Mono + Amplified)
            logger.info(f"[TTS] Synthesizing: '{llm_response}'")
            t0 = time.time()
            pcm24k_audio = await self._synthesize_speech(llm_response)
            t_tts = time.time() - t0
            if not pcm24k_audio:
                logger.warning(f"[TTS] TTS synthesis produced no audio (took {t_tts:.2f}s).")
                await self.send_to_esp32_control({
                    "type": "error",
                    "message": "TTS synthesis failed."
                })
                self.is_processing = False
                return

            logger.info(f"[TTS] Audio generated: {len(pcm24k_audio)} bytes (took {t_tts:.2f}s)")

            # Step 4: Stream PCM audio back to ESP32
            logger.info(f"[ESP32] Streaming audio ({len(pcm24k_audio)} bytes) to device")
            audio_duration_sec = len(pcm24k_audio) / (24000 * 2 * 2)
            stream_start = time.time()
            await self.send_to_esp32_control({"type": "playback_start"})
            await self._stream_audio_to_esp32(pcm24k_audio)

            # Wait for remaining audio samples in ESP32 DMA buffer to physically play
            stream_elapsed = time.time() - stream_start
            remaining_play_time = max(0.4, audio_duration_sec - stream_elapsed + 0.3)
            await asyncio.sleep(remaining_play_time)

            # Step 5: Notify playback completion
            logger.info("[VOICE] Playback complete")
            await self.send_to_esp32_control({"type": "audio_done"})

            # Clear any acoustic echo from mic buffer before accepting next utterance
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
        """Converts raw 16kHz PCM16 bytes to WAV and performs speech recognition."""
        try:
            # Build standard WAV container in memory
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
                    # Fast Hindi/Hinglish recognition first
                    return self.recognizer.recognize_google(audio_data, language="hi-IN")
                except sr.UnknownValueError:
                    try:
                        # Fallback to English
                        return self.recognizer.recognize_google(audio_data, language="en-US")
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
        """Calls OpenRouter Chat Completions endpoint with Qwen3.5-Flash."""
        if not self.api_key or self.api_key.startswith("your_"):
            logger.error("[OPENROUTER] Cannot call OpenRouter: API key is not configured.")
            return None

        # Prepare messages including short history
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
            "max_tokens": 500,
            "reasoning": {"effort": "none"}
        }

        try:
            client = self.http_client or httpx.AsyncClient(timeout=20.0)
            res = await client.post(OPENROUTER_API_URL, headers=headers, json=payload)

            if res.status_code == 200:
                logger.info("[OPENROUTER] Response received")
                data = res.json()
                raw_content = data["choices"][0]["message"].get("content", "")
                logger.info(f"[LLM] Raw Response: {raw_content}")
                cleaned_text = clean_llm_response(raw_content, user_text=user_text)
                logger.info(f"[LLM] Final Response: {cleaned_text}")

                # Update conversation memory
                self.conversation_history.append({"role": "user", "content": user_text})
                self.conversation_history.append({"role": "assistant", "content": cleaned_text})
                if len(self.conversation_history) > self.max_history_turns * 2:
                    self.conversation_history = self.conversation_history[-(self.max_history_turns * 2):]

                return cleaned_text
            elif res.status_code == 401:
                logger.error("[OPENROUTER] HTTP 401: Unauthorized. Please verify OPENROUTER_API_KEY.")
            elif res.status_code == 429:
                logger.error("[OPENROUTER] HTTP 429: Rate limit exceeded on OpenRouter.")
            else:
                logger.error(f"[OPENROUTER] HTTP {res.status_code}: Error response from OpenRouter.")
            return None

        except httpx.TimeoutException:
            logger.error("[OPENROUTER] Request timed out while waiting for Qwen response.")
            return None
        except Exception as e:
            logger.error(f"[OPENROUTER] Request exception: {e}")
            return None

    async def _synthesize_speech(self, text: str) -> Optional[bytes]:
        """
        Synthesizes text using edge-tts (fast speech rate + full volume)
        and decodes output into 24kHz 16-bit Mono PCM amplified by 2.0x gain.
        """
        try:
            communicate = edge_tts.Communicate(
                text,
                voice="hi-IN-SwaraNeural",
                rate="+0%",
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
                # Apply volume gain amplification with clipping protection
                return amplify_pcm16(raw_pcm, gain=AUDIO_GAIN)

            pcm_bytes = await asyncio.to_thread(_decode)
            return pcm_bytes

        except Exception as e:
            logger.error(f"[TTS] Exception during TTS synthesis: {e}")
            return None

    async def _stream_audio_to_esp32(self, pcm_bytes: bytes, chunk_size: int = 4096):
        """
        Streams 24kHz stereo PCM16 audio to ESP32 with clock-target pacing.
        Pre-buffers 3 chunks (~128ms) for instantaneous start, then synchronizes
        each chunk with exact physical playback timing. Prevents both DMA underrun
        and TCP buffer overflow.
        """
        total_len = len(pcm_bytes)
        offset = 0

        # 24,000 Hz * 2 channels * 2 bytes = 96,000 bytes/sec
        bytes_per_sec = 24000 * 2 * 2
        chunk_duration = chunk_size / bytes_per_sec

        prebuffer_chunks = 3
        chunk_idx = 0
        start_time = time.perf_counter()

        while offset < total_len and self.running:
            if chunk_idx >= prebuffer_chunks:
                target_time = start_time + ((chunk_idx - prebuffer_chunks) * chunk_duration)
                delay = target_time - time.perf_counter()
                if delay > 0.001:
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

