"""숏폼 캠페인 성과 대시보드 — 결정 질문 순서로 구성.

실행: venv\\Scripts\\python.exe -m streamlit run dashboard.py
지표 계산은 전부 metrics.py, 섹션별 관찰 문장은 observations.py에서 한다 (리포트와 공용).
앱을 열 때 데이터가 없거나 24시간이 지났으면 auto_collect.py가 새로 검색한다.
API 키: 로컬은 .env, 배포(Streamlit Community Cloud)는 st.secrets — keys.py 참고.
"""
import math

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from plotly.subplots import make_subplots

import auto_collect
import classify_formats
import collect
import config
import keys
import metrics
import observations as obs

SERIES_COLOR = "#257de0"   # 단일 계열 색 (키워드는 색이 아니라 축·패널 제목으로 구분)
COLUMN_LABELS = {
    "views": "조회수",
    "engagement_rate": "참여율",
    "views_per_day": "조회수 속도(회/일)",
    "views_per_subscriber": "구독자 대비 조회수",
}
COLUMN_FORMATS = {"views": "localized", "views_per_day": "localized",
                  "engagement_rate": "percent", "views_per_subscriber": "%.2f"}

# 크리에이터 표: 구독자 수 범위 필터 단계
SUB_STEPS = [0, 1_000, 10_000, 100_000, 1_000_000, 10_000_000, math.inf]
SUB_LABELS = {0: "0", 1_000: "1천", 10_000: "1만", 100_000: "10만",
              1_000_000: "100만", 10_000_000: "1,000만", math.inf: "최대"}
SUB_DEFAULT = (1_000, math.inf)

st.set_page_config(page_title="숏폼 캠페인 성과 대시보드", layout="wide")


@st.cache_data
def load_data(db_mtime: float) -> tuple[pd.DataFrame, dict]:
    df = metrics.load(config.DB_PATH, config.CRITERIA)
    return df, metrics.data_basis(config.DB_PATH, config.CRITERIA, df)


def section(title: str, observation: str) -> None:
    """섹션 제목(결정 질문) + 숫자로 확인되는 핵심 관찰 한 줄."""
    st.subheader(title)
    st.markdown(f"**관찰** · {observation}")


@st.cache_resource
def collector_state() -> auto_collect.CollectorState:
    """프로세스 전체에서 1개: 동시 접속 시 수집 잠금 + 실패 후 재시도 대기 시각."""
    return auto_collect.CollectorState()


def _fmt_run(run_id: str | None) -> str:
    return run_id.replace("T", " ").replace("Z", " UTC") if run_id else "-"


st.title("숏폼 캠페인 성과 대시보드")

# ---------------------------------------------------------------- 자동 수집 (앱을 열 때만 확인)
needed, last_seen = auto_collect.check(config.DB_PATH)
outcome = {"action": "none", "last_success": last_seen, "error": None}
if needed and config.AUTO_COLLECT:
    with st.status(f"데이터 수집 중 — 데이터가 없거나 마지막 수집 후 {config.REFRESH_HOURS}시간이 지나 "
                   "YouTube에서 새로 검색합니다 (약 1분, 페이지를 닫지 마세요)", expanded=True) as box:
        outcome = auto_collect.ensure_fresh_data(collector_state(), config.DB_PATH, progress=box.write)
        if outcome["action"] == "collected":
            box.update(label=f"수집 완료 ({_fmt_run(outcome['last_success'])})", state="complete", expanded=False)
        elif outcome["action"] == "none":
            box.update(label="다른 방문자가 방금 수집을 마쳤습니다", state="complete", expanded=False)
        else:
            box.update(label="수집하지 못했습니다", state="error", expanded=False)
elif needed:
    outcome = {"action": "disabled", "last_success": last_seen, "error": None}

action, last_ok = outcome["action"], outcome["last_success"]
if action in ("failed", "retry_wait"):
    msg = collect.explain_error(outcome["error"])          # 키는 이미 *** 로 가려진 메시지
    if action == "retry_wait":
        msg += f" — {_fmt_run(outcome['retry_at'].strftime('%Y-%m-%dT%H:%M:%SZ'))} 이후 방문 시 다시 시도합니다."
    st.error(f"데이터 수집 실패: {msg}")
elif action == "no_key":
    st.error(f"API 키가 설정되지 않아 수집할 수 없습니다. 배포 환경은 Streamlit Secrets에 "
             f'`{keys.KEY_NAME} = "..."`를, 로컬은 .env에 `{keys.KEY_NAME}=...`를 넣어 주세요.')
elif action == "disabled":
    st.info("자동 수집이 꺼져 있습니다 (AUTO_COLLECT=0). `collect.py search`로 직접 수집하세요.")
if action in ("failed", "retry_wait", "no_key", "disabled") and last_ok:
    st.warning(f"마지막 성공 수집({_fmt_run(last_ok)}) 데이터를 표시합니다. "
               f"{config.REFRESH_HOURS}시간이 지난 데이터입니다.")

# ---------------------------------------------------------------- 포맷 분류 (새 영상만, 앱을 열 때 확인)
format_outcome = {"action": "none", "error": None}
pending_formats = auto_collect.pending_format_count(config.DB_PATH)
if pending_formats and config.AUTO_COLLECT:
    with st.status(f"콘텐츠 포맷 분류 중 — 아직 분류하지 않은 영상 {pending_formats}개 (OpenAI {config.FORMAT_MODEL})",
                   expanded=False) as fbox:
        format_outcome = auto_collect.ensure_formats(collector_state(), config.DB_PATH, progress=fbox.write)
        r = format_outcome.get("result") or {}
        if format_outcome["action"] == "classified":
            fbox.update(label=f"포맷 분류 완료 ({r.get('classified', 0)}개, 실패 {r.get('failed', 0)}개)",
                        state="complete")
        elif format_outcome["action"] == "no_key":
            fbox.update(label="OpenAI 키가 없어 포맷 분류를 건너뜀", state="error")
        elif format_outcome["action"] != "none":
            fbox.update(label="포맷 분류를 하지 못했습니다", state="error")
if format_outcome["action"] in ("failed", "retry_wait"):
    fmsg = classify_formats.explain_error(format_outcome["error"])
    if format_outcome["action"] == "retry_wait":
        fmsg += f" — {_fmt_run(format_outcome['retry_at'].strftime('%Y-%m-%dT%H:%M:%SZ'))} 이후 방문 시 다시 시도합니다."
    st.error(f"포맷 분류 실패: {fmsg}")
elif format_outcome["action"] == "no_key":
    st.warning(f"OpenAI 키가 없어 포맷 분류를 하지 않았습니다 (미분류 {pending_formats}개). 배포 환경은 Streamlit "
               f'Secrets에 `{keys.OPENAI_KEY_NAME} = "..."`를, 로컬은 .env에 `{keys.OPENAI_KEY_NAME}=...`를 넣어 주세요.')

df_all, basis = load_data(config.DB_PATH.stat().st_mtime)
if df_all.empty:
    st.warning(f"표시할 데이터가 없습니다 (수집 기준 {config.CRITERIA}의 성공한 수집 없음).")
    st.stop()

st.caption(f"수집 기준 `{config.CRITERIA}` · 마지막 수집 {_fmt_run(basis.get('used_search_run'))} · "
           "쇼츠로 판정된 영상만 · 영상별 가장 최근 스냅샷 기준")

# ---------------------------------------------------------------- 필터
keywords_all = sorted(df_all["keyword"].unique())
d_min = df_all["published_at"].min().date()
d_max = df_all["published_at"].max().date()

f1, f2 = st.columns([2, 1])
selected = f1.multiselect("키워드", keywords_all, default=keywords_all)
period = f2.date_input("업로드 기간 (UTC)", value=(d_min, d_max), min_value=d_min, max_value=d_max)
start, end = (period if isinstance(period, (list, tuple)) and len(period) == 2 else (d_min, d_max))

upload_date = df_all["published_at"].dt.date
df = df_all[df_all["keyword"].isin(selected) & (upload_date >= start) & (upload_date <= end)]
if df.empty:
    st.info("선택한 조건에 해당하는 영상이 없습니다.")
    st.stop()

videos = metrics.dedupe_videos(df)
desc = metrics.DESCRIPTIONS              # 긴 버전: 요약표 툴팁·데이터 기준 안내
short = metrics.SHORT_DESCRIPTIONS       # 짧은 버전: 상단 카드

# ---------------------------------------------------------------- 1. 기준선
section("우리 캠페인 기준선은?", obs.baseline(df))


def _fmt_median(key: str, fmt: str) -> str:
    value = videos[key].median()
    return "-" if pd.isna(value) else format(value, fmt)


cards = [
    ("영상 수", f"{len(videos):,}", short["video_count"], None),
    ("조회수 중앙값", _fmt_median("views", ",.0f"), short["views"], "views"),
    ("조회수 속도 중앙값", _fmt_median("views_per_day", ",.0f"), short["views_per_day"], "views_per_day"),
    ("참여율 중앙값", _fmt_median("engagement_rate", ".2%"), short["engagement_rate"], "engagement_rate"),
    ("구독자 대비 조회수 중앙값", _fmt_median("views_per_subscriber", ".2f"),
     short["views_per_subscriber"], "views_per_subscriber"),
]
for col, (label, value, text, key) in zip(st.columns(len(cards)), cards):
    help_text = metrics.exclusion_text(df, key) if key else None   # 현재 필터 기준 제외 수·사유
    col.metric(label, value, help=help_text)
    col.caption(text)

# ---------------------------------------------------------------- 2. 주제
summary = metrics.summary_by_keyword(df)
section("어떤 주제가 잘 퍼지나?", obs.topics(summary))

table = pd.DataFrame({"키워드": summary["keyword"], "영상 수": summary["video_count"]})
for key, label in COLUMN_LABELS.items():
    table[f"{label} 평균"] = summary[f"{key}_mean"]
    table[f"{label} 중앙값"] = summary[f"{key}_median"]
    table[f"{label} n"] = summary[f"{key}_n"]
table["구독자 대비 조회수 > 1 비율"] = summary["views_per_subscriber_over_1_share"]

num_fmt = {"영상 수": st.column_config.NumberColumn(format="%d", help=desc["video_count"])}
for key, label in COLUMN_LABELS.items():
    num_fmt[f"{label} 평균"] = st.column_config.NumberColumn(
        format=COLUMN_FORMATS[key], help=f"{desc[key]} — 영상별 값의 단순 평균")
    num_fmt[f"{label} 중앙값"] = st.column_config.NumberColumn(
        format=COLUMN_FORMATS[key], help=f"{desc[key]} — 영상들을 값 순으로 세웠을 때 가운데 값")
    num_fmt[f"{label} n"] = st.column_config.NumberColumn(
        format="%d", help=f"{label} 계산에 쓰인 영상 수 (제외 영상 빼고)")
num_fmt["구독자 대비 조회수 > 1 비율"] = st.column_config.NumberColumn(
    format="percent", help=f"구독자 대비 조회수가 1을 넘는 영상 비율. {desc['views_per_subscriber']}")
st.dataframe(table, hide_index=True, width="stretch", column_config=num_fmt)
st.caption(
    f"참여율: {metrics.exclusion_text(df, 'engagement_rate')} (0으로 넣지 않음) · "
    f"구독자 대비 조회수: {metrics.exclusion_text(df, 'views_per_subscriber')} · "
    "'전체' 행은 여러 키워드에 속한 영상을 한 번만 셈"
)

# ---------------------------------------------------------------- 2-1. 포맷
fsum = metrics.summary_by_format(df)
other = metrics.other_share(df)
section("어떤 포맷이 잘 퍼지나?", obs.formats(fsum, other))

ftable = pd.DataFrame({
    "포맷": fsum["format"],
    "영상 수": fsum["video_count"],
    "비율": fsum["share"],
    **{f"{COLUMN_LABELS[k]} 중앙값": fsum[f"{k}_median"] for k in COLUMN_LABELS},
    "참여율 n": fsum["engagement_rate_n"],
    "비고": fsum["sample_insufficient"].map({True: "표본 부족", False: ""}),
})
st.dataframe(
    ftable, hide_index=True, width="stretch",
    column_config={
        "영상 수": st.column_config.NumberColumn(format="%d", help="이 포맷으로 분류된 쇼츠 수 (중복 제외)"),
        "비율": st.column_config.NumberColumn(format="percent", help="분류된 영상 중 이 포맷의 비율 (미분류 제외)"),
        **{f"{label} 중앙값": st.column_config.NumberColumn(format=COLUMN_FORMATS[k], help=desc[k])
           for k, label in COLUMN_LABELS.items()},
        "참여율 n": st.column_config.NumberColumn(format="%d", help="참여율 계산에 쓰인 영상 수"),
        "비고": st.column_config.TextColumn(
            help=f"영상 {config.FORMAT_MIN_SAMPLE}개 미만 포맷은 중앙값이 우연에 크게 흔들림"),
    },
)
o_share, o_n, o_total = other
if o_total and o_share > config.FORMAT_OTHER_REVIEW_SHARE:
    st.warning(f"기타 비율 {o_share:.1%} ({o_n}/{o_total})가 기준 {config.FORMAT_OTHER_REVIEW_SHARE:.0%}를 넘었습니다 — "
               "포맷 목록 재검토 대상입니다 (CLAUDE.md 기준).")
st.markdown("**키워드 × 포맷 (영상 수)**")
st.dataframe(metrics.keyword_format_table(df), width="stretch")
st.caption(f"포맷은 제목·설명·해시태그 기반 LLM 분류 ({config.FORMAT_MODEL}, 프롬프트 {config.FORMAT_PROMPT_VERSION}) · "
           f"영상 {config.FORMAT_MIN_SAMPLE}개 미만은 표본 부족 · 키워드 × 포맷 표는 여러 키워드에 속한 영상을 키워드마다 셈")

# ---------------------------------------------------------------- 3. 크리에이터
channels_all = metrics.summary_by_channel(df)

st.subheader("어떤 크리에이터와 일할까?")
obs_slot = st.empty()   # 필터 적용 후의 관찰 문장을 제목 바로 아래에 채움
cf1, cf2, cf3 = st.columns([2, 2, 1])
sub_lo, sub_hi = cf1.select_slider("구독자 수 범위", options=SUB_STEPS, value=SUB_DEFAULT,
                                   format_func=SUB_LABELS.get)
cf2.caption("기본 최솟값 1,000명: 구독자가 아주 적은 채널은 구독자 대비 조회수가 비정상적으로 커져 "
            "발굴 대상으로 의미가 없어 기본 제외합니다.")
multi_only = cf3.checkbox("영상 2개 이상만 보기", value=False)

subs = channels_all["subscriber_count"]
full_range = sub_lo == 0 and math.isinf(sub_hi)
if full_range:
    in_range = pd.Series(True, index=channels_all.index)
else:
    in_range = subs.notna() & (subs >= sub_lo) & ((subs <= sub_hi) if not math.isinf(sub_hi) else True)
channels = channels_all[in_range]
if multi_only:
    channels = channels[channels["video_count"] >= 2]
obs_slot.markdown(f"**관찰** · {obs.creators(channels)}")

ch_table = pd.DataFrame({
    "채널명": channels["channel_title"],
    "구독자 수": channels["subscriber_count"],
    "영상 수": channels["video_count"],
    "구독자 대비 조회수 중앙값": channels["views_per_subscriber_median"],
    "참여율 중앙값": channels["engagement_rate_median"],
    "대표 영상": channels["rep_title"],
    "대표 영상 링크": channels["rep_url"],
    "비고": channels["single_video"].map({True: "영상 1개", False: ""}),
})
st.dataframe(
    ch_table, hide_index=True, width="stretch",
    column_config={
        "구독자 수": st.column_config.NumberColumn(format="localized"),
        "영상 수": st.column_config.NumberColumn(format="%d", help="이번 데이터(현재 필터)에 포함된 이 채널의 쇼츠 수"),
        "구독자 대비 조회수 중앙값": st.column_config.NumberColumn(
            format="%.2f", help=f"채널 영상들의 중앙값. {desc['views_per_subscriber']}"),
        "참여율 중앙값": st.column_config.NumberColumn(
            format="percent", help=f"채널 영상들의 중앙값. {desc['engagement_rate']}"),
        "대표 영상": st.column_config.TextColumn(help="채널 영상 중 구독자 대비 조회수가 가장 높은 영상"),
        "대표 영상 링크": st.column_config.LinkColumn(display_text="YouTube에서 열기"),
        "비고": st.column_config.TextColumn(help="영상이 1개뿐인 채널은 우연한 결과일 수 있음"),
    },
)
below = int((subs.notna() & (subs < sub_lo)).sum())
above = int((subs.notna() & (subs > sub_hi)).sum()) if not math.isinf(sub_hi) else 0
hidden = 0 if full_range else int(subs.isna().sum())
note = [f"전체 {len(channels_all)}개 채널 중 {len(channels)}개 표시"]
if below:
    note.append(f"구독자 {SUB_LABELS[sub_lo]} 미만 {below}개 제외")
if above:
    note.append(f"구독자 {SUB_LABELS[sub_hi]} 초과 {above}개 제외")
if hidden:
    note.append(f"구독자 비공개 {hidden}개 제외")
if multi_only:
    note.append("영상 1개 채널 제외")
st.caption(" · ".join(note) + " · 기본 정렬: 구독자 대비 조회수 중앙값 높은 순 (열 제목을 눌러 다시 정렬 가능)")

# ---------------------------------------------------------------- 4. 참고 영상
st.subheader("무엇을 참고해 만들까?")
ref_slot = st.empty()
s1, s2 = st.columns([2, 1])
sort_key = s1.selectbox("정렬 기준", list(COLUMN_LABELS), format_func=COLUMN_LABELS.get)
top_n = s2.slider("표시 개수", 5, max(5, len(videos)), min(20, len(videos)))
top = videos.sort_values(sort_key, ascending=False, na_position="last").head(top_n)
ref_slot.markdown(f"**관찰** · {obs.references(top, sort_key)}")
top_table = pd.DataFrame({
    "제목": top["title"],
    "채널": top["channel_title"],
    "키워드": top["keywords"],
    "업로드일": top["published_at"].dt.date,
    "조회수": top["views"],
    "좋아요": top["like_count"],
    "댓글": top["comment_count"],
    "참여율": top["engagement_rate"],
    "조회수 속도(회/일)": top["views_per_day"],
    "구독자 수": top["subscriber_count"],
    "구독자 대비 조회수": top["views_per_subscriber"],
    "링크": top["url"],
})
st.dataframe(
    top_table, hide_index=True, width="stretch",
    column_config={
        "조회수": st.column_config.NumberColumn(format="localized"),
        "좋아요": st.column_config.NumberColumn(format="localized"),
        "댓글": st.column_config.NumberColumn(format="localized"),
        "참여율": st.column_config.NumberColumn(format="percent"),
        "조회수 속도(회/일)": st.column_config.NumberColumn(format="localized"),
        "구독자 수": st.column_config.NumberColumn(format="localized"),
        "구독자 대비 조회수": st.column_config.NumberColumn(format="%.2f"),
        "링크": st.column_config.LinkColumn(display_text="YouTube에서 열기"),
    },
)
st.caption("빈 칸은 계산 제외(값 없음)이며 정렬 시 맨 아래로 갑니다.")

# ---------------------------------------------------------------- 5. 세부 분포
section("세부 분포", obs.distribution(df))
er = df.dropna(subset=["engagement_rate"])
c1, c2 = st.columns(2)

box = go.Figure()
for kw in sorted(er["keyword"].unique()):
    g = er[er["keyword"] == kw]
    box.add_trace(go.Box(
        y=g["engagement_rate"], name=kw, boxpoints="all", jitter=0.4, pointpos=0,
        marker=dict(color=SERIES_COLOR, size=8, opacity=0.55, line=dict(width=1, color="white")),
        line=dict(color=SERIES_COLOR, width=2), fillcolor="rgba(37,125,224,0.12)",
        text=g["title"], customdata=g["views"],
        hovertemplate="<b>%{text}</b><br>참여율 %{y:.2%}<br>조회수 %{customdata:,}<extra></extra>",
    ))
box.update_layout(title="키워드별 참여율 분포", showlegend=False, height=460,
                  yaxis=dict(title="참여율", tickformat=".0%"), margin=dict(t=50, l=10, r=10))
c1.plotly_chart(box, width="stretch")

kws = sorted(er["keyword"].unique())
if kws:
    ncols = 2 if len(kws) > 1 else 1
    nrows = (len(kws) + ncols - 1) // ncols
    scatter = make_subplots(rows=nrows, cols=ncols, subplot_titles=kws,
                            horizontal_spacing=0.08, vertical_spacing=0.16)
    for i, kw in enumerate(kws):
        g = er[er["keyword"] == kw]
        scatter.add_trace(go.Scatter(
            x=g["views"], y=g["engagement_rate"], mode="markers", name=kw, text=g["title"],
            marker=dict(color=SERIES_COLOR, size=8, opacity=0.7, line=dict(width=1, color="white")),
            hovertemplate="<b>%{text}</b><br>조회수 %{x:,}<br>참여율 %{y:.2%}<extra></extra>",
        ), row=i // ncols + 1, col=i % ncols + 1)
    # 모든 패널에 같은 x·y 범위 고정 (패널끼리 바로 비교할 수 있게)
    x_lo, x_hi = math.log10(er["views"].min()), math.log10(er["views"].max())
    x_pad = max(0.1, (x_hi - x_lo) * 0.05)
    y_hi = er["engagement_rate"].max()
    scatter.update_xaxes(type="log", tickformat="~s", range=[x_lo - x_pad, x_hi + x_pad])
    scatter.update_yaxes(tickformat=".0%", range=[0, y_hi * 1.08 if y_hi > 0 else 0.01])
    scatter.update_layout(title="조회수(로그 축) vs 참여율 — 키워드별 (모든 패널 같은 축 범위)",
                          showlegend=False, height=460, margin=dict(t=70, l=10, r=10))
    c2.plotly_chart(scatter, width="stretch")
st.caption(f"차트는 참여율을 계산할 수 있는 영상만 표시 ({metrics.exclusion_text(df, 'engagement_rate')}). "
           "점 하나 = 영상 하나.")

# ---------------------------------------------------------------- 데이터 기준 안내
st.subheader("데이터 기준")
sc = basis.get("search_criteria", {})
rule = basis["short_rule"]
fmt_ts = lambda t: t.strftime("%Y-%m-%d %H:%M UTC") if t is not None else "-"  # noqa: E731
b1, b2 = st.columns(2)
b1.markdown(
    f"""
**수집 기준** `{basis['criteria']}`
- 검색: {sc.get('order', '-')}순(관련도순), 지역 {sc.get('region_code', '-')}, 언어 {sc.get('relevance_language', '-')}
- 업로드 기간: 수집 시점 기준 {config.PUBLISHED_MIN_DAYS_AGO}~{config.PUBLISHED_MAX_DAYS_AGO}일 전
  ({sc.get('published_after', '-')} ~ {sc.get('published_before', '-')})
- 키워드당 쇼츠 최대 {sc.get('max_shorts_per_keyword', '-')}개 (검색 최대 {sc.get('max_pages', '-')}페이지)
- 사용한 수집: 가장 최근 성공 search 실행 `{basis.get('used_search_run') or '-'}`
  (마지막 수집 후 {config.REFRESH_HOURS}시간이 지나면 앱을 열 때 새로 검색)
- 사용한 스냅샷 수집 시각: {fmt_ts(basis.get('snapshot_from'))} ~ {fmt_ts(basis.get('snapshot_to'))}
- 보관 기간: 수집 후 {basis['retention_days']}일이 지난 데이터는 자동 삭제 (YouTube API 정책)

**쇼츠 판정 기준**
- 화면 비율: {rule['aspect']} — 가로 영상은 길이와 상관없이 일반 영상
- 길이: {rule['rule_change_at'][:10]} 이후 업로드는 {rule['max_seconds']}초 이하,
  이전 업로드는 {rule['max_seconds_old']}초 이하

**지표 정의**
- 영상 수: {desc['video_count']}
- 조회수 중앙값: {desc['views']}
- 조회수 속도: {desc['views_per_day']} (경과 일수는 최소 {basis['min_elapsed_days']}일로 처리)
- 참여율: {desc['engagement_rate']} (공유 수는 API 미제공으로 제외)
- 구독자 대비 조회수: {desc['views_per_subscriber']} (같은 수집 시점, 없으면 가장 가까운 시점의 구독자 수)
- 요약 통계는 평균(영상별 값의 단순 평균)과 중앙값을 함께 제시
"""
)
ex = metrics.exclusion_summary(df).rename(columns={"metric": "지표", "reason": "사유", "count": "영상 수"})
b2.markdown("**지표별 제외 영상 (현재 필터 기준, 영상 중복 제거)**")
b2.dataframe(ex, hide_index=True, width="stretch")

# ---------------------------------------------------------------- 해석 주의
st.subheader("해석 주의")
accuracy_text = (f"정확도는 표본 {config.FORMAT_ACCURACY_SAMPLE}개 기준 {config.FORMAT_ACCURACY:.0%}"
                 if config.FORMAT_ACCURACY is not None
                 else f"정확도는 표본 {config.FORMAT_ACCURACY_SAMPLE}개 사람 라벨로 검증 예정 (검증 전)")
st.warning(
    f"""
- **포맷은 제목·설명 기반 LLM 분류이며 {accuracy_text}입니다.** 영상 내용은 보지 않았습니다.
- **무작위 표본이 아닙니다.** 키워드 검색의 관련도 상위 영상이라 성과가 좋은 영상 쪽으로 치우쳐 있을 수 있습니다.
- **공유 수·시청 시간·비용·전환은 포함하지 않습니다.** YouTube Data API가 제공하지 않습니다.
- **광고 조회수와 자연 조회수를 구분할 수 없습니다.**
- 한 영상이 여러 키워드에 속하면 키워드별 표에는 각각 집계됩니다('전체' 행은 한 번만).
- 구독자 수는 YouTube가 반올림해 제공하는 값입니다(유효숫자 3자리).
- 크리에이터 표의 영상 수는 이번 검색에 잡힌 영상 수이며 채널 전체 영상 수가 아닙니다.
"""
)
