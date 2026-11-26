"""SQLite 스키마와 저장 함수."""
import json
import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS videos (
    video_id        TEXT PRIMARY KEY,
    title           TEXT,
    description     TEXT,
    hashtags        TEXT,     -- JSON 배열 (제목+설명에서 추출, 소문자)
    published_at    TEXT,
    duration_sec    INTEGER,
    channel_id      TEXT,
    embed_width     INTEGER,
    embed_height    INTEGER,
    has_shorts_tag  INTEGER,  -- #shorts 해시태그 여부 (보조 신호)
    is_short        INTEGER,  -- 1=쇼츠 판정, 0=제외
    first_seen_at   TEXT,
    updated_at      TEXT
);

CREATE TABLE IF NOT EXISTS video_snapshots (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id          TEXT NOT NULL,
    video_id        TEXT NOT NULL REFERENCES videos(video_id),
    collected_at    TEXT NOT NULL,
    view_count      INTEGER,  -- 비공개면 NULL
    like_count      INTEGER,  -- 좋아요 숨김이면 NULL
    comment_count   INTEGER,  -- 댓글 사용 중지면 NULL
    UNIQUE (run_id, video_id)
);

CREATE TABLE IF NOT EXISTS channels (
    channel_id      TEXT PRIMARY KEY,
    title           TEXT,
    first_seen_at   TEXT,
    updated_at      TEXT
);

CREATE TABLE IF NOT EXISTS channel_snapshots (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id              TEXT NOT NULL,
    channel_id          TEXT NOT NULL REFERENCES channels(channel_id),
    collected_at        TEXT NOT NULL,
    subscriber_count    INTEGER,  -- 비공개면 NULL
    subscriber_hidden   INTEGER,
    UNIQUE (run_id, channel_id)
);

CREATE TABLE IF NOT EXISTS keyword_videos (
    keyword         TEXT NOT NULL,
    video_id        TEXT NOT NULL REFERENCES videos(video_id),
    first_seen_at   TEXT,
    first_run_id    TEXT,
    last_run_id     TEXT,
    PRIMARY KEY (keyword, video_id)
);

CREATE TABLE IF NOT EXISTS runs (
    run_id          TEXT PRIMARY KEY,
    mode            TEXT,     -- search | refresh
    started_at      TEXT,
    finished_at     TEXT,
    quota_units     INTEGER,
    status          TEXT,     -- running | ok | quota_exceeded | error
    error_message   TEXT,
    criteria        TEXT,     -- 수집 기준 이름 (config.CRITERIA)
    criteria_json   TEXT      -- 실제 적용한 검색 조건
);

CREATE TABLE IF NOT EXISTS run_keyword_videos (   -- 실행별로 어떤 키워드에서 어떤 영상이 나왔는지
    run_id          TEXT NOT NULL REFERENCES runs(run_id),
    keyword         TEXT NOT NULL,
    video_id        TEXT NOT NULL REFERENCES videos(video_id),
    PRIMARY KEY (run_id, keyword, video_id)
);

CREATE TABLE IF NOT EXISTS video_formats (   -- LLM 콘텐츠 포맷 분류 (제목·설명·해시태그 기반)
    video_id        TEXT NOT NULL REFERENCES videos(video_id),
    prompt_version  TEXT NOT NULL,
    format          TEXT NOT NULL,
    confidence      TEXT,         -- 높음 | 중간 | 낮음
    reason          TEXT,         -- 판단 근거 한 줄
    model           TEXT NOT NULL,
    classified_at   TEXT NOT NULL,
    input_tokens    INTEGER,
    output_tokens   INTEGER,
    PRIMARY KEY (video_id, prompt_version)
);

CREATE TABLE IF NOT EXISTS collection_logs (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id              TEXT NOT NULL REFERENCES runs(run_id),
    mode                TEXT,
    keyword             TEXT,
    started_at          TEXT,
    finished_at         TEXT,
    pages               INTEGER,  -- search.list 호출 페이지 수
    requested           INTEGER,  -- 요청한 개수
    received            INTEGER,  -- 받은 개수
    new_videos          INTEGER,  -- DB에 처음 저장된 영상 수
    shorts              INTEGER,  -- 쇼츠로 판정된 수
    excluded_not_short  INTEGER,  -- 쇼츠 판정에서 제외된 수
    duplicates          INTEGER,  -- 이미 저장돼 있던 영상 수
    failed              INTEGER,  -- 조회 실패 영상 수 + 실패한 API 호출 수
    quota_units         INTEGER,
    error_message       TEXT
);
"""


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys = ON")
    _migrate_runs_columns(conn)
    conn.executescript(SCHEMA)
    _backfill_v1(conn)
    return conn


def _migrate_runs_columns(conn) -> None:
    """기존 DB의 runs 테이블에 criteria 컬럼이 없으면 추가."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(runs)")}
    if not cols:  # 새 DB: SCHEMA가 생성
        return
    for col in ("criteria", "criteria_json"):
        if col not in cols:
            conn.execute(f"ALTER TABLE runs ADD COLUMN {col} TEXT")
    conn.commit()


def _backfill_v1(conn) -> None:
    """기준 기록 이전의 실행을 v1로 표시하고, 그 search 실행의 키워드 연결을 run_keyword_videos에 복사."""
    conn.execute("UPDATE runs SET criteria = 'v1_date_latest' WHERE criteria IS NULL")
    conn.execute(
        """
        INSERT OR IGNORE INTO run_keyword_videos (run_id, keyword, video_id)
        SELECT kv.first_run_id, kv.keyword, kv.video_id
        FROM keyword_videos kv JOIN runs r ON r.run_id = kv.first_run_id
        WHERE r.criteria = 'v1_date_latest' AND r.mode = 'search'
        """
    )
    conn.commit()


def video_exists(conn, video_id: str) -> bool:
    return conn.execute("SELECT 1 FROM videos WHERE video_id = ?", (video_id,)).fetchone() is not None


def upsert_video(conn, row: dict, now: str) -> None:
    conn.execute(
        """
        INSERT INTO videos (video_id, title, description, hashtags, published_at, duration_sec,
                            channel_id, embed_width, embed_height, has_shorts_tag, is_short,
                            first_seen_at, updated_at)
        VALUES (:video_id, :title, :description, :hashtags, :published_at, :duration_sec,
                :channel_id, :embed_width, :embed_height, :has_shorts_tag, :is_short, :now, :now)
        ON CONFLICT(video_id) DO UPDATE SET
            title = excluded.title, description = excluded.description,
            hashtags = excluded.hashtags, published_at = excluded.published_at,
            duration_sec = excluded.duration_sec, channel_id = excluded.channel_id,
            embed_width = excluded.embed_width, embed_height = excluded.embed_height,
            has_shorts_tag = excluded.has_shorts_tag, is_short = excluded.is_short,
            updated_at = excluded.updated_at
        """,
        {**row, "hashtags": json.dumps(row["hashtags"], ensure_ascii=False), "now": now},
    )


def link_keyword(conn, keyword: str, video_id: str, run_id: str, now: str) -> None:
    conn.execute(
        """
        INSERT INTO keyword_videos (keyword, video_id, first_seen_at, first_run_id, last_run_id)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(keyword, video_id) DO UPDATE SET last_run_id = excluded.last_run_id
        """,
        (keyword, video_id, now, run_id, run_id),
    )


def link_run_keyword(conn, run_id: str, keyword: str, video_id: str) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO run_keyword_videos (run_id, keyword, video_id) VALUES (?, ?, ?)",
        (run_id, keyword, video_id),
    )


def insert_video_snapshot(conn, run_id, video_id, collected_at, views, likes, comments) -> None:
    conn.execute(
        """
        INSERT OR IGNORE INTO video_snapshots
            (run_id, video_id, collected_at, view_count, like_count, comment_count)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (run_id, video_id, collected_at, views, likes, comments),
    )


def upsert_channel(conn, channel_id: str, title: str, now: str) -> None:
    conn.execute(
        """
        INSERT INTO channels (channel_id, title, first_seen_at, updated_at) VALUES (?, ?, ?, ?)
        ON CONFLICT(channel_id) DO UPDATE SET title = excluded.title, updated_at = excluded.updated_at
        """,
        (channel_id, title, now, now),
    )


def insert_channel_snapshot(conn, run_id, channel_id, collected_at, subscribers, hidden) -> None:
    conn.execute(
        """
        INSERT OR IGNORE INTO channel_snapshots
            (run_id, channel_id, collected_at, subscriber_count, subscriber_hidden)
        VALUES (?, ?, ?, ?, ?)
        """,
        (run_id, channel_id, collected_at, subscribers, int(hidden)),
    )


def short_ids_by_keyword(conn, keywords: list[str], criteria: str) -> dict[str, list[str]]:
    """재수집 대상: 해당 수집 기준의 search 실행에서 나온 키워드별 쇼츠 영상 ID (최신 업로드 순)."""
    marks = ",".join("?" * len(keywords))
    rows = conn.execute(
        f"""
        SELECT DISTINCT rkv.keyword, rkv.video_id
        FROM run_keyword_videos rkv
        JOIN runs r   ON r.run_id = rkv.run_id
        JOIN videos v ON v.video_id = rkv.video_id
        WHERE r.mode = 'search' AND r.criteria = ? AND v.is_short = 1 AND rkv.keyword IN ({marks})
        ORDER BY rkv.keyword, v.published_at DESC
        """,
        [criteria, *keywords],
    ).fetchall()
    result: dict[str, list[str]] = {k: [] for k in keywords}
    for kw, vid in rows:
        result[kw].append(vid)
    return result


def elapsed_days_by_keyword(conn, run_id: str) -> dict[str, list[float]]:
    """해당 실행에서 수집한 쇼츠의 업로드 후 경과 일수 (스냅샷 수집 시각 기준)."""
    rows = conn.execute(
        """
        SELECT rkv.keyword, julianday(s.collected_at) - julianday(v.published_at)
        FROM run_keyword_videos rkv
        JOIN videos v ON v.video_id = rkv.video_id
        JOIN video_snapshots s ON s.run_id = rkv.run_id AND s.video_id = rkv.video_id
        WHERE rkv.run_id = ? AND v.is_short = 1
        """,
        (run_id,),
    ).fetchall()
    result: dict[str, list[float]] = {}
    for kw, days in rows:
        result.setdefault(kw, []).append(days)
    return result


def channel_ids_for(conn, video_ids: list[str]) -> set[str]:
    ids: set[str] = set()
    for i in range(0, len(video_ids), 500):
        chunk = video_ids[i:i + 500]
        marks = ",".join("?" * len(chunk))
        ids.update(r[0] for r in conn.execute(
            f"SELECT DISTINCT channel_id FROM videos WHERE video_id IN ({marks}) AND channel_id IS NOT NULL",
            chunk,
        ))
    return ids


def last_successful_search(conn, criteria: str) -> str | None:
    """해당 기준의 마지막 성공 search 실행 시각(run_id, UTC ISO)."""
    row = conn.execute(
        "SELECT MAX(run_id) FROM runs WHERE criteria = ? AND mode = 'search' AND status = 'ok'",
        (criteria,),
    ).fetchone()
    return row[0] if row else None


def purge_older_than(conn, cutoff_iso: str) -> dict[str, int]:
    """수집 시각이 cutoff 이전인 데이터를 삭제 (YouTube API 정책의 30일 기준).

    스냅샷·실행 기록을 지운 뒤, 남은 스냅샷이 없는 영상·채널·키워드 연결도 지운다.
    """
    counts = {}

    def run(name: str, sql: str, params=()):
        counts[name] = conn.execute(sql, params).rowcount

    old_runs = "SELECT run_id FROM runs WHERE started_at < ?"
    run("video_snapshots", "DELETE FROM video_snapshots WHERE collected_at < ?", (cutoff_iso,))
    run("channel_snapshots", "DELETE FROM channel_snapshots WHERE collected_at < ?", (cutoff_iso,))
    run("run_keyword_videos", f"DELETE FROM run_keyword_videos WHERE run_id IN ({old_runs})", (cutoff_iso,))
    run("collection_logs", f"DELETE FROM collection_logs WHERE run_id IN ({old_runs})", (cutoff_iso,))
    run("runs", "DELETE FROM runs WHERE started_at < ?", (cutoff_iso,))
    run("keyword_videos", """DELETE FROM keyword_videos WHERE video_id NOT IN
                             (SELECT DISTINCT video_id FROM video_snapshots)""")
    run("run_keyword_videos_orphan", """DELETE FROM run_keyword_videos WHERE video_id NOT IN
                                        (SELECT DISTINCT video_id FROM video_snapshots)""")
    run("video_formats", """DELETE FROM video_formats WHERE video_id NOT IN
                            (SELECT DISTINCT video_id FROM video_snapshots)""")
    run("videos", "DELETE FROM videos WHERE video_id NOT IN (SELECT DISTINCT video_id FROM video_snapshots)")
    run("channels", """DELETE FROM channels WHERE channel_id NOT IN
                       (SELECT DISTINCT channel_id FROM channel_snapshots)""")
    conn.commit()
    return {k: v for k, v in counts.items() if v}


def start_run(conn, run_id: str, mode: str, now: str, criteria: str, criteria_json: dict) -> None:
    conn.execute(
        """
        INSERT INTO runs (run_id, mode, started_at, quota_units, status, criteria, criteria_json)
        VALUES (?, ?, ?, 0, 'running', ?, ?)
        """,
        (run_id, mode, now, criteria, json.dumps(criteria_json, ensure_ascii=False)),
    )
    conn.commit()


def finish_run(conn, run_id: str, now: str, units: int, status: str, error: str | None) -> None:
    conn.execute(
        "UPDATE runs SET finished_at = ?, quota_units = ?, status = ?, error_message = ? WHERE run_id = ?",
        (now, units, status, error, run_id),
    )
    conn.commit()


def insert_log(conn, run_id: str, mode: str, log: dict) -> None:
    conn.execute(
        """
        INSERT INTO collection_logs
            (run_id, mode, keyword, started_at, finished_at, pages, requested, received, new_videos,
             shorts, excluded_not_short, duplicates, failed, quota_units, error_message)
        VALUES (:run_id, :mode, :keyword, :started_at, :finished_at, :pages, :requested, :received,
                :new_videos, :shorts, :excluded, :duplicates, :failed, :units, :error)
        """,
        {**log, "run_id": run_id, "mode": mode},
    )
    conn.commit()
