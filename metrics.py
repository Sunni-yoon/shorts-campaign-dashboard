"""지표 계산 모듈 (대시보드·리포트 공용, Streamlit 의존 없음).

지표 정의는 CLAUDE.md를 따른다.
- 참여율 = (좋아요 + 댓글) / 조회수 — 좋아요·댓글이 비어 있으면 0으로 넣지 않고 제외
- 조회수 속도 = 조회수 / 업로드 후 경과 일수 — 경과 일수는 최소 config.MIN_ELAPSED_DAYS
- 구독자 대비 조회수 = 조회수 / 채널 구독자 수 — 구독자 비공개 채널은 제외
- 요약 통계는 평균(영상별 값의 단순 평균)과 중앙값을 함께 제시

대상: 수집 기준(criteria)의 가장 최근 성공 search 실행에서 나온 쇼츠.
지표는 영상별 가장 최근 스냅샷 (그 뒤의 refresh 포함).

데이터 단위: 행 = (키워드, 영상). 한 영상이 여러 키워드에 속하면 키워드마다 한 행.
(v3는 키워드가 buldak 하나라 행 = 영상. 대시보드 비교 기준은 포맷)
영상 단위 집계(전체 요약, 제외 수, 상위 영상)는 video_id로 중복을 제거한다.
"""
import json
import re
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
    "views_per_subscriber": f"조회수 ÷ 구독자 수 ({config.VPS_MIN_SUBSCRIBERS:,}명 이상 채널)",
}

# 지표 설명 — 긴 버전: 요약표 열 툴팁·데이터 기준 안내·리포트용
DESCRIPTIONS = {
    "video_count": f"이번 수집(키워드 {', '.join(config.KEYWORDS)})의 쇼츠 수 (영상 중복 제거)",
    "views": "영상들을 조회수 순으로 세웠을 때 가운데 값",
    "views_per_day": "조회수 ÷ 업로드 후 경과 일수, 업로드 후 지금까지의 하루 평균 조회수",
    "engagement_rate": "(좋아요 + 댓글) ÷ 조회수, 좋아요·댓글 숨김 영상 제외",
    "views_per_subscriber": (f"조회수 ÷ 채널 구독자 수, 1보다 크면 구독자보다 많이 봤다는 뜻. "
                             f"구독자 {config.VPS_MIN_SUBSCRIBERS:,}명 이상 채널의 영상만 계산. "
                             "구독자가 매우 적은 채널은 분모가 작아 값이 극단적으로 커짐"),
    # 포맷·댓글 분석
    "format": (f"제목·설명·해시태그로 LLM이 분류 ({' / '.join(config.FORMATS)}), 겹치면 이 순서로 우선"),
    "positive_rate": "영상별 분석 댓글 중 긍정으로 분류된 댓글 비율",
    "negative_rate": "영상별 분석 댓글 중 부정으로 분류된 댓글 비율 (제품·출연자·연출 등 대상은 구분하지 않음)",
    "purchase_rate": ('영상별 분석 댓글 중 "어디서 사요?", "먹어 보고 싶다"처럼 사거나 먹어 보고 싶다는 '
                      "표현이 있는 댓글 비율"),
    "comment_basis": (f"영상별 관련도 상위 댓글 최대 {config.COMMENTS_PER_VIDEO}개, "
                      f"분석 댓글이 {config.COMMENT_MIN_PER_VIDEO}개 미만인 영상은 제외"),
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
    "small_channel": f"구독자 {config.VPS_MIN_SUBSCRIBERS:,}명 미만 채널",
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
    WHERE criteria = :criteria AND mode = 'search' AND status = 'ok'
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
SELECT t.keyword, v.video_id, v.title, v.description, v.hashtags, v.published_at, v.duration_sec,
       v.embed_width, v.embed_height, v.channel_id, c.title AS channel_title,
       l.run_id AS snapshot_run_id, l.collected_at,
       l.view_count, l.like_count, l.comment_count,
       f.format, f.confidence AS format_confidence, f.reason AS format_reason, f.model AS format_model,
       f.agreement AS format_agreement, f.trial_formats AS format_trials
FROM targets t
JOIN videos v ON v.video_id = t.video_id
JOIN latest l ON l.video_id = t.video_id AND l.rn = 1
LEFT JOIN channels c ON c.channel_id = v.channel_id
LEFT JOIN video_formats f ON f.video_id = t.video_id AND f.prompt_version = :prompt_version
ORDER BY t.keyword, v.published_at
"""


def _connect(db_path: Path) -> sqlite3.Connection:
    # 읽기 전용으로 열어 대시보드가 DB를 바꾸지 않게 함
    return sqlite3.connect(f"file:{Path(db_path).as_posix()}?mode=ro", uri=True)


def load_videos(db_path: Path = config.DB_PATH, criteria: str = config.CRITERIA) -> pd.DataFrame:
    """수집 기준에 해당하는 쇼츠의 최신 스냅샷 + 가장 가까운 시점의 구독자 수."""
    with _connect(db_path) as conn:
        df = pd.read_sql_query(_VIDEOS_SQL, conn, params={
            "criteria": criteria, "prompt_version": config.FORMAT_PROMPT_VERSION})
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
        [hidden, subs.isna(), subs == 0, subs < config.VPS_MIN_SUBSCRIBERS, views.isna()],
        [REASONS["subscribers_hidden"], REASONS["no_subscribers"], REASONS["zero_subscribers"],
         REASONS["small_channel"], REASONS["no_views"]],
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


def main_formats(videos: pd.DataFrame) -> list[str]:
    """채널 영상 중 가장 많은 포맷 (동률이면 모두, 정의된 포맷 순서). 기타·미분류도 그대로."""
    labels = _format_labels(videos)
    if labels.empty:
        return []
    counts = labels.value_counts()
    top = counts[counts == counts.max()].index
    return [f for f in [*config.FORMATS, UNCLASSIFIED] if f in top]


def summary_by_channel(df: pd.DataFrame) -> pd.DataFrame:
    """채널 단위 집계 (영상 중복 제거 후). 구독자 대비 조회수 중앙값 높은 순, 값 없음은 맨 아래.

    - subscriber_count: 그 채널의 가장 최근 채널 스냅샷 값 (비공개면 NaN)
    - 대표 영상: 구독자 대비 조회수가 가장 높은 영상 (모두 계산 불가면 조회수가 가장 높은 영상)
    """
    cols = ["channel_id", "channel_title", "subscriber_count", "subscriber_hidden", "video_count",
            "views_per_subscriber_median", "engagement_rate_median", "engagement_rate_n",
            "rep_video_id", "rep_title", "rep_url", "rep_views_per_subscriber", "single_video", "main_formats"]
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
            "main_formats": main_formats(g),
        })
    out = pd.DataFrame(rows, columns=cols)
    return out.sort_values("views_per_subscriber_median", ascending=False, na_position="last",
                           kind="stable").reset_index(drop=True)


UNCLASSIFIED = "미분류"


def _format_labels(df: pd.DataFrame) -> pd.Series:
    col = df["format"] if "format" in df else pd.Series(index=df.index, dtype=object)
    return col.fillna(UNCLASSIFIED)


def summary_by_format(df: pd.DataFrame, min_sample: int = config.FORMAT_MIN_SAMPLE,
                      total_label: str | None = None) -> pd.DataFrame:
    """포맷별 요약 (영상 중복 제거). 정의된 포맷 순서대로, 미분류는 있으면 마지막.

    share: 분류된 영상 중 비율 (미분류·전체 행은 NaN) / sample_insufficient: 영상 수 < min_sample
    total_label을 주면 맨 아래에 전체(미분류 포함) 행 추가 — 포맷별 값을 비교할 기준선
    """
    videos = dedupe_videos(df)
    videos = videos.assign(format=_format_labels(videos))
    classified = int((videos["format"] != UNCLASSIFIED).sum())
    order = list(config.FORMATS) + ([UNCLASSIFIED] if (videos["format"] == UNCLASSIFIED).any() else [])
    rows = []
    for fmt in order:
        g = videos[videos["format"] == fmt]
        stats = _stats(g) if not g.empty else {"video_count": 0, **{
            f"{k}_{s}": (0 if s == "n" else np.nan) for k in METRICS for s in ("mean", "median", "n")},
            "views_per_subscriber_over_1_share": np.nan}
        rows.append({
            "format": fmt, **stats,
            "share": (len(g) / classified if classified else np.nan) if fmt != UNCLASSIFIED else np.nan,
            "sample_insufficient": fmt != UNCLASSIFIED and len(g) < min_sample,
        })
    if total_label is not None and not videos.empty:
        rows.append({"format": total_label, **_stats(videos), "share": np.nan, "sample_insufficient": False})
    return pd.DataFrame(rows)


def keyword_format_table(df: pd.DataFrame) -> pd.DataFrame:
    """키워드 × 포맷 영상 수. 한 영상이 여러 키워드에 속하면 키워드마다 셈."""
    labels = _format_labels(df)
    table = pd.crosstab(df["keyword"], labels)
    columns = list(config.FORMATS) + ([UNCLASSIFIED] if UNCLASSIFIED in table.columns else [])
    table = table.reindex(columns=columns, fill_value=0)
    table["합계"] = table.sum(axis=1)
    return table


def other_group(row) -> str:
    """기타 영상 한 개의 구성 그룹 (config.OTHER_GROUPS를 위에서부터 적용)."""
    title = str(row.get("title") or "").lower()
    text = " ".join(str(row.get(k) or "") for k in ("title", "description", "hashtags")).lower()
    for name, field, pattern in config.OTHER_GROUPS:
        if field == "low_confidence":
            if row.get("format_confidence") == "낮음":
                return name
        elif re.search(pattern, title if field == "title" else text):
            return name
    return config.OTHER_GROUP_REST


def other_breakdown(df: pd.DataFrame) -> dict[str, int]:
    """기타로 분류된 영상(중복 제거)의 구성 그룹별 개수 — 정의된 그룹 순서, 0개도 포함."""
    videos = dedupe_videos(df)
    other = videos[_format_labels(videos) == "기타"]
    counts = other.apply(other_group, axis=1).value_counts() if not other.empty else pd.Series(dtype=int)
    names = [g[0] for g in config.OTHER_GROUPS] + [config.OTHER_GROUP_REST]
    return {n: int(counts.get(n, 0)) for n in names}


def other_breakdown_text(df: pd.DataFrame) -> str:
    """예: '종이 DIY 17 · 랭킹·모음 6 · 밈 5 · 판단 불가 (제목·설명에 정보 부족) 7 · 그 외 5 (기타 40개)'."""
    b = other_breakdown(df)
    return " · ".join(f"{k} {v}" for k, v in b.items()) + f" (기타 {sum(b.values())}개)"


def boundary_label(agreement, trials: int | None = None) -> str:
    """경계 사례 표시: 회차 결과가 모두 같지 않으면 '경계 (2/3)' 또는 '경계 (3회 모두 다름)'. 단일 분류(NULL)는 빈 칸."""
    trials = trials or config.FORMAT_TRIALS
    if agreement is None or pd.isna(agreement) or int(agreement) >= trials:
        return ""
    n = int(agreement)
    return f"경계 ({trials}회 모두 다름)" if n < 2 else f"경계 ({n}/{trials})"


def agreement_text(df: pd.DataFrame) -> str:
    """예: '포맷 분류 3회 일치 170개 (85.0%) · 경계 사례 30개 (2/3 일치 28 · 모두 다름 2)'."""
    videos = dedupe_videos(df)
    a = pd.to_numeric(videos.get("format_agreement"), errors="coerce").dropna()
    if a.empty:
        return "회차 분류 기록 없음"
    t = config.FORMAT_TRIALS
    full, tie = int((a >= t).sum()), int((a < 2).sum())
    return (f"포맷 분류 {t}회 일치 {full}개 ({full / len(a):.1%}) · 경계 사례 {len(a) - full}개 "
            f"({t - 1}/{t} 일치 {len(a) - full - tie} · 모두 다름 {tie})")


EXCLUDED_NOTE = "비교 제외 — 내용이 섞인 묶음 (데이터 기준 참고)"


def format_note(fmt: str, sample_insufficient: bool = False) -> str:
    """포맷 비교 표의 비고: 비교 제외 포맷 > 표본 부족 > 빈 칸."""
    if fmt == config.FORMAT_EXCLUDED_FROM_COMPARISON:
        return EXCLUDED_NOTE
    return "표본 부족" if sample_insufficient else ""


def chart_formats() -> list[str]:
    """그래프에 그리는 포맷 (정의된 순서, 비교 제외 포맷 빼고)."""
    return [f for f in config.FORMATS if f != config.FORMAT_EXCLUDED_FROM_COMPARISON]


# 포맷 비교 그래프: (지표 키, 제목, 통계) — 구매의향은 중앙값이 대부분 0이라 평균
COMPARISON_METRICS = [
    ("views_per_day", "조회수 속도 (중앙값, 회/일)", "median"),
    ("views_per_subscriber", "구독자 대비 조회수 (중앙값)", "median"),
    ("engagement_rate", "참여율 (중앙값)", "median"),
    ("purchase_rate", "구매의향 비율 (평균)", "mean"),
]


def format_comparison(df: pd.DataFrame) -> pd.DataFrame:
    """포맷 비교 그래프용 긴 표: metric, title, format, value, n, is_top (최댓값, 동률이면 모두 True)."""
    fsum = summary_by_format(df).set_index("format")
    react = summary_reactions(df, by="format")
    react = react.set_index("group") if not react.empty else react
    rows = []
    for key, title, stat in COMPARISON_METRICS:
        for f in chart_formats():
            if key in REACTION_RATES:
                ok = not react.empty and f in react.index
                value = react.loc[f, f"{key}_{stat}"] if ok else np.nan
                n = int(react.loc[f, "videos_analyzed"]) if ok else 0
            else:
                ok = f in fsum.index
                value = fsum.loc[f, f"{key}_{stat}"] if ok else np.nan
                n = int(fsum.loc[f, f"{key}_n"]) if ok else 0
            rows.append({"metric": key, "title": title, "format": f, "value": value, "n": n})
    out = pd.DataFrame(rows, columns=["metric", "title", "format", "value", "n"])
    best = out.groupby("metric")["value"].transform("max")
    out["is_top"] = out["value"].notna() & np.isclose(out["value"], best)
    return out


def sentiment_mix(df: pd.DataFrame) -> pd.DataFrame:
    """포맷별 감성 구성 (영상별 비율의 평균). 중립 = 1 − 긍정 − 부정 (감성은 셋 중 하나). n = 분석 영상 수."""
    react = summary_reactions(df, by="format")
    rows = []
    for f in chart_formats():
        r = react[react["group"] == f] if not react.empty else react
        if r.empty or r.iloc[0]["videos_analyzed"] == 0:
            continue
        r = r.iloc[0]
        pos, neg = r["positive_rate_mean"], r["negative_rate_mean"]
        rows.append({"format": f, "긍정": pos, "중립": max(0.0, 1 - pos - neg), "부정": neg,
                     "n": int(r["videos_analyzed"])})
    return pd.DataFrame(rows, columns=["format", "긍정", "중립", "부정", "n"])


def creator_scatter_data(df: pd.DataFrame, channel_ids=None) -> pd.DataFrame:
    """크리에이터 산점도용 영상: 비교 제외 포맷·미분류, 구독자 비공개·VPS_MIN_SUBSCRIBERS 미만·조회수 0 제외."""
    v = dedupe_videos(df)
    v = v.assign(format_label=_format_labels(v))
    keep = (v["format_label"].isin(chart_formats()) & v["subscriber_count"].notna()
            & (v["subscriber_count"] >= config.VPS_MIN_SUBSCRIBERS) & (v["views"] > 0))
    if "subscriber_hidden" in v:
        keep &= v["subscriber_hidden"].fillna(0) != 1
    if channel_ids is not None:
        keep &= v["channel_id"].isin(set(channel_ids))
    return v[keep]


# ---------------------------------------------------------------- 부트스트랩 신뢰구간 (포맷 비교)
# (지표 키, 통계) — 포맷 비교 표·그래프와 같은 통계. 구매의향은 중앙값이 대부분 0이라 평균
BOOT_METRICS = [
    ("views", "median"), ("views_per_day", "median"), ("engagement_rate", "median"),
    ("views_per_subscriber", "median"), ("positive_rate", "median"), ("negative_rate", "median"),
    ("purchase_rate", "mean"),
]
BOOT_N, BOOT_SEED = 10_000, 42
BOOT_NOTE = "마우스를 올리면 95% 신뢰구간(부트스트랩 1만 회)이 보임"
VERDICT_SURE = "확실한 차이"          # 보정(Bonferroni) 구간도 0을 포함하지 않음
VERDICT_MAYBE = "우연일 수 있는 차이"  # 95% 구간은 0을 포함하지 않지만 보정 구간은 포함
VERDICT_NONE = "차이 확인 안 됨"      # 95% 구간이 0을 포함


def _boot_stat(sample: np.ndarray, stat: str) -> np.ndarray:
    """(B, m) 표본 행렬 → 행별 통계. 채널 단위는 빈 칸을 NaN으로 채우므로 nan 함수 사용."""
    return np.nanmedian(sample, axis=1) if stat == "median" else np.nanmean(sample, axis=1)


def bootstrap_samples(df: pd.DataFrame, n_boot: int = BOOT_N, seed: int = BOOT_SEED,
                      unit: str = "video") -> dict:
    """{(지표, 포맷): (점추정, 부트스트랩 통계 배열, n)}. 포맷마다 따로, 그 지표 값이 있는 영상만 다시 뽑음.

    unit='video': 영상을 복원 추출 / unit='channel': 채널을 복원 추출하고 뽑힌 채널의 영상을 모두 포함
    난수는 (seed, 지표 순번, 포맷 순번)으로 고정 → 일부만 계산해도 같은 결과
    """
    videos = dedupe_videos(df)
    videos = videos.assign(format_label=_format_labels(videos))
    out = {}
    for mi, (key, stat) in enumerate(BOOT_METRICS):
        if key not in videos:
            continue
        for fi, f in enumerate(chart_formats()):
            g = videos[(videos["format_label"] == f) & videos[key].notna()]
            values = g[key].to_numpy(dtype=float)
            n = len(values)
            if n == 0:
                out[(key, f)] = (np.nan, np.full(n_boot, np.nan), 0)
                continue
            point = float(np.median(values) if stat == "median" else np.mean(values))
            rng = np.random.default_rng([seed, mi, fi])
            if unit == "channel":
                groups = [grp[key].to_numpy(dtype=float)
                          for _, grp in g.groupby(g["channel_id"].fillna(g["video_id"]))]
                width = max(len(x) for x in groups)
                padded = np.full((len(groups), width), np.nan)
                for i, x in enumerate(groups):
                    padded[i, : len(x)] = x
                pick = rng.integers(0, len(groups), size=(n_boot, len(groups)))
                sample = padded[pick].reshape(n_boot, -1)
            else:
                sample = values[rng.integers(0, n, size=(n_boot, n))]
            out[(key, f)] = (point, _boot_stat(sample, stat), n)
    return out


def bootstrap_format_ci(df: pd.DataFrame, n_boot: int = BOOT_N, seed: int = BOOT_SEED,
                        alpha: float = 0.05, unit: str = "video", samples: dict | None = None) -> pd.DataFrame:
    """지표 × 포맷의 점추정과 (1−alpha) 백분위 신뢰구간: metric, format, value, low, high, n."""
    samples = samples if samples is not None else bootstrap_samples(df, n_boot, seed, unit)
    rows = []
    for (key, f), (point, boot, n) in samples.items():
        ok = n > 0 and np.isfinite(boot).any()
        lo, hi = (np.nanquantile(boot, [alpha / 2, 1 - alpha / 2]) if ok else (np.nan, np.nan))
        rows.append({"metric": key, "format": f, "value": point, "low": lo, "high": hi, "n": n})
    return pd.DataFrame(rows, columns=["metric", "format", "value", "low", "high", "n"])


def bootstrap_format_diffs(df: pd.DataFrame, n_boot: int = BOOT_N, seed: int = BOOT_SEED,
                           alpha: float = 0.05, unit: str = "video", samples: dict | None = None,
                           n_comparisons: int | None = None) -> pd.DataFrame:
    """포맷 쌍(정의 순서 a−b)마다 통계량 차이의 신뢰구간과 0 포함 여부.

    adj_*: 여러 번 비교 보정(Bonferroni, alpha / 비교 수) 구간. 비교 수 기본값 = 지표 수 × 포맷 쌍 수
    verdict: 확실한 차이(보정 구간도 0 미포함) / 우연일 수 있는 차이(95%만 0 미포함) / 차이 확인 안 됨
    """
    samples = samples if samples is not None else bootstrap_samples(df, n_boot, seed, unit)
    fmts = chart_formats()
    pairs = [(fmts[i], fmts[j]) for i in range(len(fmts)) for j in range(i + 1, len(fmts))]
    metrics_ = list(dict.fromkeys(k for k, _ in samples))
    m = n_comparisons or len(metrics_) * len(pairs)
    adj = alpha / m
    rows = []
    for key in metrics_:
        for a, b in pairs:
            pa, ba, na = samples[(key, a)]
            pb, bb, nb = samples[(key, b)]
            d = ba - bb
            ok = na > 0 and nb > 0 and np.isfinite(d).any()
            lo, hi = np.nanquantile(d, [alpha / 2, 1 - alpha / 2]) if ok else (np.nan, np.nan)
            alo, ahi = np.nanquantile(d, [adj / 2, 1 - adj / 2]) if ok else (np.nan, np.nan)
            zero = not ok or lo <= 0 <= hi
            adj_zero = not ok or alo <= 0 <= ahi
            rows.append({"metric": key, "a": a, "b": b, "diff": pa - pb, "low": lo, "high": hi,
                         "includes_zero": zero, "adj_low": alo, "adj_high": ahi, "adj_includes_zero": adj_zero,
                         "verdict": VERDICT_NONE if zero else (VERDICT_MAYBE if adj_zero else VERDICT_SURE)})
    return pd.DataFrame(rows)


SURE_RULE = ("그래프 아래 '확실한 차이'는 채널 단위 부트스트랩(1만 회)에서 21번 비교를 보정한 구간도 "
             "0을 포함하지 않는 포맷 쌍만 표시 (막대 높이만으로 순위를 정하지 않음)")
NO_SURE_LABEL = "포맷 간 확실한 차이 없음"


def sure_pairs(diffs: pd.DataFrame, metric: str) -> list[tuple[str, str]]:
    """해당 지표에서 '확실한 차이'인 포맷 쌍 [(높은 포맷, 낮은 포맷)], 정의된 포맷 쌍 순서.

    diffs: bootstrap_format_diffs 결과 (대시보드는 채널 단위). diff = a − b 이므로 부호로 방향을 정함
    """
    d = diffs[(diffs["metric"] == metric) & (diffs["verdict"] == VERDICT_SURE)]
    return [(r.a, r.b) if r.diff > 0 else (r.b, r.a) for r in d.itertuples()]


def sure_pairs_text(diffs: pd.DataFrame, metric: str, short: dict | None = None) -> str:
    """예: '확실한 차이: 챌린지 > 먹방' / 없으면 '포맷 간 확실한 차이 없음'."""
    name = (short or {}).get
    pairs = sure_pairs(diffs, metric)
    if not pairs:
        return NO_SURE_LABEL
    return "확실한 차이: " + " · ".join(f"{name(hi) or hi} > {name(lo) or lo}" for hi, lo in pairs)


def other_share(df: pd.DataFrame) -> tuple[float, int, int]:
    """(기타 비율, 기타 수, 분류된 영상 수) — 영상 중복 제거, 미분류 제외."""
    labels = _format_labels(dedupe_videos(df))
    classified = labels[labels != UNCLASSIFIED]
    other = int((classified == "기타").sum())
    return (other / len(classified) if len(classified) else np.nan), other, int(len(classified))


# ---------------------------------------------------------------- 댓글 반응
REACTION_RATES = {"positive_rate": "긍정 비율", "negative_rate": "부정 비율", "purchase_rate": "구매의향 비율"}
REACTION_REASONS = {
    "not_fetched": "댓글 미수집",
    "disabled": "댓글 사용 중지",
    "no_comments": "댓글 없음",
    "error": "댓글 수집 오류",
    "too_few": f"분석 댓글 {config.COMMENT_MIN_PER_VIDEO}개 미만",
}


def load_video_reactions(db_path: Path = config.DB_PATH) -> pd.DataFrame:
    """영상별 댓글 수집 상태와 분류 집계 (분석 댓글 수, 긍정·부정·구매의향 댓글 수)."""
    with _connect(db_path) as conn:
        return pd.read_sql_query(
            """
            SELECT cf.video_id, cf.status AS comment_status,
                   COUNT(r.comment_key) AS comments_analyzed,
                   SUM(CASE WHEN r.sentiment = '긍정' THEN 1 ELSE 0 END) AS positive,
                   SUM(CASE WHEN r.sentiment = '부정' THEN 1 ELSE 0 END) AS negative,
                   SUM(CASE WHEN r.purchase_intent = '있음' THEN 1 ELSE 0 END) AS purchase
            FROM comment_fetches cf
            LEFT JOIN video_comments c ON c.video_id = cf.video_id
            LEFT JOIN comment_reactions r ON r.comment_key = c.comment_key AND r.prompt_version = ?
            GROUP BY cf.video_id, cf.status
            """,
            conn, params=[config.COMMENT_PROMPT_VERSION],
        )


def attach_reactions(df: pd.DataFrame, reactions: pd.DataFrame,
                     min_comments: int = config.COMMENT_MIN_PER_VIDEO) -> pd.DataFrame:
    """영상별 비율 = 해당 댓글 수 / 분석 댓글 수. 분석 댓글이 min_comments개 미만이면 NaN + 제외 사유."""
    out = df.drop(columns=[c for c in ("comment_status", "comments_analyzed", *REACTION_RATES,
                                       "reaction_excluded_reason") if c in df.columns])
    out = out.merge(reactions[["video_id", "comment_status", "comments_analyzed", "positive", "negative",
                               "purchase"]], on="video_id", how="left")
    out["comment_status"] = out["comment_status"].fillna("not_fetched")
    out["comments_analyzed"] = pd.to_numeric(out["comments_analyzed"], errors="coerce").fillna(0).astype(int)
    for src in ("positive", "negative", "purchase"):
        out[src] = pd.to_numeric(out[src], errors="coerce")
    enough = out["comments_analyzed"] >= min_comments
    for col, src in (("positive_rate", "positive"), ("negative_rate", "negative"), ("purchase_rate", "purchase")):
        out[col] = np.where(enough, out[src] / out["comments_analyzed"].where(out["comments_analyzed"] > 0), np.nan)
    status_reason = out["comment_status"].map(
        {"not_fetched": REACTION_REASONS["not_fetched"], "disabled": REACTION_REASONS["disabled"],
         "no_comments": REACTION_REASONS["no_comments"], "error": REACTION_REASONS["error"]})
    out["reaction_excluded_reason"] = np.where(
        enough, None, status_reason.where(out["comment_status"] != "ok", REACTION_REASONS["too_few"]))
    return out.drop(columns=["positive", "negative", "purchase"])


def _reaction_stats(g: pd.DataFrame) -> dict:
    inc = g[g["positive_rate"].notna()]
    reasons = g["reaction_excluded_reason"].value_counts()
    row = {"video_count": int(len(g)), "videos_analyzed": int(len(inc)),
           "comments_analyzed": int(inc["comments_analyzed"].sum()),
           "excluded_too_few": int(reasons.get(REACTION_REASONS["too_few"], 0)),
           "excluded_no_comments": int(reasons.get(REACTION_REASONS["disabled"], 0)
                                       + reasons.get(REACTION_REASONS["no_comments"], 0)),
           "excluded_other": int(reasons.get(REACTION_REASONS["not_fetched"], 0)
                                 + reasons.get(REACTION_REASONS["error"], 0))}
    for col in REACTION_RATES:
        row[f"{col}_mean"] = inc[col].mean() if len(inc) else np.nan
        row[f"{col}_median"] = inc[col].median() if len(inc) else np.nan
    return row


def summary_reactions(df: pd.DataFrame, by: str = "keyword", total_label: str = "전체") -> pd.DataFrame:
    """키워드별(by='keyword') 또는 포맷별(by='format') 반응 요약 — 영상별 비율의 평균·중앙값."""
    if "positive_rate" not in df.columns:
        return pd.DataFrame()
    if by == "format":
        videos = dedupe_videos(df).assign(format=lambda d: _format_labels(d))
        order = [f for f in [*config.FORMATS, UNCLASSIFIED] if (videos["format"] == f).any()]
        rows = [{"group": f, **_reaction_stats(videos[videos["format"] == f])} for f in order]
    else:
        rows = [{"group": kw, **_reaction_stats(g)} for kw, g in df.groupby("keyword", sort=True)]
    if not df.empty:
        rows.append({"group": total_label, **_reaction_stats(dedupe_videos(df))})
    return pd.DataFrame(rows)


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
    """load_videos + compute_metrics + 영상별 댓글 반응 비율."""
    df = compute_metrics(load_videos(db_path, criteria))
    return attach_reactions(df, load_video_reactions(db_path))
