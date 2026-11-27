"""섹션별 핵심 관찰 한 줄 생성 (대시보드·리포트 공용).

metrics.py가 계산한 결과만 받아 숫자로 확인되는 사실을 문장으로 만든다.
해석("좋다", "유망") 이나 추천은 넣지 않는다.
"""
import pandas as pd

import config
import metrics

LABELS = {
    "views": "조회수",
    "views_per_day": "조회수 속도",
    "engagement_rate": "참여율",
    "views_per_subscriber": "구독자 대비 조회수",
}


def fmt(key: str, value) -> str:
    """지표별 표기: 조회수 1,234 / 조회수 속도 1,234회/일 / 참여율 1.23% / 구독자 대비 1.23."""
    if value is None or pd.isna(value):
        return "-"
    if key == "engagement_rate":
        return f"{value:.2%}"
    if key == "views_per_subscriber":
        return f"{value:.2f}"
    if key == "views_per_day":
        return f"{value:,.0f}회/일"
    return f"{value:,.0f}"


def _short(text: str, n: int = 40) -> str:
    text = str(text or "")
    return text if len(text) <= n else text[: n - 1] + "…"


def baseline(df: pd.DataFrame) -> str:
    v = metrics.dedupe_videos(df)
    n_er = int(v["engagement_rate"].notna().sum())
    return (f"쇼츠 {len(v)}개 · 조회수 중앙값 {fmt('views', v['views'].median())} · "
            f"조회수 속도 중앙값 {fmt('views_per_day', v['views_per_day'].median())} · "
            f"참여율 중앙값 {fmt('engagement_rate', v['engagement_rate'].median())} (계산 가능 {n_er}개) · "
            f"구독자 대비 조회수 중앙값 {fmt('views_per_subscriber', v['views_per_subscriber'].median())}")


def topics(summary: pd.DataFrame, total_label: str = "전체") -> str:
    s = summary[summary["keyword"] != total_label]
    if s.empty:
        return "선택한 키워드가 없음."
    if len(s) == 1:
        r = s.iloc[0]
        return (f"{r['keyword']} 1개 키워드: 조회수 중앙값 {fmt('views', r['views_median'])} · "
                f"참여율 중앙값 {fmt('engagement_rate', r['engagement_rate_median'])} · "
                f"구독자 대비 조회수 중앙값 {fmt('views_per_subscriber', r['views_per_subscriber_median'])}")
    parts = []
    vps = s.dropna(subset=["views_per_subscriber_median"])
    if not vps.empty:
        hi = vps.loc[vps["views_per_subscriber_median"].idxmax()]
        lo = vps.loc[vps["views_per_subscriber_median"].idxmin()]
        parts.append(f"구독자 대비 조회수 중앙값 최고 {hi['keyword']} "
                     f"{fmt('views_per_subscriber', hi['views_per_subscriber_median'])}, "
                     f"최저 {lo['keyword']} {fmt('views_per_subscriber', lo['views_per_subscriber_median'])}")
    for key in ("views", "engagement_rate"):
        col = s.dropna(subset=[f"{key}_median"])
        if not col.empty:
            hi = col.loc[col[f"{key}_median"].idxmax()]
            parts.append(f"{LABELS[key]} 중앙값 최고 {hi['keyword']} {fmt(key, hi[f'{key}_median'])}")
    return " · ".join(parts)


def creators(channels: pd.DataFrame) -> str:
    if channels.empty:
        return "조건에 맞는 채널이 없음."
    multi = int((channels["video_count"] >= 2).sum())
    over1 = int((channels["views_per_subscriber_median"] > 1).sum())
    text = (f"채널 {len(channels)}개 (영상 2개 이상 {multi}개) · "
            f"구독자 대비 조회수 중앙값이 1을 넘는 채널 {over1}개")
    top = channels.dropna(subset=["views_per_subscriber_median"])
    if not top.empty:
        r = top.iloc[0]
        subs = "비공개" if pd.isna(r["subscriber_count"]) else f"{r['subscriber_count']:,.0f}명"
        text += (f" · 1위 {_short(r['channel_title'], 30)} "
                 f"{fmt('views_per_subscriber', r['views_per_subscriber_median'])} "
                 f"(구독자 {subs}, 영상 {r['video_count']}개)")
    return text


def references(top: pd.DataFrame, sort_key: str) -> str:
    if top.empty:
        return "조건에 맞는 영상이 없음."
    valid = top.dropna(subset=[sort_key])
    if valid.empty:
        return f"{LABELS[sort_key]}를 계산할 수 있는 영상이 없음."
    r = valid.iloc[0]
    counts = metrics._format_labels(top).value_counts()
    by_fmt = ", ".join(f"{f} {n}개" for f, n in counts.items())
    return (f"{LABELS[sort_key]} 1위: {_short(r['title'])} ({fmt(sort_key, r[sort_key])}) · "
            f"표시된 {len(top)}개의 포맷 분포: {by_fmt}")


def distribution(df: pd.DataFrame) -> str:
    er = df.dropna(subset=["engagement_rate"])
    if er.empty:
        return "참여율을 계산할 수 있는 영상이 없음."
    v = metrics.dedupe_videos(er)
    text = (f"참여율 범위 {fmt('engagement_rate', v['engagement_rate'].min())} ~ "
            f"{fmt('engagement_rate', v['engagement_rate'].max())} · "
            f"조회수 범위 {fmt('views', v['views'].min())} ~ {fmt('views', v['views'].max())}")
    med = v.assign(format=metrics._format_labels(v)).groupby("format")["engagement_rate"].median()
    if len(med) >= 2:
        text += (f" · 포맷별 참여율 중앙값 차이 최대 {(med.max() - med.min()) * 100:.2f}%p "
                 f"({med.idxmax()} {fmt('engagement_rate', med.max())} vs "
                 f"{med.idxmin()} {fmt('engagement_rate', med.min())})")
    return text


def formats(fsum: pd.DataFrame, other: tuple, total_label: str = "전체") -> str:
    """포맷 섹션 관찰: 가장 많은 포맷, 기타 비율, 표본 충분한 포맷 중 구독자 대비 조회수 중앙값 최고·최저."""
    classified = fsum[~fsum["format"].isin([metrics.UNCLASSIFIED, total_label])]
    total = int(classified["video_count"].sum())
    if total == 0:
        return "분류된 영상이 없음."
    top = classified.loc[classified["video_count"].idxmax()]
    share, n_other, n_cls = other
    parts = [f"분류 {total}개 중 가장 많은 포맷 {top['format']} {top['video_count']}개({top['share']:.0%})",
             f"기타 비율 {share:.1%} ({n_other}/{n_cls})"]
    enough = classified[~classified["sample_insufficient"]
                        & (classified["format"] != config.FORMAT_EXCLUDED_FROM_COMPARISON)
                        ].dropna(subset=["views_per_subscriber_median"])
    if len(enough) >= 2:
        hi = enough.loc[enough["views_per_subscriber_median"].idxmax()]
        lo = enough.loc[enough["views_per_subscriber_median"].idxmin()]
        parts.append(f"표본 충분한 포맷 중(기타 제외) 구독자 대비 조회수 중앙값 최고 {hi['format']} "
                     f"{fmt('views_per_subscriber', hi['views_per_subscriber_median'])}, "
                     f"최저 {lo['format']} {fmt('views_per_subscriber', lo['views_per_subscriber_median'])}")
    unclassified = fsum[fsum["format"] == metrics.UNCLASSIFIED]
    if not unclassified.empty:
        parts.append(f"미분류 {int(unclassified['video_count'].iloc[0])}개")
    return " · ".join(parts)


def reactions(kw_summary: pd.DataFrame, total_label: str = "전체") -> str:
    """반응 섹션 관찰: 분석 규모, 전체 중앙값, 긍정 비율 중앙값 최고·최저 그룹(포맷 또는 키워드)."""
    if kw_summary.empty:
        return "분석한 댓글이 없음."
    total = kw_summary[kw_summary["group"] == total_label]
    if total.empty or total.iloc[0]["videos_analyzed"] == 0:
        return "분석 댓글이 충분한 영상이 없음."
    t = total.iloc[0]
    parts = [f"영상 {t['videos_analyzed']}개(댓글 {t['comments_analyzed']:,}개) 분석",
             f"영상별 긍정 비율 중앙값 {t['positive_rate_median']:.1%} · 부정 {t['negative_rate_median']:.1%} · "
             f"구매의향 {t['purchase_rate_median']:.1%}"]
    kws = kw_summary[~kw_summary["group"].isin([total_label, metrics.UNCLASSIFIED,
                                                 config.FORMAT_EXCLUDED_FROM_COMPARISON])].dropna(subset=["positive_rate_median"])
    if len(kws) >= 2:
        hi = kws.loc[kws["positive_rate_median"].idxmax()]
        lo = kws.loc[kws["positive_rate_median"].idxmin()]
        parts.append(f"긍정 비율 중앙값 최고 {hi['group']} {hi['positive_rate_median']:.1%}, "
                     f"최저 {lo['group']} {lo['positive_rate_median']:.1%}")
    return " · ".join(parts)
