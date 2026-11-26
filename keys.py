"""YouTube API 키 읽기. 키 값은 반환만 하고 절대 출력·로그하지 않는다.

순서: 1) 환경변수 YOUTUBE_API_KEY (로컬은 .env를 불러와 채움)
      2) Streamlit Secrets의 YOUTUBE_API_KEY (Streamlit Community Cloud 배포)
"""
import os
from pathlib import Path

from dotenv import load_dotenv

KEY_NAME = "YOUTUBE_API_KEY"
DOTENV_PATH = Path(__file__).with_name(".env")


def get_api_key(dotenv_path: Path | None = DOTENV_PATH) -> str | None:
    if dotenv_path is not None and Path(dotenv_path).exists():
        load_dotenv(dotenv_path)              # 이미 설정된 환경변수는 덮어쓰지 않음
    key = os.environ.get(KEY_NAME, "").strip()
    if key:
        return key
    try:
        import streamlit as st
        key = str(st.secrets.get(KEY_NAME, "")).strip()
    except Exception:                         # secrets 파일이 없거나 Streamlit 밖에서 실행
        key = ""
    return key or None


def mask(text, key: str | None) -> str:
    """문자열에서 키 값을 *** 로 가림."""
    text = str(text)
    return text.replace(key, "***") if key else text
