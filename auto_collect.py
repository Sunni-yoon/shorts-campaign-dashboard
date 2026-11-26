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

import classify_comments
import classify_formats
import collect
import collect_comments
import config
import db
import keys


@dataclass
class CollectorState:
    """프로세스 안에서 공유하는 수집 상태 (Streamlit에서는 st.cache_resource로 1개만 생성)."""
    lock: threading.Lock = field(default_factory=threading.Lock)
    last_failure_at: datetime | None = None
    last_error: str | None = None
    format_failure_at: datetime | None = None      # 포맷 분류 실패 (재시도 대기용, 수집과 별도)
    format_error: str | None = None
    comment_failure_at: datetime | None = None     # 댓글 수집 실패
    comment_error: str | None = None
    reaction_failure_at: datetime | None = None    # 댓글 분류 실패
    reaction_error: str | None = None


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


# ---------------------------------------------------------------- 포맷 분류 (새 영상만)

def pending_format_count(db_path: Path) -> int:
    conn = db.connect(db_path)
    try:
        return len(classify_formats.pending_videos(conn))
    finally:
        conn.close()


def ensure_formats(state: CollectorState, db_path: Path, progress=None, now=None,
                   api_key_getter=lambda: keys.get_api_key(keys.OPENAI_KEY_NAME), classifier=None) -> dict:
    """최신 v2 실행의 미분류 쇼츠만 분류. 반환: action = none | classified | failed | retry_wait | no_key | disabled."""
    now = now or datetime.now(timezone.utc)
    if pending_format_count(db_path) == 0:
        return {"action": "none", "error": None}
    if not config.AUTO_COLLECT:
        return {"action": "disabled", "error": None}
    api_key = api_key_getter()
    if not api_key:
        return {"action": "no_key", "error": None}
    with state.lock:
        if state.format_failure_at and now - state.format_failure_at < timedelta(
                minutes=config.RETRY_AFTER_FAILURE_MINUTES):
            return {"action": "retry_wait", "error": state.format_error,
                    "retry_at": state.format_failure_at + timedelta(minutes=config.RETRY_AFTER_FAILURE_MINUTES)}
        conn = db.connect(db_path)
        try:
            result = classify_formats.classify_pending(conn, api_key, classifier=classifier, progress=progress)
        finally:
            conn.close()
        if result["failed"] and not result["classified"] or result["stopped"]:
            state.format_failure_at = now
            state.format_error = keys.mask("; ".join(result["errors"]) or "분류 실패", api_key)
            return {"action": "failed", "error": state.format_error, "result": result}
        state.format_failure_at, state.format_error = None, None
        return {"action": "classified", "error": None, "result": result}


# ---------------------------------------------------------------- 댓글 수집 + 반응 분류 (새 영상·새 댓글만)

def pending_reaction_work(db_path: Path) -> tuple[int, int]:
    """(댓글을 수집할 영상 수, 분류할 댓글 수)."""
    conn = db.connect(db_path)
    try:
        return len(collect_comments.pending_videos(conn)), len(classify_comments.pending_comments(conn))
    finally:
        conn.close()


def ensure_reactions(state: CollectorState, db_path: Path, progress=None, now=None,
                     youtube_key_getter=lambda: keys.get_api_key(keys.KEY_NAME),
                     openai_key_getter=lambda: keys.get_api_key(keys.OPENAI_KEY_NAME),
                     fetcher=None, classifier=None) -> dict:
    """새 영상의 댓글을 수집하고 새 댓글을 분류. 단계별 결과를 돌려줌 (오류 메시지는 키가 가려짐)."""
    now = now or datetime.now(timezone.utc)
    out = {"comments": {"action": "none"}, "reactions": {"action": "none"}}
    if not config.AUTO_COLLECT:
        return {"comments": {"action": "disabled"}, "reactions": {"action": "disabled"}}
    wait = timedelta(minutes=config.RETRY_AFTER_FAILURE_MINUTES)
    with state.lock:
        n_videos, _ = pending_reaction_work(db_path)
        if n_videos:
            yt_key = youtube_key_getter()
            if not yt_key:
                out["comments"] = {"action": "no_key"}
            elif state.comment_failure_at and now - state.comment_failure_at < wait:
                out["comments"] = {"action": "retry_wait", "error": state.comment_error,
                                   "retry_at": state.comment_failure_at + wait}
            else:
                conn = db.connect(db_path)
                try:
                    r = collect_comments.collect_pending(
                        conn, fetcher or collect_comments.youtube_fetcher(yt_key), progress, yt_key)
                finally:
                    conn.close()
                if r["stopped"] or (r["error"] and not r["ok"]):
                    state.comment_failure_at, state.comment_error = now, "; ".join(r["errors"]) or "댓글 수집 실패"
                    out["comments"] = {"action": "failed", "error": state.comment_error, "result": r}
                else:
                    state.comment_failure_at, state.comment_error = None, None
                    out["comments"] = {"action": "collected", "result": r}

        _, n_comments = pending_reaction_work(db_path)
        if n_comments:
            ai_key = openai_key_getter()
            if not ai_key:
                out["reactions"] = {"action": "no_key"}
            elif state.reaction_failure_at and now - state.reaction_failure_at < wait:
                out["reactions"] = {"action": "retry_wait", "error": state.reaction_error,
                                    "retry_at": state.reaction_failure_at + wait}
            else:
                conn = db.connect(db_path)
                try:
                    r = classify_comments.classify_pending(conn, ai_key, classifier=classifier, progress=progress)
                finally:
                    conn.close()
                if r["stopped"] or (r["failed"] and not r["classified"]):
                    state.reaction_failure_at = now
                    state.reaction_error = keys.mask("; ".join(r["errors"]) or "댓글 분류 실패", ai_key)
                    out["reactions"] = {"action": "failed", "error": state.reaction_error, "result": r}
                else:
                    state.reaction_failure_at, state.reaction_error = None, None
                    out["reactions"] = {"action": "classified", "result": r}
    return out
