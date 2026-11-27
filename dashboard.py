"""숏폼 캠페인 성과 대시보드 — 결정 질문 순서로 구성 (v3: 키워드 buldak 하나, 포맷별 비교).

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
import classify_comments
import classify_formats
import collect
import config
import keys
import metrics
import observations as obs
import snapshot

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

FORMAT_COLORS = config.FORMAT_COLORS     # 포맷 색 (모든 그래프 공통). 기타는 그래프에서 제외
CHART_FORMATS = metrics.chart_formats()  # 그래프 포맷 순서: 챌린지 → 레시피·조리 → 먹방·ASMR·맛평가
INK, INK_2, GRID = "#191919", "#666666", "#e7e7e7"   # 글자·보조 글자·격자 (글자는 계열 색을 쓰지 않음)
COMPARE_FORMATS = {"views_per_day": ",.0f", "views_per_subscriber": ".2f",
                   "engagement_rate": ".2%", "purchase_rate": ".1%"}
SHORT_FORMAT = {"챌린지": "챌린지", "레시피·조리": "레시피", "먹방·ASMR·맛평가": "먹방"}
# 감성 기준 설명용 예시 (실제 댓글이 아니라 직접 만든 문장). 정의는 classify_comments.SENTIMENT_DEFINITIONS 그대로
SENTIMENT_EXAMPLES = {"긍정": "맛있어 보여요", "중립": "몇 단계 매운맛이에요?", "부정": "너무 과장된 것 같아요"}
SENTIMENT_LINES = [f'{s}: {classify_comments.SENTIMENT_DEFINITIONS[s]} — 예: "{SENTIMENT_EXAMPLES[s]}"'
                   for s in ("긍정", "중립", "부정")]
SENTIMENT_EXAMPLE_NOTE = "예시는 이해를 돕기 위해 만든 문장이며, 실제 댓글 원문은 표시하지 않음"


def _rgba(hex_color: str, alpha: float) -> str:
    h = hex_color.lstrip("#")
    return f"rgba({int(h[0:2], 16)},{int(h[2:4], 16)},{int(h[4:6], 16)},{alpha})"


def _base_layout(fig, height: int, **kw) -> None:
    fig.update_layout(height=height, showlegend=False, plot_bgcolor="white", paper_bgcolor="white",
                      font=dict(color=INK_2, size=12), margin=dict(t=40, l=10, r=10, b=10), **kw)


def comparison_figure(rows: pd.DataFrame, key: str):
    """포맷 비교 작은 막대 1개: 세 막대 모두 같은 진하기(포맷 색). 95% 신뢰구간은 마우스를 올릴 때만 보임.

    rows: format_comparison 행 + low·high (부트스트랩 구간). 값 라벨은 막대 끝 위에 글자색으로.
    순위 대신 그래프 아래에 '확실한 차이' 쌍을 적음 (metrics.sure_pairs_text)
    """
    fmt = COMPARE_FORMATS[key]
    x = [SHORT_FORMAT[f] for f in rows["format"]]
    high = rows["high"].fillna(rows["value"])
    low = rows["low"].fillna(rows["value"])
    fig = go.Figure(go.Bar(
        x=x, y=rows["value"],
        marker=dict(color=[FORMAT_COLORS[f] for f in rows["format"]], line=dict(width=0), cornerradius=4),
        customdata=list(zip(rows["format"], rows["n"], low, high)),
        hovertemplate=("%{customdata[0]}<br>값 %{y:" + fmt + "}<br>95% 신뢰구간 %{customdata[2]:" + fmt
                       + "} ~ %{customdata[3]:" + fmt + "}<br>n %{customdata[1]}<extra></extra>"),
    ))
    fig.add_trace(go.Scatter(                     # 값 라벨: 막대 끝 위
        x=x, y=rows["value"], mode="text", hoverinfo="skip", cliponaxis=False,
        text=[format(v, fmt) for v in rows["value"]],
        textposition="top center", textfont=dict(color=INK, size=12),
    ))
    top = rows["value"].max()
    _base_layout(fig, 260, bargap=0.35)
    fig.update_layout(margin=dict(t=20, l=10, r=10, b=10))
    fig.update_yaxes(range=[0, (top if pd.notna(top) and top > 0 else 1) * 1.25], showgrid=True,
                     gridcolor=GRID, zeroline=True, zerolinecolor="#c2c2c2",
                     tickformat=fmt.replace(",.0f", "~s").replace(".2%", ".1%"))
    fig.update_xaxes(showline=True, linecolor="#c2c2c2")
    return fig




@st.cache_data(show_spinner=False)
def boot_diffs_channel(_df: pd.DataFrame, cache_key: tuple) -> pd.DataFrame:
    """'확실한 차이' 표시용 포맷 쌍 차이 (채널 단위 부트스트랩 1만 회, 21번 비교 보정). cache_key = (DB 시각, 기간)."""
    return metrics.bootstrap_format_diffs(_df, unit="channel")


@st.cache_data(show_spinner=False)
def boot_ci(_df: pd.DataFrame, cache_key: tuple) -> pd.DataFrame:
    """포맷 비교 그래프용 95% 신뢰구간 (영상 단위 부트스트랩 1만 회, 시드 고정). cache_key = (DB 시각, 기간)."""
    return metrics.bootstrap_format_ci(_df)


def _help(*parts: str) -> str:
    return "\n\n".join(p for p in parts if p)


@st.cache_data
def load_data(db_mtime: float) -> tuple[pd.DataFrame, dict]:
    df = metrics.load(config.DB_PATH, config.CRITERIA)
    return df, metrics.data_basis(config.DB_PATH, config.CRITERIA, df)


def section(title: str, observation=None, anchor: str | None = None) -> None:
    """섹션 제목(결정 질문). 관찰 한 줄은 config.SHOW_OBSERVATIONS가 True일 때만 표시 (observation은 지연 계산 함수)."""
    st.subheader(title, anchor=anchor)
    show_observation(st, observation)


def show_observation(slot, observation) -> None:
    if config.SHOW_OBSERVATIONS and observation is not None:
        slot.markdown(f"**관찰** · {observation()}")


@st.cache_resource
def collector_state() -> auto_collect.CollectorState:
    """프로세스 전체에서 1개: 동시 접속 시 수집 잠금 + 실패 후 재시도 대기 시각."""
    return auto_collect.CollectorState()


def _fmt_run(run_id: str | None) -> str:
    return run_id.replace("T", " ").replace("Z", " UTC") if run_id else "-"


st.title("숏폼 캠페인 성과 대시보드")

# ---------------------------------------------------------------- 데이터 준비
# 스냅샷 모드(배포): 비공개 저장소의 스냅샷만 받아 와 보여 줌 — 수집·분류·30일 자동 삭제를 하지 않음
# 로컬 모드: 앱을 열 때 수집 → 포맷 분류 → 댓글 수집·분류 (아래 _local_pipeline)
def _local_pipeline() -> None:
    # ---------------------------------------------------------------- 자동 수집 (앱을 열 때만 확인)
    needed, last_seen = auto_collect.check(config.DB_PATH)
    outcome = {"action": "none", "last_success": last_seen, "error": None}
    if needed and config.AUTO_COLLECT:
        with st.status(f"데이터 수집 중 — 데이터가 없거나 마지막 수집 후 {config.REFRESH_HOURS}시간이 지나 "
                       "YouTube에서 새로 검색함 (약 1분, 페이지를 닫지 말 것)", expanded=True) as box:
            outcome = auto_collect.ensure_fresh_data(collector_state(), config.DB_PATH, progress=box.write)
            if outcome["action"] == "collected":
                box.update(label=f"수집 완료 ({_fmt_run(outcome['last_success'])})", state="complete", expanded=False)
            elif outcome["action"] == "none":
                box.update(label="다른 방문자가 방금 수집을 마침", state="complete", expanded=False)
            else:
                box.update(label="수집하지 못함", state="error", expanded=False)
    elif needed:
        outcome = {"action": "disabled", "last_success": last_seen, "error": None}

    action, last_ok = outcome["action"], outcome["last_success"]
    if action in ("failed", "retry_wait"):
        msg = collect.explain_error(outcome["error"])          # 키는 이미 *** 로 가려진 메시지
        if action == "retry_wait":
            msg += f" — {_fmt_run(outcome['retry_at'].strftime('%Y-%m-%dT%H:%M:%SZ'))} 이후 방문 시 다시 시도함."
        st.error(f"데이터 수집 실패: {msg}")
    elif action == "no_key":
        st.error(f"API 키가 설정되지 않아 수집할 수 없음. 배포 환경은 Streamlit Secrets에 "
                 f'`{keys.KEY_NAME} = "..."`를, 로컬은 .env에 `{keys.KEY_NAME}=...`를 넣어야 함.')
    elif action == "disabled":
        st.info("자동 수집이 꺼져 있음 (AUTO_COLLECT=0). `collect.py search`로 직접 수집 필요.")
    if action in ("failed", "retry_wait", "no_key", "disabled") and last_ok:
        st.warning(f"마지막 성공 수집({_fmt_run(last_ok)}) 데이터를 표시함. "
                   f"{config.REFRESH_HOURS}시간이 지난 데이터임.")

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
                fbox.update(label="포맷 분류를 하지 못함", state="error")
    if format_outcome["action"] in ("failed", "retry_wait"):
        fmsg = classify_formats.explain_error(format_outcome["error"])
        if format_outcome["action"] == "retry_wait":
            fmsg += f" — {_fmt_run(format_outcome['retry_at'].strftime('%Y-%m-%dT%H:%M:%SZ'))} 이후 방문 시 다시 시도함."
        st.error(f"포맷 분류 실패: {fmsg}")
    elif format_outcome["action"] == "no_key":
        st.warning(f"OpenAI 키가 없어 포맷 분류를 하지 않음 (미분류 {pending_formats}개). 배포 환경은 Streamlit "
                   f'Secrets에 `{keys.OPENAI_KEY_NAME} = "..."`를, 로컬은 .env에 `{keys.OPENAI_KEY_NAME}=...`를 넣어야 함.')

    # ---------------------------------------------------------------- 댓글 수집 + 반응 분류 (새 영상·새 댓글만)
    n_comment_videos, n_pending_comments = auto_collect.pending_reaction_work(config.DB_PATH)
    reaction_outcome = {"comments": {"action": "none"}, "reactions": {"action": "none"}}
    if (n_comment_videos or n_pending_comments) and config.AUTO_COLLECT:
        with st.status(f"댓글 반응 분석 중 — 댓글 수집 대상 영상 {n_comment_videos}개, 분류 대상 댓글 "
                       f"{n_pending_comments}개 (수집 후 새 댓글이 더해짐, 처음에는 몇 분 걸릴 수 있음)",
                       expanded=False) as rbox:
            reaction_outcome = auto_collect.ensure_reactions(collector_state(), config.DB_PATH, progress=rbox.write)
            acts = {reaction_outcome["comments"]["action"], reaction_outcome["reactions"]["action"]}
            bad = acts & {"failed", "retry_wait", "no_key"}
            rbox.update(label="댓글 반응 분석 완료" if not bad else "댓글 반응 분석 일부를 하지 못함",
                        state="complete" if not bad else "error")
    for stage, label, explain, key_name in (
            ("comments", "댓글 수집", collect.explain_error, keys.KEY_NAME),
            ("reactions", "댓글 분류", classify_formats.explain_error, keys.OPENAI_KEY_NAME)):
        o = reaction_outcome[stage]
        if o["action"] in ("failed", "retry_wait"):
            msg = explain(o.get("error"))
            if o["action"] == "retry_wait":
                msg += f" — {_fmt_run(o['retry_at'].strftime('%Y-%m-%dT%H:%M:%SZ'))} 이후 방문 시 다시 시도함."
            st.error(f"{label} 실패: {msg}")
        elif o["action"] == "no_key":
            st.warning(f"{key_name}가 없어 {label}을 하지 않음.")


SNAPSHOT_MODE = snapshot.is_snapshot_mode()
if SNAPSHOT_MODE:
    snap = snapshot.ensure_snapshot(config.DB_PATH)
    if snap["action"] == "downloaded":
        st.toast("데이터 저장소에서 스냅샷을 받아 옴")
    elif snap["action"] == "no_key":
        st.error(f"스냅샷을 받을 설정이 없음. Streamlit Secrets에 `{snapshot.REPO_NAME}`와 "
                 f"`{snapshot.TOKEN_NAME}`를 넣어야 함.")
    elif snap["action"] == "bad_repo":
        st.error(f"`{snapshot.REPO_NAME}`는 \"owner/name\" 형식이어야 함.")
    elif snap["action"] == "failed":
        st.error(f"스냅샷을 받지 못함: {snapshot.explain_error(snap['error'])}")   # 토큰은 이미 *** 로 가려짐
    if not snapshot.has_data(config.DB_PATH):          # 받지 못했으면 위 오류 안내만 보여 주고 멈춤
        st.stop()
    if snapshot.is_expired(snapshot.collected_at(config.DB_PATH)):
        st.warning(snapshot.EXPIRED_NOTICE)
        st.stop()
else:
    _local_pipeline()

df_all, basis = load_data(config.DB_PATH.stat().st_mtime)
if df_all.empty:
    st.warning(f"표시할 데이터가 없음 (수집 기준 {config.CRITERIA}의 성공한 수집 없음).")
    st.stop()

st.caption(f"키워드 {', '.join(config.KEYWORDS)} · 수집 기준 `{config.CRITERIA}` · 마지막 수집 {_fmt_run(basis.get('used_search_run'))}"
           f"{' (스냅샷)' if SNAPSHOT_MODE else ''} · "
           "쇼츠로 판정된 영상만 · 영상별 가장 최근 스냅샷 기준")

# ---------------------------------------------------------------- 필터 (키워드가 하나라 업로드 기간만)
d_min = df_all["published_at"].min().date()
d_max = df_all["published_at"].max().date()

f1, _ = st.columns([1, 2])
period = f1.date_input("업로드 기간 (UTC)", value=(d_min, d_max), min_value=d_min, max_value=d_max)
start, end = (period if isinstance(period, (list, tuple)) and len(period) == 2 else (d_min, d_max))

upload_date = df_all["published_at"].dt.date
df = df_all[(upload_date >= start) & (upload_date <= end)]
if df.empty:
    st.info("선택한 조건에 해당하는 영상이 없음.")
    st.stop()

videos = metrics.dedupe_videos(df)
videos = videos.assign(format_label=metrics._format_labels(videos))
format_order = [f for f in [*config.FORMATS, metrics.UNCLASSIFIED] if (videos["format_label"] == f).any()]
desc = metrics.DESCRIPTIONS              # 긴 버전: 요약표 툴팁·데이터 기준 안내
short = metrics.SHORT_DESCRIPTIONS       # 짧은 버전: 상단 카드

# ---------------------------------------------------------------- 1. 전체 기준선
section("전체 기준선은?", lambda: obs.baseline(df))


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

# ---------------------------------------------------------------- 2. 포맷별 성과
fsum = metrics.summary_by_format(df, total_label="전체")
other = metrics.other_share(df)
section("어떤 포맷이 잘 퍼지나?", lambda: obs.formats(fsum, other), anchor="format-compare")

# 포맷 비교: 지표 4개 × 포맷 3개 작은 막대 (기타 제외). 지표마다 y축 따로 (단위가 다름)
st.markdown("**포맷 비교** — 그래프 아래에 확실한 차이가 있는 포맷 쌍만 표시", help=metrics.SURE_RULE)
boot_key = (config.DB_PATH.stat().st_mtime, str(start), str(end))
ci = boot_ci(df, boot_key)
comp = metrics.format_comparison(df).merge(ci[["metric", "format", "low", "high"]],
                                           on=["metric", "format"], how="left")
diffs_ch = boot_diffs_channel(df, boot_key)
df_chart = df[metrics._format_labels(df).isin(CHART_FORMATS)]            # 그래프 대상(기타 제외)
react_chart = metrics.summary_reactions(df, by="format")
for col, (key, title, _stat) in zip(st.columns(len(metrics.COMPARISON_METRICS)), metrics.COMPARISON_METRICS):
    rows = comp[comp["metric"] == key]
    n_text = "계산에 쓴 영상 수: " + " · ".join(f"{f} {n}" for f, n in zip(rows["format"], rows["n"]))
    if key in metrics.REACTION_RATES:
        r = react_chart[react_chart["group"].isin(CHART_FORMATS)]
        too_few, no_com = int(r["excluded_too_few"].sum()), int(r["excluded_no_comments"].sum())
        excl = (f"계산 제외 {too_few + no_com}개 — 분석 댓글 {config.COMMENT_MIN_PER_VIDEO}개 미만 {too_few}개"
                + (f", 댓글 없음·사용 중지 {no_com}개" if no_com else ""))
        tip = _help(desc[key], "대부분 영상이 0%라 중앙값 대신 평균으로 비교", n_text, excl, metrics.BOOT_NOTE,
                    metrics.SURE_RULE)
    else:
        tip = _help(desc[key], n_text, metrics.exclusion_text(df_chart, key), metrics.BOOT_NOTE, metrics.SURE_RULE)
    col.markdown(f"**{title}**", help=tip)
    col.plotly_chart(comparison_figure(rows, key), width="stretch",
                     config={"displayModeBar": False})
    col.markdown(metrics.sure_pairs_text(diffs_ch, key, SHORT_FORMAT))
    col.caption("n · " + " · ".join(f"{SHORT_FORMAT[f]} {n}" for f, n in zip(rows["format"], rows["n"])))
st.caption(f"{config.FORMAT_EXCLUDED_FROM_COMPARISON}는 내용이 섞인 묶음이라 그래프에서 제외(아래 표에는 '비교 제외'로 표시) · "
           "구매의향은 대부분 영상에서 0이라 중앙값 대신 평균 · n은 지표 계산에 쓴 영상 수 "
           "(구매의향은 분석 댓글 5개 이상 영상)")

table = pd.DataFrame({"포맷": fsum["format"], "영상 수": fsum["video_count"], "비율": fsum["share"]})
for key, label in COLUMN_LABELS.items():
    table[f"{label} 평균"] = fsum[f"{key}_mean"]
    table[f"{label} 중앙값"] = fsum[f"{key}_median"]
    table[f"{label} n"] = fsum[f"{key}_n"]
table["구독자 대비 조회수 > 1 비율"] = fsum["views_per_subscriber_over_1_share"]
table["비고"] = [metrics.format_note(f, s) for f, s in zip(fsum["format"], fsum["sample_insufficient"])]

num_fmt = {"영상 수": st.column_config.NumberColumn(format="%d", help="이 포맷으로 분류된 쇼츠 수"),
           "비율": st.column_config.NumberColumn(format="percent", help="분류된 영상 중 이 포맷의 비율 (미분류 제외)")}
for key, label in COLUMN_LABELS.items():
    num_fmt[f"{label} 평균"] = st.column_config.NumberColumn(
        format=COLUMN_FORMATS[key], help=f"{desc[key]} — 영상별 값의 단순 평균")
    num_fmt[f"{label} 중앙값"] = st.column_config.NumberColumn(
        format=COLUMN_FORMATS[key], help=f"{desc[key]} — 영상들을 값 순으로 세웠을 때 가운데 값")
    num_fmt[f"{label} n"] = st.column_config.NumberColumn(
        format="%d", help=f"{label} 계산에 쓰인 영상 수 (제외 영상 빼고)")
num_fmt["구독자 대비 조회수 > 1 비율"] = st.column_config.NumberColumn(
    format="percent", help=f"구독자 대비 조회수가 1을 넘는 영상 비율. {desc['views_per_subscriber']}")
NOTE_HELP = (f"{config.FORMAT_EXCLUDED_FROM_COMPARISON}: 종이 DIY·랭킹·밈 등 내용이 섞인 묶음이라 다른 포맷과 "
             f"비교하지 않음 (구성은 데이터 기준 참고) · 영상 {config.FORMAT_MIN_SAMPLE}개 미만 포맷은 "
             "중앙값이 우연에 크게 흔들려 표본 부족")
num_fmt["비고"] = st.column_config.TextColumn(help=NOTE_HELP)
st.dataframe(table, hide_index=True, width="stretch", column_config=num_fmt)
o_share, o_n, o_total = other
if o_total and o_share > config.FORMAT_OTHER_REVIEW_SHARE:
    st.warning(f"기타 비율 {o_share:.1%} ({o_n}/{o_total})가 기준 {config.FORMAT_OTHER_REVIEW_SHARE:.0%}를 넘음 — "
               "포맷 목록 재검토 대상 (CLAUDE.md 기준).")
st.caption(
    f"참여율: {metrics.exclusion_text(df, 'engagement_rate')} (0으로 넣지 않음) · "
    f"구독자 대비 조회수: {metrics.exclusion_text(df, 'views_per_subscriber')} · "
    "'전체' 행은 미분류 영상까지 포함한 모든 영상 · "
    f"포맷은 제목·설명·해시태그 기반 LLM 분류 ({config.FORMAT_MODEL}, 프롬프트 {config.FORMAT_PROMPT_VERSION}) · "
    f"영상 {config.FORMAT_MIN_SAMPLE}개 미만은 표본 부족"
)

# ---------------------------------------------------------------- 3. 포맷별 댓글 반응 (집계만, 원문 표시 안 함)
fmt_react = metrics.summary_reactions(df, by="format")
section("포맷별 반응은 좋았나?", lambda: obs.reactions(fmt_react))

mix = metrics.sentiment_mix(df)
if not mix.empty:
    st.markdown("**포맷별 댓글 감성 구성** — 영상별 비율의 평균",
                help=_help(f"긍정 비율: {desc['positive_rate']}", f"부정 비율: {desc['negative_rate']}",
                           "중립 = 1 − 긍정 − 부정", "감성 기준 (댓글 분류 프롬프트의 정의)", *SENTIMENT_LINES,
                           f"댓글 분석 기준: {desc['comment_basis']}"))
    sent = go.Figure()
    labels = [SHORT_FORMAT[f] for f in mix["format"]]
    for s in ("긍정", "중립", "부정"):
        sent.add_trace(go.Bar(
            y=labels, x=mix[s], name=s, orientation="h",
            marker=dict(color=config.SENTIMENT_COLORS[s], line=dict(color="white", width=2)),
            text=[f"{s} {v:.0%}" if v >= 0.08 else "" for v in mix[s]], textposition="inside",
            insidetextanchor="middle",
            textfont=dict(color="#0b0b0b" if s == "중립" else "white", size=12),
            customdata=list(zip(mix["format"], mix["n"])),
            hovertemplate="%{customdata[0]}<br>" + s + " %{x:.1%}<br>분석 영상 %{customdata[1]}개<extra></extra>",
        ))
    for lab, n in zip(labels, mix["n"]):
        sent.add_annotation(x=1.01, y=lab, xref="x", yref="y", text=f"n {n}", showarrow=False,
                            xanchor="left", font=dict(color=INK_2, size=12))
    _base_layout(sent, 220, barmode="stack", bargap=0.35,
                 legend=dict(orientation="h", y=1.18, x=0, traceorder="normal"))
    sent.update_layout(showlegend=True, margin=dict(t=40, l=10, r=60, b=10))
    sent.update_xaxes(range=[0, 1], tickformat=".0%", showgrid=True, gridcolor=GRID)
    sent.update_yaxes(autorange="reversed")       # 위에서부터 챌린지 → 레시피 → 먹방
    st.plotly_chart(sent, width="stretch", config={"displayModeBar": False})
    st.markdown("  \n".join(SENTIMENT_LINES))       # 감성 기준 세 줄 (줄바꿈: 마크다운 두 칸 공백)
    st.caption(SENTIMENT_EXAMPLE_NOTE)
    st.caption("중립 = 1 − 긍정 − 부정 (감성은 셋 중 하나) · n은 분석 영상 수 (분석 댓글 5개 이상) · "
               f"{config.FORMAT_EXCLUDED_FROM_COMPARISON}는 그래프에서 제외")

if fmt_react.empty:
    st.info("분석한 댓글이 없음.")
else:
    rt = pd.DataFrame({"포맷": fmt_react["group"], "영상 수": fmt_react["video_count"],
                       "분석 영상 수": fmt_react["videos_analyzed"], "분석 댓글 수": fmt_react["comments_analyzed"]})
    rcfg = {"영상 수": st.column_config.NumberColumn(format="%d"),
            "분석 영상 수": st.column_config.NumberColumn(
                format="%d", help=f"분석 댓글이 {config.COMMENT_MIN_PER_VIDEO}개 이상이라 비율을 계산한 영상 수"),
            "분석 댓글 수": st.column_config.NumberColumn(format="localized", help="분석 영상들의 분류된 댓글 수 합계")}
    for col, label in metrics.REACTION_RATES.items():
        for stat, name in (("median", "중앙값"), ("mean", "평균")):
            rt[f"{label} {name}"] = fmt_react[f"{col}_{stat}"]
            rcfg[f"{label} {name}"] = st.column_config.NumberColumn(
                format="percent", help=f"{desc[col]} — 영상별 값의 {name}")
    rt[f"제외: 댓글 {config.COMMENT_MIN_PER_VIDEO}개 미만"] = fmt_react["excluded_too_few"]
    rt["제외: 댓글 없음·사용 중지"] = fmt_react["excluded_no_comments"]
    rt["비고"] = [metrics.format_note(g) for g in fmt_react["group"]]
    rcfg["비고"] = st.column_config.TextColumn(help=NOTE_HELP)
    st.dataframe(rt, hide_index=True, width="stretch", column_config=rcfg)
    t0 = fmt_react[fmt_react["group"] == "전체"].iloc[0]
    st.caption(
        f"영상별 비율을 먼저 구한 뒤 평균·중앙값을 계산 · '전체' 행은 미분류 영상까지 포함 · "
        f"분석 댓글 {config.COMMENT_MIN_PER_VIDEO}개 미만 영상 "
        f"{t0['excluded_too_few']}개, 댓글 없음·사용 중지 {t0['excluded_no_comments']}개 제외"
        + (f", 미수집·오류 {t0['excluded_other']}개" if t0["excluded_other"] else "")
        + f" · 감성·구매의향은 댓글 하나씩 LLM 분류 ({config.COMMENT_MODEL}, 프롬프트 {config.COMMENT_PROMPT_VERSION})"
        " · 개인정보 보호를 위해 댓글 원문은 표시하지 않음")
st.warning(f"- {config.CAVEAT_COMMENT_TARGET}")

# ---------------------------------------------------------------- 4. 크리에이터
channels_all = metrics.summary_by_channel(df)

st.subheader("어떤 크리에이터와 일할까?")
summary_slot = st.empty()   # 필터 적용 후의 요약 한 줄 (채널·쇼츠 수)을 제목 바로 아래에 채움
obs_slot = st.empty()       # 필터 적용 후의 관찰 문장
cf1, cf3 = st.columns([4, 1])
sub_lo, sub_hi = cf1.select_slider("구독자 수 범위", options=SUB_STEPS, value=SUB_DEFAULT,
                                   format_func=SUB_LABELS.get, help="기본 최솟값 1,000명 (데이터 기준 참고)")
multi_only = cf3.checkbox("영상 2개 이상만 보기", value=False)
main_options = [f for f in [*config.FORMATS, metrics.UNCLASSIFIED]
                if channels_all["main_formats"].apply(lambda m, f=f: f in m).any()]
main_pick = st.multiselect("주력 포맷", main_options, default=main_options,
                           help="채널 영상 중 가장 많은 포맷. 동률이면 둘 다 주력으로 보고, 하나라도 고르면 표시")

subs = channels_all["subscriber_count"]
full_range = sub_lo == 0 and math.isinf(sub_hi)
if full_range:
    in_range = pd.Series(True, index=channels_all.index)
else:
    in_range = subs.notna() & (subs >= sub_lo) & ((subs <= sub_hi) if not math.isinf(sub_hi) else True)
channels = channels_all[in_range]
if multi_only:
    channels = channels[channels["video_count"] >= 2]
channels = channels[channels["main_formats"].apply(lambda m: any(f in main_pick for f in m))]
summary_slot.markdown(f"채널 {len(channels):,}개 · 이 채널들의 불닭 쇼츠 {int(channels['video_count'].sum()):,}개 "
                      f"(2개 이상 올린 채널 {int((channels['video_count'] >= 2).sum()):,}개)")
show_observation(obs_slot, lambda: obs.creators(channels))

# 산점도: 영상 하나 = 점 하나, 위 필터에 걸린 채널의 영상만 (기타·구독자 1,000명 미만 제외)
pts = metrics.creator_scatter_data(df, channels["channel_id"])
if not pts.empty:
    cs = go.Figure()
    lo = min(pts["subscriber_count"].min(), pts["views"].min())
    hi = max(pts["subscriber_count"].max(), pts["views"].max())
    cs.add_trace(go.Scatter(x=[lo / 2, hi * 2], y=[lo / 2, hi * 2], mode="lines", hoverinfo="skip",
                            line=dict(color="#666666", width=1.5, dash="dot"), name="조회수 = 구독자 수"))
    for f in CHART_FORMATS:
        g = pts[pts["format_label"] == f]
        if g.empty:
            continue
        cs.add_trace(go.Scatter(
            x=g["subscriber_count"], y=g["views"], mode="markers", name=f,
            marker=dict(color=FORMAT_COLORS[f], size=9, opacity=0.8, line=dict(width=1.5, color="white")),
            customdata=list(zip(g["title"], g["channel_title"], [f] * len(g))),
            hovertemplate=("<b>%{customdata[0]}</b><br>채널 %{customdata[1]}<br>포맷 %{customdata[2]}"
                           "<br>조회수 %{y:,}<br>구독자 %{x:,}<extra></extra>"),
        ))
    _base_layout(cs, 460, legend=dict(orientation="h", y=1.08, x=0))
    cs.update_layout(showlegend=True, margin=dict(t=30, l=10, r=10, b=10))
    cs.update_xaxes(type="log", title="채널 구독자 수", tickformat="~s", showgrid=True, gridcolor=GRID,
                    range=[math.log10(lo / 2), math.log10(hi * 2)])
    cs.update_yaxes(type="log", title="영상 조회수", tickformat="~s", showgrid=True, gridcolor=GRID,
                    range=[math.log10(lo / 2), math.log10(hi * 2)])
    st.markdown("**포맷 효과인가, 채널 크기 효과인가**",
                help=_help(f"구독자 대비 조회수: {desc['views_per_subscriber']}", "점선 위 = 구독자보다 많이 본 영상"))
    st.plotly_chart(cs, width="stretch")
    st.caption(f"x축 채널 구독자 수, y축 영상 조회수 (두 축 모두 로그) · 선 위의 점은 구독자 수보다 많이 본 영상 (점선 = 조회수와 구독자 수가 같은 선, 구독자 대비 조회수 1) · "
               f"점 하나 = 영상 하나, 영상 {len(pts)}개 · {config.FORMAT_EXCLUDED_FROM_COMPARISON}와 구독자 "
               f"{config.VPS_MIN_SUBSCRIBERS:,}명 미만 채널은 제외 · 위 필터(구독자 범위·영상 수·주력 포맷)를 그대로 따름")

ch_table = pd.DataFrame({
    "채널명": channels["channel_title"],
    "구독자 수": channels["subscriber_count"],
    "영상 수": channels["video_count"],
    "주력 포맷": channels["main_formats"].apply(" / ".join),
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
        "주력 포맷": st.column_config.TextColumn(help="채널 영상 중 가장 많은 포맷 (동률이면 ' / '로 모두 표시, 기타도 그대로)"),
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
if set(main_pick) != set(main_options):
    note.append(f"주력 포맷 {', '.join(main_pick) or '없음'}만")
st.caption(" · ".join(note) + " · 기본 정렬: 구독자 대비 조회수 중앙값 높은 순 (열 제목을 눌러 다시 정렬 가능)")

# ---------------------------------------------------------------- 5. 포맷별 상위 영상
st.subheader("포맷별로 무엇을 참고해 만들까?")
ref_slot = st.empty()
s0, s1, s2 = st.columns([1, 1, 1])
ref_format = s0.selectbox("포맷", ["전체", *format_order])
pool = videos if ref_format == "전체" else videos[videos["format_label"] == ref_format]
sort_key = s1.selectbox("정렬 기준", list(COLUMN_LABELS), format_func=COLUMN_LABELS.get)
top_n = s2.slider("표시 개수", 1, len(pool), min(20, len(pool))) if len(pool) > 1 else len(pool)
top = pool.sort_values(sort_key, ascending=False, na_position="last").head(top_n)
show_observation(ref_slot, lambda: obs.references(top, sort_key))
top_table = pd.DataFrame({
    "제목": top["title"],
    "채널": top["channel_title"],
    "포맷": top["format_label"],
    "업로드일": top["published_at"].dt.date,
    "조회수": top["views"],
    "좋아요": top["like_count"],
    "댓글": top["comment_count"],
    "참여율": top["engagement_rate"],
    "조회수 속도(회/일)": top["views_per_day"],
    "구독자 수": top["subscriber_count"],
    "구독자 대비 조회수": top["views_per_subscriber"],
    "경계 사례": [metrics.boundary_label(a) for a in top["format_agreement"]],
    "회차별 포맷": top["format_trials"],
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
        "경계 사례": st.column_config.TextColumn(
            help=f"포맷 분류 {config.FORMAT_TRIALS}회 결과가 모두 같지 않은 영상. 최종 포맷은 "
                 f"{config.FORMAT_TRIALS}회 중 2회 이상 나온 포맷, 모두 다르면 {config.FORMAT_TIE_FALLBACK}"),
        "회차별 포맷": st.column_config.TextColumn(help="회차별 LLM 분류 결과 (1회차/2회차/3회차)"),
        "링크": st.column_config.LinkColumn(display_text="YouTube에서 열기"),
    },
)
st.caption("빈 칸은 계산에서 제외된 값이며 정렬 시 맨 아래로 감")

# ---------------------------------------------------------------- 6. 세부 분포 (포맷별)
section("세부 분포", lambda: obs.distribution(df))
er = videos.dropna(subset=["engagement_rate"])
er = er[er["format_label"].isin(CHART_FORMATS)]                 # 기타·미분류는 그래프에서 제외
er_formats = [f for f in CHART_FORMATS if (er["format_label"] == f).any()]
c1, c2 = st.columns(2)

box = go.Figure()
for f in er_formats:
    g = er[er["format_label"] == f]
    box.add_trace(go.Box(
        y=g["engagement_rate"], name=f, boxpoints="all", jitter=0.4, pointpos=0,
        marker=dict(color=FORMAT_COLORS[f], size=8, opacity=0.6, line=dict(width=1, color="white")),
        line=dict(color=FORMAT_COLORS[f], width=2), fillcolor=_rgba(FORMAT_COLORS[f], 0.12),
        text=g["title"], customdata=g["views"],
        hovertemplate="<b>%{text}</b><br>참여율 %{y:.2%}<br>조회수 %{customdata:,}<extra></extra>",
    ))
box.update_layout(title="포맷별 참여율 분포", showlegend=False, height=460,
                  yaxis=dict(title="참여율", tickformat=".0%"), margin=dict(t=50, l=10, r=10))
c1.plotly_chart(box, width="stretch")

if er_formats:
    ncols = 2 if len(er_formats) > 1 else 1
    nrows = (len(er_formats) + ncols - 1) // ncols
    scatter = make_subplots(rows=nrows, cols=ncols, subplot_titles=er_formats,
                            horizontal_spacing=0.08, vertical_spacing=0.16)
    for i, f in enumerate(er_formats):
        g = er[er["format_label"] == f]
        scatter.add_trace(go.Scatter(
            x=g["views"], y=g["engagement_rate"], mode="markers", name=f, text=g["title"],
            marker=dict(color=FORMAT_COLORS[f], size=8, opacity=0.75, line=dict(width=1, color="white")),
            hovertemplate="<b>%{text}</b><br>조회수 %{x:,}<br>참여율 %{y:.2%}<extra></extra>",
        ), row=i // ncols + 1, col=i % ncols + 1)
    # 모든 패널에 같은 x·y 범위 고정 (패널끼리 바로 비교할 수 있게)
    x_lo, x_hi = math.log10(er["views"].min()), math.log10(er["views"].max())
    x_pad = max(0.1, (x_hi - x_lo) * 0.05)
    y_hi = er["engagement_rate"].max()
    scatter.update_xaxes(type="log", tickformat="~s", range=[x_lo - x_pad, x_hi + x_pad])
    scatter.update_yaxes(tickformat=".0%", range=[0, y_hi * 1.08 if y_hi > 0 else 0.01])
    scatter.update_layout(title="조회수(로그 축) vs 참여율 — 포맷별 (모든 패널 같은 축 범위)",
                          showlegend=False, height=460, margin=dict(t=70, l=10, r=10))
    c2.plotly_chart(scatter, width="stretch")
st.caption(f"차트는 참여율을 계산할 수 있는 영상만 표시 ({metrics.exclusion_text(df, 'engagement_rate')}). "
           f"점 하나 = 영상 하나 · {config.FORMAT_EXCLUDED_FROM_COMPARISON}는 그래프에서 제외(비교 제외).")

# ---------------------------------------------------------------- 데이터 기준 안내
st.subheader("데이터 기준")
REFRESH_NOTE = (f"스냅샷 데이터 — 배포 앱은 자동 수집 안 함, 수집 후 {config.RETENTION_DAYS}일이 지나면 표시하지 않음"
                if SNAPSHOT_MODE else f"마지막 수집 후 {config.REFRESH_HOURS}시간이 지나면 앱을 열 때 새로 검색")
sc = basis.get("search_criteria", {})
rule = basis["short_rule"]
fmt_ts = lambda t: t.strftime("%Y-%m-%d %H:%M UTC") if t is not None else "-"  # noqa: E731
st.markdown(
    f"""
**수집 기준** `{basis['criteria']}`
- 검색: {sc.get('order', '-')}순(관련도순), 지역 {sc.get('region_code', '-')}, 언어 {sc.get('relevance_language', '-')}
- 업로드 기간: 수집 시점 기준 {config.PUBLISHED_MIN_DAYS_AGO}~{config.PUBLISHED_MAX_DAYS_AGO}일 전
  ({sc.get('published_after', '-')} ~ {sc.get('published_before', '-')})
- 키워드 {', '.join(config.KEYWORDS)} · 쇼츠 최대 {sc.get('max_shorts_per_keyword', '-')}개 (검색 최대 {sc.get('max_pages', '-')}페이지)
- 사용한 수집: 가장 최근 성공 search 실행 `{basis.get('used_search_run') or '-'}`
  ({REFRESH_NOTE})
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
- 포맷: {desc['format']}
- 기타 구성 (제목·설명·해시태그 규칙으로 센 참고 값, 분류 결과는 그대로): {metrics.other_breakdown_text(df)}
- 포맷 확정: 영상마다 {config.FORMAT_TRIALS}회 분류해 2회 이상 나온 포맷으로 정함, 모두 다르면 {config.FORMAT_TIE_FALLBACK} · {metrics.agreement_text(df)}
- 긍정 비율: {desc['positive_rate']}
- 부정 비율: {desc['negative_rate']}
- 구매의향 비율: {desc['purchase_rate']}
- 댓글 분석 기준: {desc['comment_basis']}

**크리에이터 표**
- 기본 최솟값 1,000명: 구독자가 아주 적은 채널은 발굴 대상으로 의미가 없어 기본 제외함. 구독자 {config.VPS_MIN_SUBSCRIBERS:,}명 미만 채널은 구독자 대비 조회수를 계산하지 않음(빈 칸).
"""
)

# ---------------------------------------------------------------- 해석 주의
st.subheader("해석 주의")
st.warning("\n".join(f"- {line}" for line in config.CAVEATS))
