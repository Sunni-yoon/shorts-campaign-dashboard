"""댓글 수집·반응 분류·집계 테스트 (가짜 API·가짜 분류기, 실제 호출 없음)."""
import hashlib
import math
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import auto_collect  # noqa: E402
import classify_comments as cc  # noqa: E402
import collect  # noqa: E402
import collect_comments as ccol  # noqa: E402
import db  # noqa: E402
import metrics  # noqa: E402
import observations as obs  # noqa: E402
from test_metrics import V2, db_path  # noqa: E402,F401

# fixture 최신 스냅샷 댓글 수: A 10, B 0, C 10, D 3, E 100


def _thread(cid, text, likes=1):
    return {"id": cid, "snippet": {"topLevelComment": {"id": cid, "snippet": {
        "textDisplay": text, "likeCount": likes,
        "authorDisplayName": "Real Person", "authorChannelId": {"value": "UCsecret"},
        "authorProfileImageUrl": "http://img", "publishedAt": "2026-11-01T00:00:00Z"}}}}


COMMENTS = {
    "A": ["love it @john.doe 😍", "looks great", "where can I buy this?", "gross 🤢", "nice", "ok"],
    "D": ["yum", "wow", "hmm"],
    "E": ["need to try", "amazing", "meh", "so good", "🔥🔥"],
}


class FakeFetcher:
    def __init__(self, fail=None):
        self.calls, self.fail = [], fail or {}

    def __call__(self, vid):
        self.calls.append(vid)
        if vid in self.fail:
            raise self.fail[vid]
        if vid == "C":
            raise collect.ApiError("HTTP 403 commentsDisabled: The video has disabled comments.")
        return {"items": [_thread(f"{vid}-{i}", t) for i, t in enumerate(COMMENTS[vid])]}


def fake_label(text):
    pos = any(w in text for w in ("love", "great", "nice", "yum", "wow", "amazing", "good", "🔥", "need"))
    neg = any(w in text for w in ("gross", "meh"))
    buy = any(w in text for w in ("buy", "need to try"))
    return {"sentiment": "부정" if neg else "긍정" if pos else "중립", "purchase_intent": "있음" if buy else "없음"}


class FakeClassifier:
    def __init__(self, fail=None):
        self.calls, self.fail = [], fail

    def classify(self, text):
        self.calls.append(text)
        if self.fail:
            raise self.fail
        return fake_label(text), {"input": 300, "cached_input": 0, "output": 10, "model": "fake-model"}


def _collect(path, fetcher):
    conn = db.connect(path)
    try:
        return ccol.collect_pending(conn, fetcher)
    finally:
        conn.close()


def _classify(path, clf):
    conn = db.connect(path)
    try:
        return cc.classify_pending(conn, "sk-test-REALKEY", classifier=clf, workers=2)
    finally:
        conn.close()


def test_collect_no_personal_info(db_path):  # noqa: F811
    f = FakeFetcher()
    r = _collect(db_path, f)
    assert sorted(f.calls) == ["A", "C", "D", "E"]           # B는 댓글 0개 → 호출 안 함
    assert (r["ok"], r["disabled"], r["no_comments"], r["comments"], r["units"]) == (3, 1, 1, 14, 4)
    conn = db.connect(db_path)
    cols = {row[1] for row in conn.execute("PRAGMA table_info(video_comments)")}
    texts = [row[0] for row in conn.execute("SELECT text FROM video_comments WHERE video_id='A' ORDER BY rank")]
    keys_ = {row[0] for row in conn.execute("SELECT comment_key FROM video_comments")}
    dump = " ".join(str(v) for row in conn.execute("SELECT * FROM video_comments") for v in row)
    conn.close()
    assert cols == {"comment_key", "video_id", "rank", "text", "like_count", "collected_at"}
    assert texts[0] == "love it @user 😍"                     # @멘션 가림
    assert hashlib.sha256(b"A-0").hexdigest() in keys_ and "A-0" not in keys_   # 원본 ID 대신 해시
    for secret in ("Real Person", "UCsecret", "http://img", "john.doe"):
        assert secret not in dump
    assert _collect(db_path, FakeFetcher())["pending"] == 0  # 다시 수집하지 않음


def test_fetch_error_is_retried(db_path):  # noqa: F811
    _collect(db_path, FakeFetcher(fail={"E": collect.ApiError("HTTP 500 backendError")}))
    f = FakeFetcher()
    _collect(db_path, f)
    assert f.calls == ["E"]                                   # 오류였던 영상만 다시 시도


def test_classify_once_and_independent(db_path):  # noqa: F811
    _collect(db_path, FakeFetcher())
    clf = FakeClassifier()
    r = _classify(db_path, clf)
    assert r["classified"] == 14 and r["failed"] == 0 and len(clf.calls) == 14   # 댓글마다 1회 호출
    assert all("\n" not in t for t in clf.calls)              # 다른 댓글을 섞어 보내지 않음
    assert r["model"] == "fake-model"
    clf2 = FakeClassifier()
    assert _classify(db_path, clf2)["pending"] == 0 and clf2.calls == []


def test_classify_auth_error_stops_and_masks(db_path):  # noqa: F811
    _collect(db_path, FakeFetcher())
    clf = FakeClassifier(fail=Exception("AuthenticationError: Incorrect API key provided: sk-ab12****yz89"))
    r = _classify(db_path, clf)
    assert r["stopped"] and r["classified"] == 0 and r["failed"] == 14
    assert "ab12" not in r["errors"][0]


def test_parse_response():
    assert cc.parse_response('{"sentiment":"긍정","purchase_intent":"있음"}')["purchase_intent"] == "있음"
    with pytest.raises(Exception):
        cc.parse_response('{"sentiment":"좋음","purchase_intent":"있음"}')


def test_reaction_rates_and_summary(db_path):  # noqa: F811
    _collect(db_path, FakeFetcher())
    _classify(db_path, FakeClassifier())
    df = metrics.load(db_path, V2)
    a = df[df.video_id == "A"].iloc[0]
    assert a.comments_analyzed == 6
    assert a.positive_rate == pytest.approx(3 / 6)            # love, looks great, nice
    assert a.negative_rate == pytest.approx(1 / 6) and a.purchase_rate == pytest.approx(1 / 6)
    d = df[df.video_id == "D"].iloc[0]
    assert math.isnan(d.positive_rate) and d.reaction_excluded_reason == metrics.REACTION_REASONS["too_few"]
    c = df[df.video_id == "C"].iloc[0]
    assert c.reaction_excluded_reason == metrics.REACTION_REASONS["disabled"]
    s = metrics.summary_reactions(df, by="keyword").set_index("group")
    k1 = s.loc["k1"]
    assert (k1.video_count, k1.videos_analyzed, k1.comments_analyzed) == (5, 2, 11)   # A, E
    assert (k1.excluded_too_few, k1.excluded_no_comments) == (1, 2)                   # D / B, C
    e_pos = 4 / 5                                             # need to try, amazing, so good, 🔥 (meh 부정)
    assert k1.positive_rate_mean == pytest.approx((0.5 + e_pos) / 2)
    assert s.loc["전체", "video_count"] == 5                  # E 중복 제거
    assert obs.reactions(metrics.summary_reactions(df)).startswith("영상 2개(댓글 11개) 분석")


def test_purge_removes_comment_data(db_path):  # noqa: F811
    _collect(db_path, FakeFetcher())
    _classify(db_path, FakeClassifier())
    conn = db.connect(db_path)
    collect.purge_expired(conn, datetime(2100, 1, 1, tzinfo=timezone.utc))
    counts = [conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
              for t in ("video_comments", "comment_reactions", "comment_fetches")]
    conn.close()
    assert counts == [0, 0, 0]


def test_ensure_reactions_pipeline(db_path):  # noqa: F811
    state = auto_collect.CollectorState()
    out = auto_collect.ensure_reactions(state, db_path, youtube_key_getter=lambda: None,
                                        openai_key_getter=lambda: None)
    assert out["comments"]["action"] == "no_key"
    out = auto_collect.ensure_reactions(state, db_path, youtube_key_getter=lambda: "yt", openai_key_getter=lambda: "ai",
                                        fetcher=FakeFetcher(), classifier=FakeClassifier())
    assert out["comments"]["action"] == "collected" and out["reactions"]["action"] == "classified"
    assert out["reactions"]["result"]["classified"] == 14


def test_failed_calls_counted_in_cost(db_path):  # noqa: F811
    import classify_formats
    _collect(db_path, FakeFetcher())
    err = classify_formats.ParseError("JSON이 아님: Extra data")
    err.usage = {"input": 300, "cached_input": 0, "output": 60}
    r = _classify(db_path, FakeClassifier(fail=err))
    assert r["classified"] == 0 and r["failed"] == 14 and not r["stopped"]
    assert r["usage"]["input"] == 14 * 300 and r["cost_usd"] > 0     # 실패해도 과금된 토큰은 집계


def test_sentiment_definitions_in_prompt():
    import classify_comments as cc
    assert list(cc.SENTIMENT_DEFINITIONS) == ["긍정", "부정", "중립"]          # 프롬프트 줄 순서 유지
    for k, v in cc.SENTIMENT_DEFINITIONS.items():
        assert f"- {k}: {v}\n" in cc.SYSTEM_PROMPT                             # 대시보드 설명과 같은 문구
