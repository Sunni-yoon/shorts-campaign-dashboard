"""포맷 분류·요약·검증 스크립트 테스트 (가짜 분류기 사용, 실제 API 호출 없음)."""
import math
import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import classify_formats as cf  # noqa: E402
import collect  # noqa: E402
import config  # noqa: E402
import db  # noqa: E402
import format_eval  # noqa: E402
import metrics  # noqa: E402
import observations as obs  # noqa: E402
from test_metrics import V2, db_path  # noqa: E402,F401

ANSWERS = {"A": "챌린지", "B": "챌린지", "C": "레시피·조리", "D": "기타", "E": "먹방·ASMR·맛평가"}


class FakeClassifier:
    def __init__(self, answers=ANSWERS, fail=None):
        self.answers, self.fail, self.calls = answers, fail, []

    def classify(self, text):
        vid = text.split("title ")[1].split("\n")[0]          # fixture 제목 = "title <id>"
        self.calls.append(vid)
        if self.fail:
            raise self.fail
        return ({"format": self.answers[vid], "confidence": "높음", "reason": "테스트"},
                {"input": 400, "cached_input": 100, "output": 50, "model": "fake-model"})


def _classify(path, clf):
    conn = db.connect(path)
    try:
        return cf.classify_pending(conn, "sk-test-REALKEY", classifier=clf)
    finally:
        conn.close()


def test_parse_response():
    assert cf.parse_response('{"format":"챌린지","confidence":"중간","reason":"x"}')["format"] == "챌린지"
    with pytest.raises(cf.ParseError):
        cf.parse_response('{"format":"브이로그","confidence":"높음","reason":"x"}')
    with pytest.raises(cf.ParseError):
        cf.parse_response("not json")


def test_classifies_only_pending_and_records_meta(db_path):  # noqa: F811
    clf = FakeClassifier()
    r = _classify(db_path, clf)
    t = config.FORMAT_TRIALS
    assert r["classified"] == 5 and r["failed"] == 0
    assert sorted(clf.calls) == sorted(list("ABCDE") * t)                 # 영상마다 t회, G(쇼츠 아님) 제외
    assert r["cost_usd"] == pytest.approx(5 * t * (300 * 0.10 + 100 * 0.01 + 50 * 0.50) / 1e6)
    conn = db.connect(db_path)
    row = conn.execute("SELECT model, prompt_version, classified_at, agreement, trial_formats "
                       "FROM video_formats WHERE video_id='A'").fetchone()
    n_trials = conn.execute("SELECT COUNT(*) FROM video_format_trials WHERE video_id='A'").fetchone()[0]
    conn.close()
    assert row[0] == "fake-model" and row[1] == config.FORMAT_PROMPT_VERSION and row[2]
    assert row[3] == t and row[4] == "/".join(["챌린지"] * t) and n_trials == t
    clf2 = FakeClassifier()
    assert _classify(db_path, clf2)["pending"] == 0 and clf2.calls == []      # 다시 분류하지 않음


def test_new_prompt_version_reclassifies(db_path, monkeypatch):  # noqa: F811
    _classify(db_path, FakeClassifier())
    monkeypatch.setattr(config, "FORMAT_PROMPT_VERSION", "v2-test")
    assert _classify(db_path, FakeClassifier())["classified"] == 5


def test_auth_error_stops_and_masks(db_path):  # noqa: F811
    err = Exception("AuthenticationError: 401 Incorrect API key provided: sk-ab12****xy89. See docs")
    clf = FakeClassifier(fail=err)
    r = _classify(db_path, clf)
    assert r["stopped"] and r["classified"] == 0 and len(clf.calls) == 1      # 첫 실패에서 중단
    assert "ab12" not in r["errors"][0] and "***" in r["errors"][0]
    assert "OpenAI API 키가 올바르지 않음" in cf.explain_error(r["errors"][0])


def test_format_summary_tables_and_other_share(db_path):  # noqa: F811
    _classify(db_path, FakeClassifier())
    df = metrics.load(db_path, V2)
    s = metrics.summary_by_format(df).set_index("format")
    assert list(s.index) == config.FORMATS                     # 미분류 없음 → 미분류 행 없음
    assert s.loc["챌린지", "video_count"] == 2 and s.loc["챌린지", "share"] == pytest.approx(0.4)
    assert s["sample_insufficient"].all()                       # 모두 5개 미만
    assert "코미디·상황극" not in s.index                       # v2: 4개 포맷
    assert s.loc["먹방·ASMR·맛평가", "video_count"] == 1
    assert metrics.other_share(df) == (pytest.approx(0.2), 1, 5)
    kt = metrics.keyword_format_table(df)
    assert kt.loc["k1", "합계"] == 5 and kt.loc["k2", "먹방·ASMR·맛평가"] == 1
    text = obs.formats(metrics.summary_by_format(df), metrics.other_share(df))
    assert text.startswith("분류 5개 중 가장 많은 포맷 챌린지 2개(40%)") and "기타 비율 20.0% (1/5)" in text


def test_unclassified_row(db_path):  # noqa: F811
    df = metrics.load(db_path, V2)
    s = metrics.summary_by_format(df).set_index("format")
    assert s.loc[metrics.UNCLASSIFIED, "video_count"] == 5 and math.isnan(metrics.other_share(df)[0])


def test_purge_removes_formats(db_path):  # noqa: F811
    from datetime import datetime, timezone
    _classify(db_path, FakeClassifier())
    conn = db.connect(db_path)
    collect.purge_expired(conn, datetime(2026, 12, 5, tzinfo=timezone.utc))
    left = {r[0] for r in conn.execute("SELECT video_id FROM video_formats")}
    conn.close()
    assert left == {"D"}                                        # 남은 스냅샷이 있는 쇼츠만


def test_export_and_evaluate(db_path, tmp_path, monkeypatch):  # noqa: F811
    _classify(db_path, FakeClassifier())
    monkeypatch.setattr(config, "DB_PATH", db_path)
    monkeypatch.setattr(format_eval, "LABEL_DIR", tmp_path)
    monkeypatch.setattr(format_eval, "SAMPLE_PATH", tmp_path / "s.csv")
    monkeypatch.setattr(format_eval, "ERRORS_PATH", tmp_path / "e.csv")
    monkeypatch.setattr(format_eval, "GUIDE_PATH", tmp_path / "g.txt")
    path = format_eval.export(n=5)
    sample = pd.read_csv(path, encoding="utf-8-sig", dtype=str)
    assert "llm" not in sample.columns and "format" not in sample.columns     # LLM 결과 숨김
    truth = {"A": "챌린지", "B": "먹방·ASMR·맛평가", "C": "레시피·조리", "D": "기타", "E": "먹방·ASMR·맛평가"}
    sample["label"] = sample["video_id"].map(truth)
    sample.to_csv(path, index=False, encoding="utf-8-sig")
    r = format_eval.evaluate(path)
    assert r["n"] == 5 and r["accuracy"] == pytest.approx(0.8)
    assert r["confusion"].loc["먹방·ASMR·맛평가", "챌린지"] == 1
    pf = r["per_format"].set_index("format")
    assert pf.loc["챌린지", "정밀도"] == pytest.approx(0.5) and pf.loc["먹방·ASMR·맛평가", "재현율"] == pytest.approx(0.5)
    assert len(pd.read_csv(tmp_path / "e.csv", encoding="utf-8-sig")) == 1
    with pytest.raises(FileExistsError):
        format_eval.export(n=5)                                 # 라벨 파일 덮어쓰기 방지


def test_format_summary_total_row(db_path):  # noqa: F811
    _classify(db_path, FakeClassifier())
    df = metrics.load(db_path, V2)
    s = metrics.summary_by_format(df, total_label="전체").set_index("format")
    assert list(s.index) == [*config.FORMATS, "전체"]           # 전체 행은 맨 아래
    videos = metrics.dedupe_videos(df)
    assert s.loc["전체", "video_count"] == len(videos) == 5
    assert s.loc["전체", "views_median"] == pytest.approx(videos["views"].median())
    assert math.isnan(s.loc["전체", "share"]) and not s.loc["전체", "sample_insufficient"]
    text = obs.formats(metrics.summary_by_format(df, total_label="전체"), metrics.other_share(df))
    assert text.startswith("분류 5개 중")                       # 전체 행은 포맷 비교에서 빠짐


def test_reactions_by_format_has_total(db_path):  # noqa: F811
    _classify(db_path, FakeClassifier())
    r = metrics.summary_reactions(metrics.load(db_path, V2), by="format")
    assert list(r["group"]) == [*config.FORMATS, "전체"]
    assert r.loc[r["group"] == "전체", "video_count"].iloc[0] == 5


def _row(title, desc="", tags="", conf="높음"):
    return {"title": title, "description": desc, "hashtags": tags, "format_confidence": conf}


def test_other_group_rules():
    g = metrics.other_group
    assert g(_row("DIY Buldak", tags='["papercraft"]')) == "종이 DIY"
    assert g(_row("Ranking buldak made of PAPER")) == "종이 DIY"            # 앞 규칙 우선
    assert g(_row("Ranking Truffle Flavor Buldak!")) == "랭킹·모음"
    assert g(_row("x", desc="rank everything")) != "랭킹·모음"              # 랭킹은 제목만 봄
    assert g(_row("Your Birthday Month = Your Buldak")) == "밈"
    assert g(_row("Your Birth Month Picks Your Buldak!")) == "밈"
    assert g(_row("funny", tags='["meme"]')) == "밈"
    assert g(_row("Buldak Spring Roll", conf="낮음")) == "판단 불가 (제목·설명에 정보 부족)"
    assert g(_row("eat #rockpaperscissors", conf="중간")) == "그 외"         # 가위바위보는 종이 DIY 아님


def test_other_breakdown_and_note(db_path):  # noqa: F811
    _classify(db_path, FakeClassifier())
    df = metrics.load(db_path, V2)
    b = metrics.other_breakdown(df)
    assert list(b) == ["종이 DIY", "랭킹·모음", "밈", "판단 불가 (제목·설명에 정보 부족)", "그 외"] and sum(b.values()) == 1   # 기타 = D
    assert metrics.other_breakdown_text(df).endswith("(기타 1개)")
    assert metrics.format_note("기타", True).startswith("비교 제외")        # 비교 제외가 표본 부족보다 우선
    assert metrics.format_note("챌린지", True) == "표본 부족" and metrics.format_note("챌린지") == ""


# ---------------------------------------------------------------- v3.1: 회차 분류·다수결
def _t(fmt, conf="높음", reason="r"):
    return {"format": fmt, "confidence": conf, "reason": reason}


def test_majority_rules():
    m = cf.majority
    assert m([_t("챌린지")] * 3)["agreement"] == 3
    r = m([_t("레시피·조리", "중간", "r1"), _t("챌린지", "높음", "r2"), _t("챌린지", "낮음", "r3")])
    assert (r["format"], r["confidence"], r["reason"], r["agreement"]) == ("챌린지", "높음", "r2", 2)
    assert r["trial_formats"] == "레시피·조리/챌린지/챌린지"
    r = m([_t("챌린지"), _t("레시피·조리"), _t("먹방·ASMR·맛평가")])
    assert (r["format"], r["confidence"], r["agreement"]) == ("기타", "낮음", 1)   # 모두 다르면 기타


def test_schema_order_reason_first():
    assert list(cf.RESPONSE_SCHEMA["properties"]) == ["reason", "format", "confidence"]
    assert cf.RESPONSE_SCHEMA["required"] == ["reason", "format", "confidence"]


class FlakyClassifier(FakeClassifier):
    """영상 A의 2회차 호출만 실패."""

    def classify(self, text):
        vid = text.split("title ")[1].split("\n")[0]
        if vid == "A" and self.calls.count("A") == 1:
            self.calls.append(vid)
            raise cf.ParseError("JSON이 아님")
        return super().classify(text)


def test_failed_trial_retried_only(db_path):  # noqa: F811
    r = _classify(db_path, FlakyClassifier())
    assert r["classified"] == 4 and r["failed"] == 1                     # A는 최종 저장 안 함
    conn = db.connect(db_path)
    assert conn.execute("SELECT COUNT(*) FROM video_formats WHERE video_id='A'").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM video_format_trials WHERE video_id='A'").fetchone()[0] == 2
    conn.close()
    clf = FakeClassifier()
    r = _classify(db_path, clf)
    assert r["classified"] == 1 and clf.calls == ["A"]                   # 빠진 회차 1번만 다시
    conn = db.connect(db_path)
    assert conn.execute("SELECT agreement FROM video_formats WHERE video_id='A'").fetchone()[0] == 3
    conn.close()


def test_boundary_label_and_text(db_path):  # noqa: F811
    assert metrics.boundary_label(3) == "" and metrics.boundary_label(None) == ""
    assert metrics.boundary_label(2) == "경계 (2/3)" and metrics.boundary_label(1) == "경계 (3회 모두 다름)"
    _classify(db_path, FakeClassifier())
    df = metrics.load(db_path, V2)
    assert metrics.agreement_text(df).startswith("포맷 분류 3회 일치 5개 (100.0%) · 경계 사례 0개")


# ---------------------------------------------------------------- 그래프 데이터
def test_main_formats_ties_and_other():
    mk = lambda fs: pd.DataFrame({"format": fs})  # noqa: E731
    assert metrics.main_formats(mk(["챌린지", "챌린지", "기타"])) == ["챌린지"]
    assert metrics.main_formats(mk(["기타", "레시피·조리"])) == ["레시피·조리", "기타"]   # 동률 → 둘 다, 정의 순서
    assert metrics.main_formats(mk(["기타"])) == ["기타"]                               # 기타 주력은 그대로
    assert metrics.main_formats(mk([None])) == [metrics.UNCLASSIFIED]


def test_format_comparison_excludes_other(db_path):  # noqa: F811
    _classify(db_path, FakeClassifier())
    comp = metrics.format_comparison(metrics.load(db_path, V2))
    assert metrics.chart_formats() == ["챌린지", "레시피·조리", "먹방·ASMR·맛평가"]
    assert "기타" not in set(comp["format"])
    assert list(comp["metric"].unique()) == [m[0] for m in metrics.COMPARISON_METRICS]
    vpd = comp[comp["metric"] == "views_per_day"].set_index("format")
    assert vpd["is_top"].sum() >= 1 and vpd.loc[vpd["value"].idxmax(), "is_top"]
    assert (vpd["n"] == [2, 1, 1]).all()                                  # 챌린지 A·B, 레시피 C, 먹방 E
    assert set(metrics.summary_by_channel(metrics.load(db_path, V2))["main_formats"].explode()) <= {
        *config.FORMATS, metrics.UNCLASSIFIED}


def test_format_comparison_tie_all_top():
    import numpy as np
    df = pd.DataFrame({"metric": ["m"] * 3, "value": [1.0, 1.0, 0.5]})
    best = df.groupby("metric")["value"].transform("max")
    assert list(np.isclose(df["value"], best)) == [True, True, False]   # format_comparison과 같은 판정


def test_creator_scatter_excludes_other_and_small(db_path):  # noqa: F811
    _classify(db_path, FakeClassifier())
    pts = metrics.creator_scatter_data(metrics.load(db_path, V2))
    assert "기타" not in set(pts["format_label"])
    assert (pts["subscriber_count"] >= config.VPS_MIN_SUBSCRIBERS).all()


# ---------------------------------------------------------------- 부트스트랩
def _boot_df(values_by_format, channels_per_format=None):
    import numpy as np
    rows = []
    for f, vals in values_by_format.items():
        chans = (channels_per_format or {}).get(f) or [f"{f}-c{i}" for i in range(len(vals))]
        for i, (v, ch) in enumerate(zip(vals, chans)):
            row = {"keyword": "k", "video_id": f"{f}-{i}", "format": f, "channel_id": ch}
            row.update({k: v for k, _ in metrics.BOOT_METRICS})
            rows.append(row)
    df = pd.DataFrame(rows)
    return df.astype({k: float for k, _ in metrics.BOOT_METRICS}) if len(df) else df.assign(**{
        k: np.nan for k, _ in metrics.BOOT_METRICS})


def test_bootstrap_reproducible_and_contains_point():
    import numpy as np
    rng = np.random.default_rng(0)
    df = _boot_df({f: list(rng.normal(10, 2, 30)) for f in metrics.chart_formats()} | {"기타": [100.0] * 5})
    a = metrics.bootstrap_format_ci(df, n_boot=2000, seed=7)
    b = metrics.bootstrap_format_ci(df, n_boot=2000, seed=7)
    pd.testing.assert_frame_equal(a, b)                                        # 같은 시드 → 같은 결과
    assert set(a["format"]) == set(metrics.chart_formats())                    # 기타 제외
    assert len(a) == len(metrics.BOOT_METRICS) * 3 and (a["n"] == 30).all()
    assert ((a["low"] <= a["value"]) & (a["value"] <= a["high"])).all()
    c = metrics.bootstrap_format_ci(df, n_boot=2000, seed=8)
    assert not a["low"].equals(c["low"])                                       # 시드가 다르면 달라짐


def test_bootstrap_diffs_separated_vs_same():
    import numpy as np
    rng = np.random.default_rng(1)
    same = list(rng.normal(10, 1, 40))
    df = _boot_df({"챌린지": same, "레시피·조리": list(np.array(same) + 50), "먹방·ASMR·맛평가": same})
    d = metrics.bootstrap_format_diffs(df, n_boot=2000, seed=3)
    assert len(d) == len(metrics.BOOT_METRICS) * 3
    far = d[(d["a"] == "챌린지") & (d["b"] == "레시피·조리")]
    assert (~far["includes_zero"]).all() and (far["verdict"] == metrics.VERDICT_SURE).all()
    eq = d[(d["a"] == "챌린지") & (d["b"] == "먹방·ASMR·맛평가")]
    assert eq["includes_zero"].all() and (eq["verdict"] == metrics.VERDICT_NONE).all()
    assert ((d["adj_low"] <= d["low"]) & (d["high"] <= d["adj_high"])).all()   # 보정 구간이 더 넓음


def test_bootstrap_empty_format_and_channel_unit():
    import numpy as np
    df = _boot_df({"챌린지": [1.0, 2.0, 3.0, 4.0], "레시피·조리": [5.0, 6.0, 7.0, 8.0]},
                  {"챌린지": ["x", "x", "y", "y"], "레시피·조리": ["p", "p", "p", "q"]})
    ci = metrics.bootstrap_format_ci(df, n_boot=500, seed=1, unit="channel").set_index(["metric", "format"])
    row = ci.loc[("views", "먹방·ASMR·맛평가")]
    assert row["n"] == 0 and np.isnan(row["low"]) and np.isnan(row["high"])    # 영상 없는 포맷
    d = metrics.bootstrap_format_diffs(df, n_boot=500, seed=1, unit="channel")
    assert d[d["b"] == "먹방·ASMR·맛평가"]["verdict"].eq(metrics.VERDICT_NONE).all()
    s = metrics.bootstrap_samples(df, n_boot=500, seed=1, unit="channel")
    point, boot, n = s[("views", "챌린지")]
    # 채널 x(1,2)·y(3,4)를 통째로 뽑으므로 가능한 중앙값은 1.5, 2.5, 3.5 셋뿐
    assert n == 4 and set(np.round(boot, 6)) <= {1.5, 2.5, 3.5}


def test_sure_pairs_text():
    f1, f2, f3 = metrics.chart_formats()
    diffs = pd.DataFrame({
        "metric": ["m1", "m1", "m1", "m2"], "a": [f1, f1, f2, f1], "b": [f2, f3, f3, f2],
        "diff": [5.0, -2.0, 1.0, 3.0],
        "verdict": [metrics.VERDICT_SURE, metrics.VERDICT_SURE, metrics.VERDICT_MAYBE, metrics.VERDICT_MAYBE],
    })
    assert metrics.sure_pairs(diffs, "m1") == [(f1, f2), (f3, f1)]            # 부호로 방향, 우연일 수 있음은 제외
    short = {f1: "챌린지", f2: "레시피", f3: "먹방"}
    assert metrics.sure_pairs_text(diffs, "m1", short) == "확실한 차이: 챌린지 > 레시피 · 먹방 > 챌린지"
    assert metrics.sure_pairs_text(diffs, "m2", short) == metrics.NO_SURE_LABEL == "포맷 간 확실한 차이 없음"
