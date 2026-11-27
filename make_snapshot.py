"""배포 앱용 스냅샷 DB 만들기 (로컬에서만 실행).

현재 수집 기준(config.CRITERIA)의 가장 최근 성공 search 실행 1회분만 새 DB에 복사한다.
- 포함: 그 실행(+ 같은 기준의 이후 refresh)·영상·영상 스냅샷·채널·채널 스냅샷, 현재 프롬프트 버전의 포맷 최종 결과, 댓글 수집 기록·감성 분류
- 제외: 다른 수집 기준(v1·v2)·다른 실행, 이전 프롬프트 버전, 포맷 회차별 원본(대시보드가 읽지 않음)
- 댓글 본문은 빈 문자열로 저장 (대시보드는 집계만 쓰므로 원문을 스냅샷에 남기지 않음)
결과: data/snapshot/shorts_snapshot.db + snapshot.json (해시·수집 시각·개수). data/는 git 제외.

사용법: venv\\Scripts\\python.exe make_snapshot.py
"""
import hashlib
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import config
import db

OUT_DIR = Path(__file__).parent / "data" / "snapshot"
SNAPSHOT_FILE = "shorts_snapshot.db"
MANIFEST_FILE = "snapshot.json"


def _columns(conn, table: str) -> str:
    return ", ".join(r[1] for r in conn.execute(f"PRAGMA main.table_info({table})"))


def build(src: Path, out: Path, criteria: str | None = None) -> dict:
    """src DB에서 criteria의 최신 성공 search 실행만 out DB로 복사. 반환: manifest dict(해시 제외)."""
    criteria = criteria or config.CRITERIA
    if out.exists():
        out.unlink()
    conn = db.connect(out)                     # 같은 스키마로 빈 DB 생성
    try:
        conn.execute("ATTACH DATABASE ? AS src", (str(src),))   # src는 SELECT만 함 (쓰지 않음)
        row = conn.execute("SELECT run_id, started_at FROM src.runs WHERE criteria = ? AND mode = 'search' "
                           "AND status = 'ok' ORDER BY run_id DESC LIMIT 1", (criteria,)).fetchone()
        if not row:
            raise RuntimeError(f"수집 기준 {criteria}의 성공한 search 실행이 없음")
        run_id, started_at = row
        vids = "(SELECT video_id FROM src.run_keyword_videos WHERE run_id = :run)"
        chans = f"(SELECT channel_id FROM src.videos WHERE video_id IN {vids})"
        coms = f"(SELECT comment_key FROM src.video_comments WHERE video_id IN {vids})"
        # 같은 기준으로 이 search 이후에 한 실행(refresh)의 스냅샷도 포함 — 대시보드는 영상별 가장 최근 스냅샷을 씀
        runs = "(SELECT run_id FROM src.runs WHERE criteria = :crit AND run_id >= :run AND status = 'ok')"
        p = {"run": run_id, "crit": criteria, "fpv": config.FORMAT_PROMPT_VERSION, "cpv": config.COMMENT_PROMPT_VERSION}
        # 외래 키 순서대로: 실행 → 채널 → 영상 → 스냅샷·연결 → 분류·댓글
        steps = [
            ("runs", f"run_id IN {runs}"),
            ("channels", f"channel_id IN {chans}"),
            ("videos", f"video_id IN {vids}"),
            ("channel_snapshots", f"run_id IN {runs} AND channel_id IN {chans}"),
            ("video_snapshots", f"run_id IN {runs} AND video_id IN {vids}"),
            ("keyword_videos", f"video_id IN {vids} AND keyword IN (SELECT DISTINCT keyword FROM src.run_keyword_videos WHERE run_id = :run)"),
            ("run_keyword_videos", "run_id = :run"),
            ("collection_logs", f"run_id IN {runs}"),
            ("video_formats", f"prompt_version = :fpv AND video_id IN {vids}"),
            ("comment_fetches", f"video_id IN {vids}"),
            ("video_comments", f"video_id IN {vids}"),
            ("comment_reactions", f"prompt_version = :cpv AND comment_key IN {coms}"),
        ]
        counts = {}
        for table, where in steps:
            cols = _columns(conn, table)
            select = cols
            if table == "video_comments":       # 댓글 본문은 남기지 않음
                select = ", ".join("'' AS text" if c.strip() == "text" else c for c in cols.split(","))
            cur = conn.execute(f"INSERT INTO main.{table} ({cols}) SELECT {select} FROM src.{table} WHERE {where}", p)
            counts[table] = cur.rowcount
        conn.commit()
        conn.execute("DETACH DATABASE src")
        conn.execute("VACUUM")
    finally:
        conn.close()
    return {
        "criteria": criteria, "run_id": run_id, "collected_at": started_at,
        "keywords": config.KEYWORDS, "format_prompt_version": config.FORMAT_PROMPT_VERSION,
        "comment_prompt_version": config.COMMENT_PROMPT_VERSION, "comment_text_included": False,
        "counts": counts, "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def sha256(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUT_DIR / SNAPSHOT_FILE
    manifest = build(config.DB_PATH, out)
    manifest.update(file=SNAPSHOT_FILE, bytes=out.stat().st_size, sha256=sha256(out))
    (OUT_DIR / MANIFEST_FILE).write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"스냅샷: {out} ({manifest['bytes']:,} bytes)")
    print(f"수집 기준 {manifest['criteria']} · 실행 {manifest['run_id']} · 수집 시각 {manifest['collected_at']}")
    for table, n in manifest["counts"].items():
        print(f"  {table:<20} {n:>6,}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
