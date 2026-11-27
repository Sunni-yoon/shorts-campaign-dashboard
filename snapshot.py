"""배포 앱의 스냅샷 모드: 비공개 저장소의 스냅샷 DB를 받아 와 보여 주기만 한다 (수집·분류 안 함).

- 켜기: SNAPSHOT_MODE=1 (환경변수 또는 Streamlit Secrets). 로컬 기본값은 꺼짐 → 지금처럼 수집
- 저장소·읽기 키: DATA_REPO ("owner/name"), DATA_TOKEN (keys.get_api_key로 읽음, 출력하지 않음)
- 로컬 DB가 없거나 현재 수집 기준의 성공한 수집이 없을 때만 받음. snapshot.json의 SHA-256으로 검증 후 교체
- 보관 기간: 스냅샷 수집 시각이 config.RETENTION_DAYS일을 넘으면 표시하지 않음 (dashboard가 판단)
"""
import hashlib
import json
import os
import re
import sqlite3
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import config
import keys

MODE_NAME, REPO_NAME, TOKEN_NAME = "SNAPSHOT_MODE", "DATA_REPO", "DATA_TOKEN"
SNAPSHOT_FILE, MANIFEST_FILE = "shorts_snapshot.db", "snapshot.json"
REPO_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
EXPIRED_NOTICE = f"데이터 보관 기간({config.RETENTION_DAYS}일)이 지나 표시하지 않음"


def is_snapshot_mode(getter=keys.get_api_key) -> bool:
    return str(getter(MODE_NAME) or "").strip().lower() in ("1", "true", "yes", "on")


def has_data(db_path: Path, criteria: str | None = None) -> bool:
    """로컬 DB에 현재 수집 기준의 성공한 search 실행이 있는지 (파일이 없거나 깨졌으면 False)."""
    path = Path(db_path)
    if not path.exists() or path.stat().st_size == 0:
        return False
    try:
        with sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True) as conn:
            row = conn.execute("SELECT 1 FROM runs WHERE criteria = ? AND mode = 'search' AND status = 'ok' LIMIT 1",
                               (criteria or config.CRITERIA,)).fetchone()
        return row is not None
    except sqlite3.Error:
        return False


def collected_at(db_path: Path, criteria: str | None = None) -> datetime | None:
    """DB에 들어 있는 현재 수집 기준 최신 성공 search 실행의 수집 시작 시각 (UTC)."""
    try:
        with sqlite3.connect(f"file:{Path(db_path).as_posix()}?mode=ro", uri=True) as conn:
            row = conn.execute("SELECT MAX(started_at) FROM runs WHERE criteria = ? AND mode = 'search' AND status = 'ok'",
                               (criteria or config.CRITERIA,)).fetchone()
    except sqlite3.Error:
        return None
    if not row or not row[0]:
        return None
    return datetime.fromisoformat(row[0].replace("Z", "+00:00"))


def is_expired(collected: datetime | None, now: datetime | None = None,
               days: int = config.RETENTION_DAYS) -> bool:
    """수집 후 days일이 지났으면 True (정확히 days일이면 지난 것으로 봄). 수집 시각을 모르면 True."""
    if collected is None:
        return True
    now = now or datetime.now(timezone.utc)
    return now - collected >= timedelta(days=days)


def github_fetcher(repo: str, token: str, path: str, timeout: int = 60) -> bytes:
    """비공개 저장소 파일 1개를 GitHub Contents API(raw)로 받음. 토큰은 헤더에만 씀."""
    url = f"https://api.github.com/repos/{repo}/contents/{path}"
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {token}", "Accept": "application/vnd.github.raw",
        "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "shorts-dashboard-snapshot"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def ensure_snapshot(db_path: Path, getter=keys.get_api_key, fetcher=github_fetcher) -> dict:
    """로컬 데이터가 없으면 스냅샷을 받아 db_path에 둠. 예외를 던지지 않음 (오류 메시지는 토큰을 가림).

    반환 action: none(이미 있음) | downloaded | no_key | bad_repo | failed
    """
    if has_data(db_path):
        return {"action": "none", "error": None}
    repo, token = (getter(REPO_NAME) or "").strip(), getter(TOKEN_NAME) or ""
    if not repo or not token:
        return {"action": "no_key", "error": None}
    if not REPO_PATTERN.match(repo):
        return {"action": "bad_repo", "error": None}
    path = Path(db_path)
    tmp = path.with_name(path.name + ".download")
    try:
        manifest = json.loads(fetcher(repo, token, MANIFEST_FILE).decode("utf-8"))
        data = fetcher(repo, token, manifest.get("file") or SNAPSHOT_FILE)
        if hashlib.sha256(data).hexdigest() != manifest.get("sha256"):
            raise ValueError("스냅샷 파일 해시가 snapshot.json과 다름 (손상되었거나 다른 파일)")
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_bytes(data)
        if not has_data(tmp, manifest.get("criteria")):
            raise ValueError("받은 스냅샷에 현재 수집 기준의 데이터가 없음")
        os.replace(tmp, path)                  # 다 받은 뒤 한 번에 교체 (중간에 끊겨도 깨진 DB가 남지 않음)
        return {"action": "downloaded", "error": None, "manifest": manifest}
    except Exception as e:                     # 네트워크·권한·해시 오류 등
        if tmp.exists():
            tmp.unlink()
        return {"action": "failed", "error": keys.mask(f"{type(e).__name__}: {e}", token)}


def explain_error(error: str | None) -> str:
    if not error:
        return ""
    if "HTTP Error 401" in error or "HTTP Error 403" in error:
        hint = "데이터 저장소 읽기 권한이 없음. DATA_TOKEN(읽기 권한)과 만료 여부 확인 필요."
    elif "HTTP Error 404" in error:
        hint = "데이터 저장소나 스냅샷 파일을 찾지 못함. DATA_REPO 이름과 토큰의 저장소 권한 확인 필요."
    elif "해시" in error or "데이터가 없음" in error:
        hint = "받은 스냅샷이 올바르지 않음."
    elif "URLError" in error or "timed out" in error or "Timeout" in error:
        hint = "데이터 저장소에 연결하지 못함(네트워크 오류). 잠시 후 다시 열어 볼 것."
    else:
        hint = "스냅샷을 받지 못함."
    return f"{hint} (원문: {error})"
