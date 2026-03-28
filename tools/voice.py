"""
Voice message transcription using OpenAI Whisper.

Supports 99 languages — auto-detects if no language hint given.
Telegram sends voice as OGG Opus, which Whisper accepts natively.
"""

import io
import logging

from config.settings import settings

log = logging.getLogger(__name__)

# Languages most common for US convenience store owners
SUPPORTED_LANGUAGES = {
    "english":   "en",
    "hindi":     "hi",
    "gujarati":  "gu",
    "punjabi":   "pa",
    "spanish":   "es",
    "arabic":    "ar",
    "urdu":      "ur",
    "bengali":   "bn",
    "chinese":   "zh",
    "korean":    "ko",
    "vietnamese":"vi",
    "portuguese":"pt",
    "french":    "fr",
    "auto":      None,   # Whisper auto-detects
}


def transcribe_voice(audio_bytes: bytes, language_code: str | None = None) -> str:
    """
    Transcribe a Telegram voice message (OGG Opus) using OpenAI Whisper.

    audio_bytes: raw bytes from Telegram file download
    language_code: ISO 639-1 code ('hi', 'gu', 'es', etc.) or None for auto-detect

    Returns the transcribed text string.
    Raises ValueError if OPENAI_API_KEY is not configured.
    """
    api_key = getattr(settings, "openai_api_key", None)
    if not api_key:
        raise ValueError(
            "OPENAI_API_KEY is not set. Add it to your .env file to enable voice messages."
        )

    from openai import OpenAI
    client = OpenAI(api_key=api_key)

    audio_file = io.BytesIO(audio_bytes)
    audio_file.name = "voice.ogg"

    kwargs: dict = {"model": "whisper-1", "file": audio_file}
    if language_code:
        kwargs["language"] = language_code

    transcript = client.audio.transcriptions.create(**kwargs)
    text = transcript.text.strip()
    log.info("Voice transcribed (%s): %s", language_code or "auto", text[:80])
    return text
