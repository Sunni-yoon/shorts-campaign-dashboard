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
    assert r["classified"] == 5 and r["failed"] == 0 and sorted(clf.calls) == list("ABCDE")   # G(쇼츠 아님) 제외
    assert r["cost_usd"] == pytest.approx(5 * (300 * 0.10 + 100 * 0.01 + 50 * 0.50) / 1e6)
    conn = db.connect(db_path)
    row = conn.execute("SELECT model, prompt_version, classified_at FROM video_formats WHERE video_id='A'").fetchone()
    conn.close()
    assert row[0] == "fake-model" and row[1] == config.FORMAT_PROMPT_VERSION and row[2]
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
    assert "OpenAI API 키가 올바르지 않습니다" in cf.explain_error(r["errors"][0])


def test_format_summary_tables_and_other_share(db_path):  # noqa: F811
    _classify(db_path, FakeClassifier())
    df = metrics.load(db_path, V2)
    s = metrics.summary_by_format(df).set_index("format")
    assert list(s.index) == config.FORMATS                     # 미분류 없음 → 미분류 행 없음
    assert s.loc["챌린지", "video_count"] == 2 and s.loc["챌린지", "share"] == pytest.approx(0.4)
    assert s["sample_insufficient"].all()                       # 모두 5개 미만
    assert s.loc["코미디·상황극", "video_count"] == 0
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
