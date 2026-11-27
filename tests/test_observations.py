"""observations.py: 관찰 문장의 숫자가 metrics 결과와 일치하는지 검증."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import metrics  # noqa: E402
import observations as obs  # noqa: E402
from test_metrics import V2, db_path  # noqa: E402,F401  (fixture 재사용)


def test_baseline(db_path):  # noqa: F811
    text = obs.baseline(metrics.load(db_path, V2))
    assert text.startswith("쇼츠 5개")
    assert "(계산 가능 4개)" in text


def test_topics_and_creators(db_path):  # noqa: F811
    df = metrics.load(db_path, V2)
    assert "구독자 대비 조회수 중앙값 최고 k2 2.00" in obs.topics(metrics.summary_by_keyword(df))
    text = obs.creators(metrics.summary_by_channel(df))
    assert text.startswith("채널 3개 (영상 2개 이상 1개)")
    assert "1위 c1 1.00 (구독자 1,000명, 영상 3개)" in text


def test_references_and_distribution(db_path):  # noqa: F811
    df = metrics.load(db_path, V2)
    top = metrics.dedupe_videos(df).sort_values("views", ascending=False).head(2)
    text = obs.references(top, "views")
    assert text.startswith("조회수 1위: title E (2,000)")
    assert "포맷 분포: 미분류 2개" in text                      # 키워드 대신 포맷 분포 (fixture는 미분류)
    assert obs.distribution(df).startswith("참여율 범위 2.00% ~ ")


def test_no_recommendation_words(db_path):  # noqa: F811
    df = metrics.load(db_path, V2)
    texts = [obs.baseline(df), obs.topics(metrics.summary_by_keyword(df)),
             obs.creators(metrics.summary_by_channel(df)), obs.distribution(df)]
    for word in ("추천", "좋", "유망", "해야"):
        assert all(word not in t for t in texts)
