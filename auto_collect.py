"""대시보드를 열 때 데이터 신선도를 확인하고, 필요하면 v2 기준으로 새로 검색한다.

- 수집은 누군가 앱을 열었을 때만 일어난다 (백그라운드 타이머 없음)
- 마지막 성공 search 후 config.REFRESH_HOURS(24시간)가 지났거나 데이터가 없으면 수집
- 실패하면 config.RETRY_AFTER_FAILURE_MINUTES(60분) 동안 다시 시도하지 않음 (할당량 보호)
- 열 때마다 보관 기간(30일)이 지난 데이터를 삭제
- 여러 방문자가 동시에 열어도 잠금으로 수집은 한 번만 실행
Streamlit에 의존하지 않음 (대시보드가 잠금 상태 객체와 진행 표시 함수를 넘겨줌).
"""
import sqlite3
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import collect
import config
import db
import keys


@dataclass
class CollectorState:
    """프로세스 안에서 공유하는 수집 상태 (Streamlit에서는 st.cache_resource로 1개만 생성)."""
    lock: threading.Lock = field(default_factory=threading.Lock)
    last_failure_at: datetime | None = None
    last_error: str | None = None


def _parse(ts: str | None) -> datetime | None:
    if not ts:
        return None
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def needs_collection(last_success: str | None, now: datetime,
                     refresh_hours: float = config.REFRESH_HOURS) -> bool:
    last = _parse(last_success)
    return last is None or now - last >= timedelta(hours=refresh_hours)


def in_retry_wait(state: CollectorState, now: datetime,
                  minutes: float = config.RETRY_AFTER_FAILURE_MINUTES) -> bool:
    return state.last_failure_at is not None and now - state.last_failure_at < timedelta(minutes=minutes)


def check(db_path: Path, now: datetime | None = None) -> tuple[bool, str | None]:
    """(수집이 필요한지, 마지막 성공 search 시각). 보관 기간이 지난 데이터도 여기서 삭제."""
    now = now or datetime.now(timezone.utc)
    conn = db.connect(db_path)
    try:
        try:
            collect.purge_expired(conn, now)
        except sqlite3.OperationalError:     # 다른 세션이 수집 중이라 잠겨 있으면 다음 방문 때 삭제
            pass
        last = db.last_successful_search(conn, config.CRITERIA)
    finally:
        conn.close()
    return needs_collection(last, now), last


def ensure_fresh_data(state: CollectorState, db_path: Path, progress=None, now=None,
                      api_key_getter=keys.get_api_key, collector=collect.run_collection) -> dict:
    """필요하면 수집. 반환: action = none | collected | failed | retry_wait | no_key | disabled."""
    now = now or datetime.now(timezone.utc)
    needed, last = check(db_path, now)
    if not needed:
        return {"action": "none", "last_success": last, "error": None}
    if not config.AUTO_COLLECT:
        return {"action": "disabled", "last_success": last, "error": None}
    api_key = api_key_getter()
    if not api_key:
        return {"action": "no_key", "last_success": last, "error": None}

    with state.lock:
        needed, last = check(db_path, now)       # 기다리는 동안 다른 방문자가 수집했을 수 있음
        if not needed:
            return {"action": "none", "last_success": last, "error": None}
        if in_retry_wait(state, now):
            return {"action": "retry_wait", "last_success": last, "error": state.last_error,
                    "retry_at": state.last_failure_at + timedelta(minutes=config.RETRY_AFTER_FAILURE_MINUTES)}
        result = collector("search", api_key, db_path=db_path, progress=progress)
        if result["status"] == "ok":
            state.last_failure_at, state.last_error = None, None
            return {"action": "collected", "last_success": result["run_id"], "error": None, "result": result}
        state.last_failure_at = now
        state.last_error = keys.mask(result["error"] or result["status"], api_key)
        _, last = check(db_path, now)
        return {"action": "failed", "last_success": last, "error": state.last_error, "result": result}
