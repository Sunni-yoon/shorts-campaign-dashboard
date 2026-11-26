"""지표 계산 모듈 (대시보드·리포트 공용, Streamlit 의존 없음).

지표 정의는 CLAUDE.md를 따른다.
- 참여율 = (좋아요 + 댓글) / 조회수 — 좋아요·댓글이 비어 있으면 0으로 넣지 않고 제외
- 조회수 속도 = 조회수 / 업로드 후 경과 일수 — 경과 일수는 최소 config.MIN_ELAPSED_DAYS
- 구독자 대비 조회수 = 조회수 / 채널 구독자 수 — 구독자 비공개 채널은 제외
- 요약 통계는 평균(영상별 값의 단순 평균)과 중앙값을 함께 제시

대상: 수집 기준(criteria)의 가장 최근 성공 search 실행에서 나온 쇼츠.
지표는 영상별 가장 최근 스냅샷 (그 뒤의 refresh 포함).

데이터 단위: 행 = (키워드, 영상). 한 영상이 여러 키워드에 속하면 키워드마다 한 행.
영상 단위 집계(전체 요약, 제외 수, 상위 영상)는 video_id로 중복을 제거한다.
"""
import json
import sqlite3
from pathlib import Path

import numpy as np
import pandas as pd

import config

# 지표 키 → 표시 이름
METRICS = {
    "views": "조회수",
    "engagement_rate": "참여율",
    "views_per_day": "조회수 속도",
    "views_per_subscriber": "구독자 대비 조회수",
}

# 지표 설명 — 짧은 버전: 대시보드 상단 카드용
SHORT_DESCRIPTIONS = {
    "video_count": "쇼츠 수 (중복 제외)",
    "views": "조회수의 가운데 값",
    "views_per_day": "조회수 ÷ 경과 일수",
    "engagement_rate": "(좋아요+댓글) ÷ 조회수",
    "views_per_subscriber": "조회수 ÷ 구독자 수",
}

# 지표 설명 — 긴 버전: 요약표 열 툴팁·데이터 기준 안내·리포트용
DESCRIPTIONS = {
    "video_count": "선택한 키워드의 v2 쇼츠 수 (여러 키워드에 걸친 영상은 1개로 셈)",
    "views": "영상들을 조회수 순으로 세웠을 때 가운데 값",
    "views_per_day": "조회수 ÷ 업로드 후 경과 일수, 업로드 후 지금까지의 하루 평균 조회수",
    "engagement_rate": "(좋아요 + 댓글) ÷ 조회수, 좋아요·댓글 숨김 영상 제외",
    "views_per_subscriber": "조회수 ÷ 채널 구독자 수, 1보다 크면 구독자보다 많이 봤다는 뜻",
}

# 제외 사유 이름 (한 곳에서 관리)
REASONS = {
    "no_views": "조회수 없음",
    "zero_views": "조회수 0",
    "likes_and_comments_hidden": "좋아요·댓글 숨김",
    "likes_hidden": "좋아요 숨김",
    "comments_disabled": "댓글 사용 중지",
    "no_published_at": "업로드 시각 없음",
    "subscribers_hidden": "구독자 비공개",
    "no_subscribers": "구독자 정보 없음",
    "zero_subscribers": "구독자 0",
}

# 지표 키 → 제외 사유 컬럼
REASON_COLUMNS = {
    "views": "views_excluded_reason",
    "engagement_rate": "engagement_excluded_reason",
    "views_per_day": "views_per_day_excluded_reason",
    "views_per_subscriber": "views_per_subscriber_excluded_reason",
}

_VIDEOS_SQL = """
WITH latest_run AS (   -- 해당 기준의 가장 최근 성공 search 실행
    SELECT MAX(run_id) AS run_id FROM runs
    WHERE criteria = ? AND mode = 'search' AND status = 'ok'
),
targets AS (
    SELECT DISTINCT rkv.keyword, rkv.video_id
    FROM run_keyword_videos rkv
    JOIN latest_run lr ON lr.run_id = rkv.run_id
    JOIN videos v ON v.video_id = rkv.video_id
    WHERE v.is_short = 1
),
latest AS (
    SELECT s.*, ROW_NUMBER() OVER (PARTITION BY s.video_id ORDER BY s.collected_at DESC, s.id DESC) AS rn
    FROM video_snapshots s
    WHERE s.video_id IN (SELECT video_id FROM targets)
)
SELECT t.keyword, v.video_id, v.title, v.published_at, v.duration_sec,
       v.embed_width, v.embed_height, v.channel_id, c.title AS channel_title,
       l.run_id AS snapshot_run_id, l.collected_at,
       l.view_count, l.like_count, l.comment_count
FROM targets t
JOIN videos v ON v.video_id = t.video_id
JOIN latest l ON l.video_id = t.video_id AND l.rn = 1
LEFT JOIN channels c ON c.channel_id = v.channel_id
ORDER BY t.keyword, v.published_at
"""


def _connect(db_path: Path) -> sqlite3.Connection:
    # 읽기 전용으로 열어 대시보드가 DB를 바꾸지 않게 함
    return sqlite3.connect(f"file:{Path(db_path).as_posix()}?mode=ro", uri=True)


def load_videos(db_path: Path = config.DB_PATH, criteria: str = config.CRITERIA) -> pd.DataFrame:
    """수집 기준에 해당하는 쇼츠의 최신 스냅샷 + 가장 가까운 시점의 구독자 수."""
    with _connect(db_path) as conn:
        df = pd.read_sql_query(_VIDEOS_SQL, conn, params=[criteria])
        channels = df["channel_id"].dropna().unique().tolist()
        if channels:
            marks = ",".join("?" * len(channels))
            ch = pd.read_sql_query(
                f"""SELECT channel_id, run_id AS channel_run_id, collected_at AS channel_collected_at,
                           subscriber_count, subscriber_hidden
                    FROM channel_snapshots WHERE channel_id IN ({marks})""",
                conn, params=channels,
            )
        else:
            ch = pd.DataFrame(columns=["channel_id", "channel_run_id", "channel_collected_at",
                                       "subscriber_count", "subscriber_hidden"])

    for col in ("published_at", "collected_at"):
        df[col] = pd.to_datetime(df[col], utc=True, errors="coerce")
    ch["channel_collected_at"] = pd.to_datetime(ch["channel_collected_at"], utc=True, errors="coerce")
    return _attach_subscribers(df, ch)


def _attach_subscribers(df: pd.DataFrame, ch: pd.DataFrame) -> pd.DataFrame:
    """영상 스냅샷과 같은 run의 채널 스냅샷을 우선, 없으면 수집 시각이 가장 가까운 것을 붙임."""
    cand = df[["keyword", "video_id", "channel_id", "snapshot_run_id", "collected_at"]].merge(
        ch, on="channel_id", how="inner")
    if not cand.empty:
        cand["_other_run"] = (cand["channel_run_id"] != cand["snapshot_run_id"]).astype(int)
        cand["_gap"] = (cand["channel_collected_at"] - cand["collected_at"]).abs()
        cand = (cand.sort_values(["_other_run", "_gap"])
                    .drop_duplicates(["keyword", "video_id"])
                    [["keyword", "video_id", "subscriber_count", "subscriber_hidden",
                      "channel_run_id", "channel_collected_at"]])
    else:
        cand = pd.DataFrame(columns=["keyword", "video_id", "subscriber_count", "subscriber_hidden",
                                     "channel_run_id", "channel_collected_at"])
    return df.merge(cand, on=["keyword", "video_id"], how="left")


def compute_metrics(df: pd.DataFrame, min_elapsed_days: float = config.MIN_ELAPSED_DAYS) -> pd.DataFrame:
    """지표와 제외 사유 컬럼을 추가한 사본을 반환. 계산할 수 없는 값은 NaN (0으로 대체하지 않음)."""
    df = df.copy()
    views = pd.to_numeric(df["view_count"], errors="coerce").astype(float)
    likes = pd.to_numeric(df["like_count"], errors="coerce").astype(float)
    comments = pd.to_numeric(df["comment_count"], errors="coerce").astype(float)
    subs = pd.to_numeric(df["subscriber_count"], errors="coerce").astype(float)
    hidden = df["subscriber_hidden"].fillna(0).astype(float) == 1

    # 조회수
    df["views"] = views
    df[REASON_COLUMNS["views"]] = np.where(views.isna(), REASONS["no_views"], None)

    # 참여율
    er_reason = np.select(
        [views.isna(), views == 0, likes.isna() & comments.isna(), likes.isna(), comments.isna()],
        [REASONS["no_views"], REASONS["zero_views"], REASONS["likes_and_comments_hidden"],
         REASONS["likes_hidden"], REASONS["comments_disabled"]],
        default=None,
    )
    df["engagement_rate"] = np.where(pd.isna(er_reason), (likes + comments) / views.where(views > 0), np.nan)
    df[REASON_COLUMNS["engagement_rate"]] = er_reason

    # 조회수 속도
    elapsed = (df["collected_at"] - df["published_at"]).dt.total_seconds() / 86400
    df["elapsed_days"] = elapsed
    df["elapsed_days_used"] = elapsed.clip(lower=min_elapsed_days)
    vpd_reason = np.select([views.isna(), elapsed.isna()],
                           [REASONS["no_views"], REASONS["no_published_at"]], default=None)
    df["views_per_day"] = np.where(pd.isna(vpd_reason), views / df["elapsed_days_used"], np.nan)
    df[REASON_COLUMNS["views_per_day"]] = vpd_reason

    # 구독자 대비 조회수
    vps_reason = np.select(
        [hidden, subs.isna(), subs == 0, views.isna()],
        [REASONS["subscribers_hidden"], REASONS["no_subscribers"], REASONS["zero_subscribers"],
         REASONS["no_views"]],
        default=None,
    )
    df["views_per_subscriber"] = np.where(pd.isna(vps_reason), views / subs.where(subs > 0), np.nan)
    df[REASON_COLUMNS["views_per_subscriber"]] = vps_reason

    df["url"] = "https://www.youtube.com/watch?v=" + df["video_id"]
    return df


def dedupe_videos(df: pd.DataFrame) -> pd.DataFrame:
    """영상 단위로 중복 제거. keywords 컬럼에 속한 키워드를 쉼표로 묶음."""
    if df.empty:
        return df.assign(keywords=pd.Series(dtype=str))
    kws = df.groupby("video_id")["keyword"].apply(lambda s: ", ".join(sorted(set(s))))
    out = df.drop_duplicates("video_id").set_index("video_id")
    out["keywords"] = kws
    return out.reset_index()


def _stats(group: pd.DataFrame) -> dict:
    row = {"video_count": int(group["video_id"].nunique())}
    for key in METRICS:
        values = group[key].dropna()
        row[f"{key}_mean"] = values.mean() if len(values) else np.nan
        row[f"{key}_median"] = values.median() if len(values) else np.nan
        row[f"{key}_n"] = int(len(values))
    vps = group["views_per_subscriber"].dropna()
    row["views_per_subscriber_over_1_share"] = (vps > 1).mean() if len(vps) else np.nan
    return row


def summary_by_keyword(df: pd.DataFrame, total_label: str = "전체") -> pd.DataFrame:
    """키워드별 요약 + 전체(영상 중복 제거) 행."""
    rows = [{"keyword": kw, **_stats(g)} for kw, g in df.groupby("keyword", sort=True)]
    if not df.empty:
        rows.append({"keyword": total_label, **_stats(dedupe_videos(df))})
    return pd.DataFrame(rows)


def summary_by_channel(df: pd.DataFrame) -> pd.DataFrame:
    """채널 단위 집계 (영상 중복 제거 후). 구독자 대비 조회수 중앙값 높은 순, 값 없음은 맨 아래.

    - subscriber_count: 그 채널의 가장 최근 채널 스냅샷 값 (비공개면 NaN)
    - 대표 영상: 구독자 대비 조회수가 가장 높은 영상 (모두 계산 불가면 조회수가 가장 높은 영상)
    """
    cols = ["channel_id", "channel_title", "subscriber_count", "subscriber_hidden", "video_count",
            "views_per_subscriber_median", "engagement_rate_median", "engagement_rate_n",
            "rep_video_id", "rep_title", "rep_url", "rep_views_per_subscriber", "single_video"]
    videos = dedupe_videos(df)
    videos = videos[videos["channel_id"].notna()]
    if videos.empty:
        return pd.DataFrame(columns=cols)
    rows = []
    for cid, g in videos.groupby("channel_id"):
        latest = g.sort_values("channel_collected_at", na_position="first").iloc[-1]
        rep_pool = g.dropna(subset=["views_per_subscriber"])
        rep = (rep_pool.loc[rep_pool["views_per_subscriber"].idxmax()] if not rep_pool.empty
               else g.loc[g["views"].fillna(-1).idxmax()])
        er = g["engagement_rate"].dropna()
        vps = g["views_per_subscriber"].dropna()
        rows.append({
            "channel_id": cid,
            "channel_title": latest["channel_title"],
            "subscriber_count": latest["subscriber_count"],
            "subscriber_hidden": bool(latest["subscriber_hidden"] == 1),
            "video_count": int(len(g)),
            "views_per_subscriber_median": vps.median() if len(vps) else np.nan,
            "engagement_rate_median": er.median() if len(er) else np.nan,
            "engagement_rate_n": int(len(er)),
            "rep_video_id": rep["video_id"],
            "rep_title": rep["title"],
            "rep_url": rep["url"],
            "rep_views_per_subscriber": rep["views_per_subscriber"],
            "single_video": len(g) == 1,
        })
    out = pd.DataFrame(rows, columns=cols)
    return out.sort_values("views_per_subscriber_median", ascending=False, na_position="last",
                           kind="stable").reset_index(drop=True)


def exclusion_summary(df: pd.DataFrame) -> pd.DataFrame:
    """지표 × 사유별 제외 영상 수 (영상 기준 중복 제거)."""
    videos = dedupe_videos(df)
    rows = []
    for key, label in METRICS.items():
        reasons = videos[REASON_COLUMNS[key]].dropna()
        for reason, count in reasons.value_counts().items():
            rows.append({"metric": label, "reason": reason, "count": int(count)})
        rows.append({"metric": label, "reason": "합계", "count": int(len(reasons))})
    return pd.DataFrame(rows, columns=["metric", "reason", "count"])


def excluded_count(df: pd.DataFrame, metric: str) -> int:
    """해당 지표 계산에서 제외된 영상 수 (영상 기준)."""
    return int(dedupe_videos(df)[REASON_COLUMNS[metric]].notna().sum())


def exclusion_text(df: pd.DataFrame, metric: str) -> str:
    """예: '계산 제외 6개 — 좋아요 숨김 5개, 댓글 사용 중지 1개' / 없으면 '제외 없음'."""
    reasons = dedupe_videos(df)[REASON_COLUMNS[metric]].dropna()
    if reasons.empty:
        return "제외 없음"
    detail = ", ".join(f"{reason} {count}개" for reason, count in reasons.value_counts().items())
    return f"계산 제외 {len(reasons)}개 — {detail}"


def data_basis(db_path: Path = config.DB_PATH, criteria: str = config.CRITERIA,
               df: pd.DataFrame | None = None) -> dict:
    """수집 기준, 실행 목록, 사용한 스냅샷 수집 시각 범위, 쇼츠 판정 기준."""
    with _connect(db_path) as conn:
        runs = conn.execute(
            "SELECT run_id, mode, criteria_json FROM runs WHERE criteria = ? AND status = 'ok' ORDER BY run_id",
            (criteria,),
        ).fetchall()
    search_runs = [r for r in runs if r[1] == "search"]
    basis = {
        "criteria": criteria,
        "search_criteria": json.loads(search_runs[-1][2]) if search_runs and search_runs[-1][2] else {},
        "runs": [{"run_id": r[0], "mode": r[1]} for r in runs],
        "used_search_run": search_runs[-1][0] if search_runs else None,   # 대시보드가 쓰는 실행
        "retention_days": config.RETENTION_DAYS,
        "short_rule": {
            "aspect": "정사각형 또는 세로 (높이 ≥ 너비)",
            "max_seconds": config.SHORT_MAX_SECONDS,
            "max_seconds_old": config.SHORT_MAX_SECONDS_OLD,
            "rule_change_at": config.SHORT_RULE_CHANGE_AT,
        },
        "min_elapsed_days": config.MIN_ELAPSED_DAYS,
    }
    if df is not None and not df.empty:
        basis["snapshot_from"] = df["collected_at"].min()
        basis["snapshot_to"] = df["collected_at"].max()
    return basis


def load(db_path: Path = config.DB_PATH, criteria: str = config.CRITERIA) -> pd.DataFrame:
    """load_videos + compute_metrics."""
    return compute_metrics(load_videos(db_path, criteria))
