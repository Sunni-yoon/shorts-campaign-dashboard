"""영상별 상위 댓글 수집 (YouTube commentThreads.list, 영상당 1유닛).

- 최신 search 실행(현재 수집 기준)의 쇼츠 중 아직 수집하지 않은 영상만
- 관련도 상위 config.COMMENTS_PER_VIDEO개, 답글 제외 (replies 파트를 요청하지 않음)
- 저장: 댓글 텍스트(@멘션 → @user), 좋아요 수, 순위, 수집 시각
- 저장하지 않음: 작성자 이름·채널 ID·프로필 이미지·작성 시각, 원본 댓글 ID (SHA-256 해시만 저장)
- 댓글 사용 중지 영상은 건너뛰고 comment_fetches에 상태를 기록

사용법: venv\\Scripts\\python.exe collect_comments.py
"""
import hashlib
import re
import sys
from datetime import datetime, timezone

import collect
import config
import db
import keys

MENTION_RE = re.compile(r"@[\w.\-]+", re.UNICODE)


def comment_key(comment_id: str) -> str:
    return hashlib.sha256(comment_id.encode("utf-8")).hexdigest()


def clean_text(text: str | None) -> str:
    return MENTION_RE.sub("@user", text or "").strip()


def parse_threads(response: dict) -> list[dict]:
    """API 응답에서 필요한 필드만 꺼냄. 작성자 관련 필드는 읽지도 않음."""
    out = []
    for rank, item in enumerate(response.get("items", []), 1):
        top = item.get("snippet", {}).get("topLevelComment", {})
        sn = top.get("snippet", {})
        cid = top.get("id") or item.get("id")
        if not cid:
            continue
        out.append({"comment_key": comment_key(cid), "rank": rank,
                    "text": clean_text(sn.get("textDisplay") or sn.get("textOriginal")),
                    "like_count": collect.to_int(sn.get("likeCount"))})
    return out


def pending_videos(conn) -> list[tuple[str, int | None]]:
    """최신 search 실행(현재 수집 기준)의 쇼츠 중 댓글을 아직 수집하지 않은 영상 (video_id, 최신 스냅샷 댓글 수)."""
    return conn.execute(
        """
        SELECT DISTINCT v.video_id,
               (SELECT s.comment_count FROM video_snapshots s WHERE s.video_id = v.video_id
                ORDER BY s.collected_at DESC LIMIT 1)
        FROM run_keyword_videos rkv
        JOIN videos v ON v.video_id = rkv.video_id
        WHERE rkv.run_id = (SELECT MAX(run_id) FROM runs
                            WHERE criteria = ? AND mode = 'search' AND status = 'ok')
          AND v.is_short = 1
          AND v.video_id NOT IN (SELECT video_id FROM comment_fetches WHERE status <> 'error')   -- 오류는 재시도
        ORDER BY v.video_id
        """,
        (config.CRITERIA,),
    ).fetchall()


def _record(conn, video_id, status, count, error=None):
    conn.execute(
        "INSERT OR REPLACE INTO comment_fetches (video_id, status, comment_count, fetched_at, error_message) "
        "VALUES (?, ?, ?, ?, ?)",
        (video_id, status, count, collect.now_iso(), error),
    )


def collect_pending(conn, fetcher, progress=None, api_key: str | None = None) -> dict:
    """fetcher(video_id) -> API 응답 dict. 예외를 던지지 않고 요약을 돌려줌 (오류 메시지는 키가 가려짐)."""
    rows = pending_videos(conn)
    result = {"pending": len(rows), "ok": 0, "disabled": 0, "no_comments": 0, "error": 0,
              "comments": 0, "units": 0, "errors": [], "stopped": False}
    for i, (vid, comment_count) in enumerate(rows, 1):
        if progress and (i == 1 or i % 25 == 0 or i == len(rows)):
            progress(f"댓글 수집 중 {i}/{len(rows)}")
        if comment_count == 0:                       # 스냅샷상 댓글 0개 → API 호출 없이 기록
            _record(conn, vid, "no_comments", 0)
            result["no_comments"] += 1
            continue
        try:
            result["units"] += config.UNIT_COST["comments"]
            items = parse_threads(fetcher(vid))
        except collect.QuotaExceeded as e:
            result["errors"].append(keys.mask(str(e), api_key))
            result["stopped"] = True
            break
        except Exception as e:
            msg = keys.mask(f"{type(e).__name__}: {e}", api_key)
            if "commentsDisabled" in msg:
                _record(conn, vid, "disabled", 0)
                result["disabled"] += 1
            else:
                _record(conn, vid, "error", 0, msg)
                result["error"] += 1
                if len(result["errors"]) < 5:
                    result["errors"].append(f"{vid}: {msg}")
            continue
        now = collect.now_iso()
        for c in items:
            conn.execute(
                "INSERT OR IGNORE INTO video_comments (comment_key, video_id, rank, text, like_count, collected_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (c["comment_key"], vid, c["rank"], c["text"], c["like_count"], now),
            )
        _record(conn, vid, "ok" if items else "no_comments", len(items))
        result["ok" if items else "no_comments"] += 1
        result["comments"] += len(items)
        if i % 25 == 0:
            conn.commit()
    conn.commit()
    return result


def youtube_fetcher(api_key: str):
    yt = collect.YouTube(api_key)
    return lambda vid: yt.comment_threads(vid, config.COMMENTS_PER_VIDEO)


def status_counts(conn) -> dict[str, int]:
    """최신 실행(현재 수집 기준) 쇼츠의 댓글 수집 상태별 영상 수."""
    rows = conn.execute(
        """
        SELECT COALESCE(cf.status, 'not_fetched'), COUNT(DISTINCT v.video_id)
        FROM run_keyword_videos rkv JOIN videos v ON v.video_id = rkv.video_id
        LEFT JOIN comment_fetches cf ON cf.video_id = v.video_id
        WHERE rkv.run_id = (SELECT MAX(run_id) FROM runs WHERE criteria = ? AND mode = 'search' AND status = 'ok')
          AND v.is_short = 1
        GROUP BY 1
        """,
        (config.CRITERIA,),
    ).fetchall()
    return dict(rows)


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    api_key = keys.get_api_key(keys.KEY_NAME)
    if not api_key:
        print(f"{keys.KEY_NAME}가 .env(또는 환경변수)에 없습니다.")
        return 1
    conn = db.connect(config.DB_PATH)
    try:
        r = collect_pending(conn, youtube_fetcher(api_key), progress=print, api_key=api_key)
        counts = status_counts(conn)
    finally:
        conn.close()
    print(f"\n[댓글 수집] 대상 {r['pending']} · 수집 {r['ok']} · 사용 중지 {r['disabled']} · "
          f"댓글 없음 {r['no_comments']} · 오류 {r['error']}" + (" (할당량 초과로 중단)" if r["stopped"] else ""))
    print(f"댓글 {r['comments']:,}개 저장 · 사용 유닛 ≈ {r['units']}")
    for err in r["errors"]:
        print(f"  ! {collect.explain_error(err)}")
    print(f"최신 {config.CRITERIA} 쇼츠 상태별 영상 수: {counts}")
    return 0 if not (r["error"] or r["stopped"]) else 2


if __name__ == "__main__":
    sys.exit(main())
