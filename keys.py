"""API 키 읽기 (YouTube·OpenAI 공용). 키 값은 반환만 하고 절대 출력·로그하지 않는다.

순서: 1) 환경변수 (로컬은 .env를 불러와 채움)
      2) Streamlit Secrets (Streamlit Community Cloud 배포)
"""
import os
import re
from pathlib import Path

from dotenv import load_dotenv

KEY_NAME = "YOUTUBE_API_KEY"
OPENAI_KEY_NAME = "OPENAI_API_KEY"
DOTENV_PATH = Path(__file__).with_name(".env")


def get_api_key(name: str = KEY_NAME, dotenv_path: Path | None = DOTENV_PATH) -> str | None:
    if dotenv_path is not None and Path(dotenv_path).exists():
        load_dotenv(dotenv_path)              # 이미 설정된 환경변수는 덮어쓰지 않음
    key = os.environ.get(name, "").strip()
    if key:
        return key
    try:
        import streamlit as st
        key = str(st.secrets.get(name, "")).strip()
    except Exception:                         # secrets 파일이 없거나 Streamlit 밖에서 실행
        key = ""
    return key or None


# API가 오류 메시지에 되돌려 주는 키 조각(예: "Incorrect API key provided: sk-ab****cd")까지 가림
_KEY_FRAGMENT_PATTERNS = [
    re.compile(r"(Incorrect API key provided:?\s*)\S+", re.I),
    re.compile(r"\bsk-[A-Za-z0-9_\-\*]{4,}"),
    re.compile(r"\bkey_[A-Za-z0-9_\-\*]{4,}"),
    re.compile(r"AIza[0-9A-Za-z_\-\*]{6,}"),
    re.compile(r"([?&]key=)[^&\s\"']+"),
]


def mask(text, *keys: str | None) -> str:
    """문자열에서 키 값과 키 조각을 *** 로 가림 (여러 키 가능)."""
    text = str(text)
    for key in keys:
        if key:
            text = text.replace(key, "***")
    for pattern in _KEY_FRAGMENT_PATTERNS:
        text = pattern.sub(lambda m: (m.group(1) if m.groups() else "") + "***", text)
    return text
