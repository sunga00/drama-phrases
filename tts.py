"""
edge-tts 기반 TTS 모듈.
캐시: audio_cache/{lang}/{md5(text)}.mp3 — 동일 텍스트는 재생성 안 함.
"""

import asyncio
import hashlib
from pathlib import Path

import edge_tts

CACHE_DIR = Path("audio_cache")


def _cache_path(lang: str, text: str) -> Path:
    key = hashlib.md5(text.encode("utf-8")).hexdigest()
    return CACHE_DIR / lang / f"{key}.mp3"


async def _save_mp3(text: str, voice: str, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    communicate = edge_tts.Communicate(text, voice)
    await communicate.save(str(path))


def get_audio_bytes(text: str, lang: str, voice: str) -> bytes:
    """
    텍스트를 mp3로 변환해 bytes 반환.
    캐시 파일이 있으면 즉시 반환, 없으면 edge-tts로 생성.
    Streamlit 호환: 독립 이벤트 루프에서 실행.
    """
    path = _cache_path(lang, text)
    if not path.exists():
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(_save_mp3(text, voice, path))
        finally:
            loop.close()
    return path.read_bytes()
