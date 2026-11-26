"""YouTube 쇼츠 성과 수집기.

사용법 (venv에서 실행):
  python collect.py search    # 첫 수집: 키워드 검색으로 영상 찾기 (search.list, 페이지당 100유닛)
  python collect.py refresh   # 재수집: 저장된 쇼츠의 지표 스냅샷만 추가 (videos.list, 50개당 1유닛)
  python collect.py reclassify  # 저장된 값으로 쇼츠 여부 재판정 (API 호출 없음)
  python collect.py purge       # 보관 기간(30일) 지난 데이터 삭제 (API 호출 없음)

옵션:
  --keywords "a" "b"   일부 키워드만 실행 (기본: config.KEYWORDS 전체)
  --limit N            search 모드에서 키워드당 쇼츠 최대 개수 (기본: 50)
"""
import argparse
import json
import re
import sys
import unicodedata
import warnings
from datetime import datetime, timedelta, timezone
from statistics import median
from pathlib import Path

# Python 3.10 지원 종료 안내 경고만 숨김 (동작에는 영향 없음)
warnings.filterwarnings("ignore", category=FutureWarning, module=r"google\.api_core")

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

import config
import db
import keys

QUOTA_REASONS = {"quotaExceeded", "dailyLimitExceeded"}


class ApiError(Exception):
    """API 호출 실패 (메시지에서 API 키는 가려져 있음)."""


class QuotaExceeded(ApiError):
    """하루 할당량 초과: 즉시 중단."""


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def chunks(items: list, size: int = 50):
    for i in range(0, len(items), size):
        yield items[i:i + size]


# ---------------------------------------------------------------- API 래퍼

class YouTube:
    def __init__(self, api_key: str):
        self._key = api_key
        self._yt = build("youtube", "v3", developerKey=api_key, cache_discovery=False)
        self.units = 0

    def _mask(self, text) -> str:
        return str(text).replace(self._key, "***")

    def _execute(self, request, kind: str) -> dict:
        self.units += config.UNIT_COST[kind]
        try:
            return request.execute(num_retries=2)
        except HttpError as e:
            reason = _error_reason(e)
            msg = self._mask(f"HTTP {e.resp.status} {reason}: {getattr(e, 'reason', '')}")
            if reason in QUOTA_REASONS:
                raise QuotaExceeded(msg) from None
            raise ApiError(msg) from None
        except Exception as e:  # 네트워크 오류 등: 메시지에 URL(키 포함)이 섞일 수 있어 가림
            raise ApiError(self._mask(f"{type(e).__name__}: {e}")) from None

    def search(self, keyword: str, page_token: str | None, window: dict) -> dict:
        params = dict(
            part="id", q=keyword, type="video", order=config.SEARCH_ORDER,
            maxResults=config.SEARCH_PAGE_SIZE, regionCode=config.REGION_CODE,
            relevanceLanguage=config.RELEVANCE_LANGUAGE, videoDuration="short",
            publishedAfter=window["published_after"], publishedBefore=window["published_before"],
        )
        if page_token:
            params["pageToken"] = page_token
        return self._execute(self._yt.search().list(**params), "search")

    def videos(self, ids: list[str], full: bool) -> dict[str, dict]:
        """ID → 영상 item. full=False면 지표(statistics)만."""
        part = "snippet,contentDetails,statistics,player" if full else "statistics"
        out: dict[str, dict] = {}
        for batch in chunks(ids):
            params = dict(part=part, id=",".join(batch), maxResults=50)
            if full:
                params["maxWidth"] = config.EMBED_MAX_WIDTH
            res = self._execute(self._yt.videos().list(**params), "videos")
            out.update({it["id"]: it for it in res.get("items", [])})
        return out

    def comment_threads(self, video_id: str, max_results: int) -> dict:
        """최상위 댓글만 (replies 파트를 요청하지 않음), 관련도순."""
        params = dict(part="snippet", videoId=video_id, order="relevance",
                      maxResults=max_results, textFormat="plainText")
        return self._execute(self._yt.commentThreads().list(**params), "comments")

    def channels(self, ids: list[str]) -> dict:
        params = dict(part="snippet,statistics", id=",".join(ids), maxResults=50)
        return self._execute(self._yt.channels().list(**params), "channels")


def _error_reason(e: HttpError) -> str:
    try:
        return json.loads(e.content.decode("utf-8"))["error"]["errors"][0].get("reason", "")
    except Exception:
        return ""


# ---------------------------------------------------------------- 파싱·쇼츠 판정

_DURATION_RE = re.compile(r"^P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?$")


def parse_duration(value: str | None) -> int | None:
    """ISO 8601 길이(PT1M5S)를 초로 변환."""
    m = _DURATION_RE.match(value or "")
    if not m:
        return None
    d, h, mi, s = (int(x) if x else 0 for x in m.groups())
    return d * 86400 + h * 3600 + mi * 60 + s


def extract_hashtags(*texts: str) -> list[str]:
    tags: list[str] = []
    for text in texts:
        for tag in re.findall(r"#(\w+)", text or ""):
            tag = tag.lower()
            if tag not in tags:
                tags.append(tag)
    return tags


def to_int(value) -> int | None:
    return int(value) if value is not None else None


def _parse_time(value: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None
    except ValueError:
        return None


_RULE_CHANGE_AT = _parse_time(config.SHORT_RULE_CHANGE_AT)


def short_max_seconds(published_at: str | None) -> int:
    """업로드 시각에 따른 쇼츠 최대 길이. 업로드 시각을 모르면 두 기준 모두 만족하는 60초를 적용."""
    published = _parse_time(published_at)
    if published is not None and published >= _RULE_CHANGE_AT:
        return config.SHORT_MAX_SECONDS
    return config.SHORT_MAX_SECONDS_OLD


def classify_short(duration: int | None, width: int | None, height: int | None,
                   published_at: str | None) -> int:
    """정사각형/세로 AND 길이가 업로드 시점 기준 이하면 쇼츠. 길이나 화면 크기를 모르면 판정 불가로 0."""
    if duration is None or not width or not height:
        return 0
    if height < width:                      # 가로 영상은 길이와 상관없이 일반 영상
        return 0
    return int(duration <= short_max_seconds(published_at))


def exclusion_reason(duration, width, height, published_at) -> str:
    if duration is None or not width or not height:
        return "길이/화면 정보 없음"
    if height < width:
        return "가로 영상"
    limit = short_max_seconds(published_at)
    if duration > limit:
        return f"{limit}초 초과" + ("" if published_at else " (업로드 시각 없음)")
    return "쇼츠"


def to_video_row(item: dict) -> dict:
    sn, cd, pl = item.get("snippet", {}), item.get("contentDetails", {}), item.get("player", {})
    duration = parse_duration(cd.get("duration"))
    width, height = to_int(pl.get("embedWidth")), to_int(pl.get("embedHeight"))
    hashtags = extract_hashtags(sn.get("title"), sn.get("description"))
    has_tag = "shorts" in hashtags
    return {
        "video_id": item["id"],
        "title": sn.get("title"),
        "description": sn.get("description"),
        "hashtags": hashtags,
        "published_at": sn.get("publishedAt"),
        "duration_sec": duration,
        "channel_id": sn.get("channelId"),
        "embed_width": width,
        "embed_height": height,
        "has_shorts_tag": int(has_tag),
        "is_short": classify_short(duration, width, height, sn.get("publishedAt")),
    }


def stats_of(item: dict) -> tuple:
    st = item.get("statistics", {})
    return to_int(st.get("viewCount")), to_int(st.get("likeCount")), to_int(st.get("commentCount"))


def new_log(keyword: str) -> dict:
    return dict(keyword=keyword, started_at=now_iso(), finished_at=None, pages=0, requested=0,
                received=0, new_videos=0, shorts=0, excluded=0, duplicates=0, failed=0,
                units=0, error=None)


# ---------------------------------------------------------------- 수집 모드

def search_criteria(started: datetime) -> dict:
    """이번 실행에 적용할 검색 조건 (runs.criteria_json에 기록)."""
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    return {
        "criteria": config.CRITERIA,
        "order": config.SEARCH_ORDER,
        "published_after": (started - timedelta(days=config.PUBLISHED_MAX_DAYS_AGO)).strftime(fmt),
        "published_before": (started - timedelta(days=config.PUBLISHED_MIN_DAYS_AGO)).strftime(fmt),
        "region_code": config.REGION_CODE,
        "relevance_language": config.RELEVANCE_LANGUAGE,
        "video_duration": "short",
        "max_shorts_per_keyword": config.MAX_SHORTS_PER_KEYWORD,
        "max_pages": config.MAX_SEARCH_PAGES,
        "short_rule": {
            "aspect": "height >= width",
            "max_seconds": config.SHORT_MAX_SECONDS,
            "max_seconds_before_rule_change": config.SHORT_MAX_SECONDS_OLD,
            "rule_change_at": config.SHORT_RULE_CHANGE_AT,
        },
    }


def run_search(yt: YouTube, conn, run_id: str, keywords: list[str], limit: int,
               logs: list, channel_ids: set, window: dict, progress=None) -> None:
    fetched: dict[str, dict] = {}  # 이번 실행에서 이미 상세 조회한 영상 (키워드 간 재조회 방지)

    for i, kw in enumerate(keywords, 1):
        if progress:
            progress(f"키워드 {i}/{len(keywords)} 검색 중: {kw}")
        log = new_log(kw)
        logs.append(log)
        units_before = yt.units
        seen_kw: set[str] = set()
        token = None
        try:
            while log["pages"] < config.MAX_SEARCH_PAGES and log["shorts"] < limit:
                res = yt.search(kw, token, window)
                log["pages"] += 1
                log["requested"] += config.SEARCH_PAGE_SIZE
                ids = [it["id"]["videoId"] for it in res.get("items", [])
                       if it.get("id", {}).get("videoId")]
                log["received"] += len(ids)

                need = list(dict.fromkeys(v for v in ids if v not in fetched and v not in seen_kw))
                if need:
                    fetched.update(yt.videos(need, full=True))

                collected_at = now_iso()
                for vid in ids:
                    if log["shorts"] >= limit:
                        break
                    if vid in seen_kw:          # 같은 키워드의 다른 페이지에서 또 나옴
                        log["duplicates"] += 1
                        continue
                    seen_kw.add(vid)
                    item = fetched.get(vid)
                    if item is None:            # 삭제·비공개 등으로 상세 조회 결과 없음
                        log["failed"] += 1
                        continue
                    row = to_video_row(item)
                    if db.video_exists(conn, vid):
                        log["duplicates"] += 1
                    else:
                        log["new_videos"] += 1
                    db.upsert_video(conn, row, collected_at)
                    db.link_keyword(conn, kw, vid, run_id, collected_at)
                    db.link_run_keyword(conn, run_id, kw, vid)
                    db.insert_video_snapshot(conn, run_id, vid, collected_at, *stats_of(item))
                    if row["channel_id"]:
                        channel_ids.add(row["channel_id"])
                    if row["is_short"]:
                        log["shorts"] += 1
                    else:
                        log["excluded"] += 1
                conn.commit()

                token = res.get("nextPageToken")
                if not token:
                    break
        except QuotaExceeded as e:
            log["failed"] += 1
            log["error"] = str(e)
            raise
        except ApiError as e:
            log["failed"] += 1
            log["error"] = str(e)
        finally:
            conn.commit()
            log["units"] = yt.units - units_before
            log["finished_at"] = now_iso()
            db.insert_log(conn, run_id, "search", log)


def run_refresh(yt: YouTube, conn, run_id: str, keywords: list[str],
                logs: list, channel_ids: set) -> None:
    targets = db.short_ids_by_keyword(conn, keywords, config.CRITERIA)
    all_ids = list(dict.fromkeys(v for ids in targets.values() for v in ids))
    results: dict[str, dict] = {}
    batch_errors: list[str] = []

    try:
        for batch in chunks(all_ids):
            try:
                results.update(yt.videos(batch, full=False))
            except QuotaExceeded:
                raise
            except ApiError as e:
                batch_errors.append(str(e))
    finally:
        collected_at = now_iso()
        for vid, item in results.items():
            db.insert_video_snapshot(conn, run_id, vid, collected_at, *stats_of(item))
        conn.commit()
        channel_ids.update(db.channel_ids_for(conn, list(results)))

        counted: set[str] = set()
        for kw in keywords:
            log = new_log(kw)
            ids = targets.get(kw, [])
            log["requested"] = len(ids)
            log["received"] = sum(1 for v in ids if v in results)
            log["shorts"] = log["received"]
            log["failed"] = len(ids) - log["received"]
            log["duplicates"] = sum(1 for v in ids if v in counted)  # 앞 키워드와 겹치는 영상
            counted.update(ids)
            log["error"] = "; ".join(batch_errors) or None
            log["finished_at"] = now_iso()
            db.insert_log(conn, run_id, "refresh", log)
            logs.append(log)


def collect_channels(yt: YouTube, conn, run_id: str, channel_ids: set) -> dict:
    summary = dict(requested=len(channel_ids), received=0, hidden=0, failed=0, error=None)
    for batch in chunks(sorted(channel_ids)):
        try:
            res = yt.channels(batch)
        except QuotaExceeded:
            summary["failed"] += len(batch)
            raise
        except ApiError as e:
            summary["failed"] += len(batch)
            summary["error"] = str(e)
            continue
        collected_at = now_iso()
        items = res.get("items", [])
        for it in items:
            st = it.get("statistics", {})
            hidden = bool(st.get("hiddenSubscriberCount"))
            subs = None if hidden else to_int(st.get("subscriberCount"))
            db.upsert_channel(conn, it["id"], it.get("snippet", {}).get("title"), collected_at)
            db.insert_channel_snapshot(conn, run_id, it["id"], collected_at, subs, hidden)
            summary["hidden"] += int(hidden)
        summary["received"] += len(items)
        summary["failed"] += len(batch) - len(items)
        conn.commit()
    return summary


def run_reclassify(conn) -> None:
    """저장된 길이·화면 크기·업로드 시각으로 쇼츠 여부를 다시 판정 (API 호출 없음)."""
    rows = conn.execute(
        "SELECT video_id, title, duration_sec, embed_width, embed_height, published_at, is_short FROM videos"
    ).fetchall()
    now = now_iso()
    changes = []
    for vid, title, dur, w, h, pub, old in rows:
        new = classify_short(dur, w, h, pub)
        if new != old:
            conn.execute("UPDATE videos SET is_short = ?, updated_at = ? WHERE video_id = ?", (new, now, vid))
            changes.append((vid, title, dur, w, h, pub, old, new, exclusion_reason(dur, w, h, pub)))
    conn.commit()

    total = len(rows)
    shorts = conn.execute("SELECT COUNT(*) FROM videos WHERE is_short = 1").fetchone()[0]
    print(f"[reclassify] 영상 {total}개 재판정 → 쇼츠 {shorts}, 제외 {total - shorts}, 변경 {len(changes)}")
    for vid, title, dur, w, h, pub, old, new, reason in changes:
        arrow = "쇼츠→제외" if old else "제외→쇼츠"
        print(f"  {arrow} [{reason}] {vid} {dur}초 {w}x{h} 업로드={pub or '없음'} | {(title or '')[:50]}")


# ---------------------------------------------------------------- 요약 출력

def _width(s: str) -> int:
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in s)


def _pad(s, w: int, right: bool = True) -> str:
    s = str(s)
    gap = " " * max(0, w - _width(s))
    return gap + s if right else s + gap


def print_summary(mode: str, run_id: str, status: str, units: int, logs: list, ch: dict | None) -> None:
    print(f"\n[{mode}] run_id={run_id}  상태={status}  사용 유닛 ≈ {units}")
    cols = [("페이지", "pages"), ("요청", "requested"), ("받음", "received"), ("신규", "new_videos"),
            ("쇼츠", "shorts"), ("제외", "excluded"), ("중복", "duplicates"), ("실패", "failed")]
    if mode == "refresh":
        cols = [c for c in cols if c[1] in ("requested", "received", "duplicates", "failed")]
    kw_w = max([_width("키워드")] + [_width(l["keyword"]) for l in logs]) + 2
    print(_pad("키워드", kw_w, right=False) + "".join(_pad(h, 8) for h, _ in cols))
    print("-" * (kw_w + 8 * len(cols)))
    for l in logs:
        print(_pad(l["keyword"], kw_w, right=False) + "".join(_pad(l[k], 8) for _, k in cols))
    print("-" * (kw_w + 8 * len(cols)))
    print(_pad("합계", kw_w, right=False) + "".join(_pad(sum(l[k] for l in logs), 8) for _, k in cols))
    if ch is not None:
        print(f"채널: 요청 {ch['requested']}, 받음 {ch['received']}, "
              f"구독자 비공개 {ch['hidden']}, 실패 {ch['failed']}")
    errors = [(l["keyword"], l["error"]) for l in logs if l["error"]]
    if ch and ch["error"]:
        errors.append(("(채널)", ch["error"]))
    for kw, err in errors:
        print(f"  ! {kw}: {err}")


def print_age_summary(keywords: list[str], ages: dict[str, list[float]]) -> None:
    """쇼츠의 업로드 후 경과 일수 분포 (실제 값, 하한 적용 전)."""
    print(f"\n업로드 후 경과 일수 (쇼츠, 수집 시각 기준 / 지표 계산 시 최소 {config.MIN_ELAPSED_DAYS}일 적용)")
    kw_w = max([_width("키워드")] + [_width(k) for k in keywords]) + 2
    print(_pad("키워드", kw_w, right=False) + "".join(_pad(h, 9) for h in ("개수", "최소", "중앙값", "최대")))
    everything = []
    for kw in keywords + ["(전체)"]:
        vals = everything if kw == "(전체)" else ages.get(kw, [])
        if kw != "(전체)":
            everything.extend(vals)
        if not vals:
            print(_pad(kw, kw_w, right=False) + _pad(0, 9))
            continue
        print(_pad(kw, kw_w, right=False) + _pad(len(vals), 9)
              + "".join(_pad(f"{v:.1f}", 9) for v in (min(vals), median(vals), max(vals))))


# ---------------------------------------------------------------- 수집 실행 (CLI·대시보드 공용)

def purge_expired(conn, now: datetime | None = None) -> dict[str, int]:
    """보관 기간(config.RETENTION_DAYS)이 지난 수집 데이터 삭제."""
    now = now or datetime.now(timezone.utc)
    cutoff = (now - timedelta(days=config.RETENTION_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return db.purge_older_than(conn, cutoff)


def explain_error(error: str | None) -> str:
    """수집 오류 메시지를 사람이 읽을 설명으로 바꿈 (원문은 이미 키가 가려진 상태)."""
    if not error:
        return ""
    e = error
    if "quotaExceeded" in e or "dailyLimitExceeded" in e:
        hint = "YouTube API 하루 할당량(10,000유닛)을 모두 썼습니다. 미국 태평양 시간 자정에 초기화됩니다."
    elif "keyInvalid" in e or "API key not valid" in e or ("badRequest" in e and "key" in e.lower()):
        hint = "API 키가 올바르지 않습니다. Secrets(배포) 또는 .env(로컬)의 YOUTUBE_API_KEY를 확인하세요."
    elif "accessNotConfigured" in e or "SERVICE_DISABLED" in e or "has not been used" in e:
        hint = "이 키의 Google Cloud 프로젝트에서 YouTube Data API v3가 활성화되어 있지 않습니다."
    elif "forbidden" in e.lower() or "HTTP 403" in e:
        hint = "API 접근이 거부되었습니다. 키의 API 제한·사용 설정을 확인하세요."
    elif any(s in e for s in ("Timeout", "ConnectionError", "ServerNotFound", "socket", "SSLError")):
        hint = "YouTube API 서버에 연결하지 못했습니다(네트워크 오류). 잠시 후 다시 시도됩니다."
    else:
        hint = "수집 중 오류가 발생했습니다."
    return f"{hint} (원문: {e})"


def run_collection(mode: str, api_key: str, keywords: list[str] | None = None, limit: int | None = None,
                   db_path: Path | None = None, progress=None) -> dict:
    """수집 1회 실행. 예외를 밖으로 던지지 않고 결과(상태·오류·로그)를 돌려줌. 오류 메시지는 키가 가려짐."""
    keywords = keywords or config.KEYWORDS
    limit = limit or config.MAX_SHORTS_PER_KEYWORD
    db_path = db_path or config.DB_PATH
    started = datetime.now(timezone.utc).replace(microsecond=0)
    run_id = started.strftime("%Y-%m-%dT%H:%M:%SZ")
    result = dict(mode=mode, run_id=run_id, status="ok", error=None, units=0,
                  logs=[], channels=None, ages={}, purged={}, keywords=keywords)

    def masked(e: Exception) -> str:
        return keys.mask(f"{type(e).__name__}: {e}", api_key)

    try:
        yt = YouTube(api_key)
    except Exception as e:
        result.update(status="error", error=masked(e))
        return result

    conn = db.connect(db_path)
    try:
        result["purged"] = purge_expired(conn, started)
        criteria = search_criteria(started) if mode == "search" else {"criteria": config.CRITERIA}
        db.start_run(conn, run_id, mode, run_id, config.CRITERIA, criteria)
        channel_ids: set[str] = set()
        try:
            if mode == "search":
                if progress:
                    progress(f"수집 기준 {config.CRITERIA}: 관련도순, 업로드 "
                             f"{criteria['published_after'][:10]} ~ {criteria['published_before'][:10]}")
                run_search(yt, conn, run_id, keywords, limit, result["logs"], channel_ids, criteria, progress)
            else:
                if progress:
                    progress("저장된 쇼츠의 지표 재수집 중")
                run_refresh(yt, conn, run_id, keywords, result["logs"], channel_ids)
            if progress:
                progress(f"채널 {len(channel_ids)}개 구독자 수 수집 중")
            result["channels"] = collect_channels(yt, conn, run_id, channel_ids)
            # 키워드 하나라도 API 오류면 실행 전체를 실패로 처리 (일부만 수집된 데이터가 '최신'으로 쓰이지 않게)
            kw_errors = [f"{l['keyword']}: {l['error']}" for l in result["logs"] if l["error"]]
            if result["channels"] and result["channels"]["error"]:
                kw_errors.append(f"(채널): {result['channels']['error']}")
            if kw_errors:
                result.update(status="error", error=kw_errors[0] if len(kw_errors) == 1
                              else f"{kw_errors[0]} 외 {len(kw_errors) - 1}건")
        except QuotaExceeded as e:
            result.update(status="quota_exceeded", error=str(e))
        except Exception as e:
            result.update(status="error", error=masked(e))
        finally:
            result["units"] = yt.units
            db.finish_run(conn, run_id, now_iso(), yt.units, result["status"], result["error"])
            if mode == "search":
                result["ages"] = db.elapsed_days_by_keyword(conn, run_id)
    except Exception as e:  # DB 오류 등
        result.update(status="error", error=masked(e))
    finally:
        conn.close()
    return result


# ---------------------------------------------------------------- 진입점

def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="YouTube 쇼츠 성과 수집기")
    parser.add_argument("mode", choices=["search", "refresh", "reclassify", "purge"])
    parser.add_argument("--keywords", nargs="+", default=config.KEYWORDS)
    parser.add_argument("--limit", type=int, default=config.MAX_SHORTS_PER_KEYWORD)
    args = parser.parse_args()

    if args.mode in ("reclassify", "purge"):
        conn = db.connect(config.DB_PATH)
        try:
            if args.mode == "reclassify":
                run_reclassify(conn)
            else:
                print(f"[purge] {config.RETENTION_DAYS}일 지난 데이터 삭제: {purge_expired(conn) or '삭제 없음'}")
        finally:
            conn.close()
        return 0

    api_key = keys.get_api_key()
    if not api_key:
        print(f"{keys.KEY_NAME}가 .env(또는 환경변수)에 없습니다.")
        return 1

    result = run_collection(args.mode, api_key, args.keywords, args.limit, progress=print)
    if result["purged"]:
        print(f"{config.RETENTION_DAYS}일 지난 데이터 삭제: {result['purged']}")
    if result["error"]:
        print(f"수집 오류: {explain_error(result['error'])}")
    print_summary(args.mode, result["run_id"], result["status"], result["units"],
                  result["logs"], result["channels"])
    if result["ages"]:
        print_age_summary(args.keywords, result["ages"])
    return 0 if result["status"] == "ok" else 2


if __name__ == "__main__":
    sys.exit(main())
