import os
import io
import re
import time
import asyncio
import threading
import subprocess
from typing import Optional

# Suppress pygame welcome message
os.environ["PYGAME_HIDE_SUPPORT_PROMPT"] = "1"
import pygame

try:
    import edge_tts
    _EDGE_TTS_AVAILABLE = True
except ImportError:
    _EDGE_TTS_AVAILABLE = False


class NyotaTTSEngine:
    """
    Nyota Natural & Adaptive Voice Engine:
    - Primary (Natural Neural): Microsoft Edge Natural TTS (Jenny / Rehema) via edge-tts.
      Studio quality, zero CPU footprint, native Swahili + English phonemes.
    - Fallback (Fast / Offline): Native Windows SAPI5 SpeechSynthesizer.
      Sub-second offline response with zero dependencies.
    """

    VOICE_EN = "en-US-JennyNeural"
    VOICE_SW = "sw-TZ-RehemaNeural"

    def __init__(self):
        self.current_mode: str = "natural"  # "natural" or "fast"
        self._is_speaking: bool = False
        self._interrupted: bool = False
        self._lock = threading.Lock()
        self._mixer_initialized: bool = False
        self._init_mixer()

    def _init_mixer(self):
        try:
            if pygame.mixer.get_init():
                pygame.mixer.quit()
            pygame.mixer.init(frequency=44100, size=-16, channels=2, buffer=2048)
            pygame.mixer.music.set_volume(1.0)
            self._mixer_initialized = True
        except Exception as e:
            print(f"[TTS]: Failed to initialize pygame mixer: {e}")
            self._mixer_initialized = False

    def is_speaking(self) -> bool:
        with self._lock:
            return self._is_speaking

    def set_mode(self, mode: str) -> str:
        mode = mode.lower().strip()
        if mode in ["natural", "premium", "neural"]:
            self.current_mode = "natural"
            return "Voice mode switched to Natural Neural voice."
        elif mode in ["fast", "standard", "sapi5"]:
            self.current_mode = "fast"
            return "Voice mode switched to Fast Windows SAPI5 voice."
        return "Invalid mode. Choose 'natural' or 'fast'."

    def get_mode(self) -> str:
        return self.current_mode

    def interrupt(self):
        """Immediately stop any active speech."""
        with self._lock:
            self._interrupted = True
            self._is_speaking = False
        try:
            if self._mixer_initialized and pygame.mixer.get_init():
                pygame.mixer.music.stop()
        except Exception:
            pass

    def speak(self, text: str, language: str = "en"):
        """Speak given text using selected mode with automatic offline fallback."""
        if not text or not text.strip():
            return

        with self._lock:
            self._interrupted = False
            self._is_speaking = True

        try:
            clean_text, target_voice = self._prepare_text(text, language)
            if not clean_text:
                return

            if self.current_mode == "natural" and _EDGE_TTS_AVAILABLE:
                success = self._speak_natural(clean_text, target_voice)
                if not success and not self._interrupted:
                    print("[TTS]: Natural voice unavailable (offline), falling back to SAPI5.")
                    self._speak_sapi5(clean_text)
            else:
                self._speak_sapi5(clean_text)
        except Exception as e:
            print(f"[TTS]: Error during speak(): {e}")
        finally:
            with self._lock:
                self._is_speaking = False

    def _prepare_text(self, raw_text: str, language: str) -> tuple[str, str]:
        """Sanitize text and choose appropriate voice based on language and split."""
        text = raw_text.strip()
        voice = self.VOICE_EN

        # Handle pipe separator: [Swahili text] | [English translation]
        if "|" in text:
            parts = text.split("|")
            sw_part = parts[0].strip()
            en_part = parts[1].strip()
            if language == "sw" and sw_part:
                text = sw_part
                voice = self.VOICE_SW
            else:
                text = en_part or sw_part
                voice = self.VOICE_EN
        else:
            if language == "sw":
                voice = self.VOICE_SW
            else:
                voice = self.VOICE_EN

        # 1. Strip markdown fences and code blocks
        if "```" in text:
            text = re.sub(r'```[\w]*[\r\n]+[\s\S]*?```', '', text).strip()
            text = re.sub(r'```[\w]*[\r\n][\s\S]*$', '', text).strip()
            if not text:
                text = "I have placed the full code in your console."

        # 2. Strip file paths and symbols
        text = re.sub(r'[A-Za-z]:\\[^\n\r.]+\.[a-zA-Z0-9]+', '', text)
        text = text.replace('`', '').replace('*', '').replace('#', '')
        text = re.sub(r'^\s*[-•]\s*', '', text, flags=re.MULTILINE)
        text = re.sub(r'\n+', ' ', text)
        text = re.sub(r'\s+', ' ', text).strip()

        # Allow generous full responses up to 2500 characters (cuts only at sentence boundary if exceeded)
        if len(text) > 2500:
            last_period = text[:2500].rfind('.')
            if last_period > 1200:
                text = text[:last_period + 1]
            else:
                text = text[:2500] + "..."

        return text, voice

    def _speak_natural(self, text: str, voice: str) -> bool:
        """Synthesize via Edge Neural TTS and play in-memory without saving to disk."""
        try:
            # Run async edge_tts in a localized event loop
            audio_data = self._generate_edge_audio(text, voice)
            if not audio_data or self._interrupted:
                return False

            if not self._mixer_initialized:
                self._init_mixer()

            audio_stream = io.BytesIO(audio_data)
            pygame.mixer.music.load(audio_stream)
            pygame.mixer.music.set_volume(1.0)
            print(f"[TTS]: [VOICE] Speaking with Natural Neural voice ({voice}): '{text[:60]}'...")
            pygame.mixer.music.play()

            while pygame.mixer.music.get_busy() and not self._interrupted:
                time.sleep(0.05)

            if self._interrupted:
                pygame.mixer.music.stop()

            return True
        except Exception as e:
            print(f"[TTS]: Edge-TTS generation/playback error: {e}")
            return False

    def _generate_edge_audio(self, text: str, voice: str) -> Optional[bytes]:
        """Fetch neural audio from edge-tts with dynamic timeout and fast fallback."""
        async def _fetch(target_voice: str):
            communicate = edge_tts.Communicate(text, target_voice)
            buf = bytearray()
            async for chunk in communicate.stream():
                if self._interrupted:
                    break
                if chunk["type"] == "audio":
                    buf.extend(chunk["data"])
            return bytes(buf)

        # Dynamic timeout: scales with text length (e.g. 7s for short, 20s for long explanations)
        calc_timeout = max(7.0, min(25.0, len(text) / 70.0 + 5.0))

        try:
            loop = asyncio.new_event_loop()
            try:
                asyncio.set_event_loop(loop)
                try:
                    res = loop.run_until_complete(asyncio.wait_for(_fetch(voice), timeout=calc_timeout))
                    if res:
                        return res
                except (asyncio.TimeoutError, Exception) as primary_err:
                    if voice != self.VOICE_EN:
                        print(f"[TTS]: {voice} slow or unavailable ({primary_err}), using {self.VOICE_EN}")
                        return loop.run_until_complete(asyncio.wait_for(_fetch(self.VOICE_EN), timeout=calc_timeout))
                    return None
            finally:
                loop.close()
        except Exception as err:
            print(f"[TTS]: Async fetch failed: {err}")
            return None

    def _speak_sapi5(self, text: str):
        """Offline fallback using Windows native SAPI.SpVoice via COM."""
        if self._interrupted:
            return
        try:
            print(f"[TTS]: [VOICE] Speaking with Fast SAPI5 (COM): '{text[:60]}'...")
            import pythoncom
            import win32com.client
            pythoncom.CoInitialize()
            try:
                speaker = win32com.client.Dispatch("SAPI.SpVoice")
                speaker.Speak(text)
            finally:
                pythoncom.CoUninitialize()
        except Exception as e:
            print(f"[TTS]: win32com SAPI5 error ({e}), trying PowerShell fallback...")
            try:
                escaped = text.replace("'", "''").replace('\n', ' ').replace('\r', ' ')
                ps_script = f"Add-Type -AssemblyName System.Speech; (New-Object System.Speech.Synthesis.SpeechSynthesizer).Speak('{escaped}')"
                subprocess.run(['powershell', '-NoProfile', '-Command', ps_script], capture_output=True, timeout=15)
            except Exception as ps_err:
                print(f"[TTS]: SAPI5 fallback completely failed: {ps_err}")


# Global Singleton Instance
nyota_tts = NyotaTTSEngine()


def speak_async(text: str, language: str = "en"):
    """Threaded helper called by routes to initiate speech without blocking."""
    threading.Thread(
        target=nyota_tts.speak,
        args=(text, language),
        daemon=True
    ).start()
