"""metrics.py 계산 규칙 검증 (임시 SQLite 사용, API 호출 없음)."""
import json
import math
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import db  # noqa: E402
import metrics  # noqa: E402

V2 = "v2_relevance_3to30d"
R1, R2, R3 = "2026-11-01T00:00:00Z", "2026-11-05T00:00:00Z", "2026-11-09T00:00:00Z"


def _video(conn, vid, channel, published, is_short=1):
    db.upsert_video(conn, {
        "video_id": vid, "title": f"title {vid}", "description": "", "hashtags": [],
        "published_at": published, "duration_sec": 30, "channel_id": channel,
        "embed_width": 480, "embed_height": 853, "has_shorts_tag": 0, "is_short": is_short,
    }, R1)


def _run(conn, run_id, mode, criteria):
    db.start_run(conn, run_id, mode, run_id, criteria, {"criteria": criteria})
    db.finish_run(conn, run_id, run_id, 0, "ok", None)


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "t.db"
    conn = db.connect(path)
    _run(conn, R1, "search", V2)
    _run(conn, R2, "refresh", V2)
    _run(conn, R3, "search", "v1_date_latest")

    # A: 좋아요 숨김 / B: 업로드 0.2일 후 수집 / C: 구독자 비공개 / D: 같은 run 채널 스냅샷 없음
    # E: 두 키워드에 속함 / F: v1 기준 영상 / G: 쇼츠 아님
    for vid, ch, pub in [("A", "c1", "2026-10-20T00:00:00Z"), ("B", "c1", "2026-10-31T19:12:00Z"),
                         ("C", "c2", "2026-10-20T00:00:00Z"), ("D", "c3", "2026-10-20T00:00:00Z"),
                         ("E", "c1", "2026-10-20T00:00:00Z"), ("F", "c1", "2026-10-20T00:00:00Z")]:
        _video(conn, vid, ch, pub)
    _video(conn, "G", "c1", "2026-10-20T00:00:00Z", is_short=0)

    for vid in "ABCDEG":
        db.link_run_keyword(conn, R1, "k1", vid)
    db.link_run_keyword(conn, R1, "k2", "E")
    db.link_run_keyword(conn, R3, "k1", "F")

    snap = db.insert_video_snapshot
    snap(conn, R1, "A", R1, 1000, None, 10)
    snap(conn, R1, "B", R1, 500, 10, 0)
    snap(conn, R1, "C", R1, 1000, 10, 10)
    snap(conn, R1, "D", R1, 100, 1, 1)
    snap(conn, R2, "D", R2, 300, 3, 3)          # D의 최신 스냅샷 = R2
    snap(conn, R1, "E", R1, 2000, 100, 100)
    snap(conn, R1, "G", R1, 9999, 1, 1)
    snap(conn, R3, "F", R3, 9999, 1, 1)

    for cid in ("c1", "c2", "c3"):
        db.upsert_channel(conn, cid, cid, R1)
    db.insert_channel_snapshot(conn, R1, "c1", R1, 1000, False)
    db.insert_channel_snapshot(conn, R1, "c2", R1, None, True)
    db.insert_channel_snapshot(conn, R1, "c3", R1, 100, False)                       # 4일 전
    db.insert_channel_snapshot(conn, R3, "c3", "2026-11-06T00:00:00Z", 999, False)   # 1일 후 (더 가까움)
    conn.commit()
    conn.close()
    return path


def _row(df, vid, keyword="k1"):
    return df[(df.video_id == vid) & (df.keyword == keyword)].iloc[0]


def test_only_criteria_shorts_loaded(db_path):
    df = metrics.load(db_path, V2)
    assert set(df.video_id) == {"A", "B", "C", "D", "E"}   # F(v1), G(쇼츠 아님) 제외


def test_missing_likes_excluded_not_zero(db_path):
    a = _row(metrics.load(db_path, V2), "A")
    assert math.isnan(a.engagement_rate)
    assert a.engagement_excluded_reason == metrics.REASONS["likes_hidden"]


def test_engagement_rate_value(db_path):
    e = _row(metrics.load(db_path, V2), "E")
    assert e.engagement_rate == pytest.approx(200 / 2000)


def test_elapsed_days_floor(db_path):
    b = _row(metrics.load(db_path, V2), "B")
    assert b.elapsed_days == pytest.approx(0.2)
    assert b.views_per_day == pytest.approx(500 / 1)


def test_hidden_subscribers_excluded(db_path):
    c = _row(metrics.load(db_path, V2), "C")
    assert math.isnan(c.views_per_subscriber)
    assert c.views_per_subscriber_excluded_reason == metrics.REASONS["subscribers_hidden"]


def test_latest_snapshot_and_nearest_subscriber(db_path):
    d = _row(metrics.load(db_path, V2), "D")
    assert d.views == 300                      # 최신 스냅샷(R2)
    assert d.subscriber_count == 999           # 같은 run 없음 → 가장 가까운 시점
    assert d.views_per_subscriber == pytest.approx(300 / 999)


def test_same_run_subscriber_preferred(db_path):
    a = _row(metrics.load(db_path, V2), "A")
    assert a.channel_run_id == R1 and a.subscriber_count == 1000


def test_summary_and_exclusions_dedupe(db_path):
    df = metrics.load(db_path, V2)
    s = metrics.summary_by_keyword(df).set_index("keyword")
    assert s.loc["k1", "video_count"] == 5
    assert s.loc["k2", "video_count"] == 1
    assert s.loc["전체", "video_count"] == 5    # E는 한 번만
    assert s.loc["전체", "engagement_rate_n"] == 4
    ex = metrics.exclusion_summary(df)
    total = ex[(ex.metric == "참여율") & (ex.reason == "합계")]["count"].item()
    assert total == 1
    assert metrics.excluded_count(df, "views_per_subscriber") == 1
    assert metrics.exclusion_text(df, "engagement_rate") == "계산 제외 1개 — 좋아요 숨김 1개"
    assert metrics.exclusion_text(df, "views") == "제외 없음"


def test_data_basis(db_path):
    b = metrics.data_basis(db_path, V2)
    assert b["search_criteria"] == {"criteria": V2}
    assert [r["mode"] for r in b["runs"]] == ["search", "refresh"]
    json.dumps(b["short_rule"])


def test_summary_by_channel(db_path):
    ch = metrics.summary_by_channel(metrics.load(db_path, V2))
    assert list(ch.channel_id) == ["c1", "c3", "c2"]          # 중앙값 높은 순, 비공개(NaN)는 맨 아래
    c1 = ch.iloc[0]
    assert c1.video_count == 3 and not c1.single_video        # E는 두 키워드에 있어도 1개로 셈
    assert c1.views_per_subscriber_median == pytest.approx(1.0)   # A 1.0, B 0.5, E 2.0
    assert c1.rep_video_id == "E"                               # 구독자 대비 조회수 최고
    assert c1.engagement_rate_n == 2                            # A는 좋아요 숨김으로 제외
    c3 = ch.iloc[1]
    assert c3.single_video and c3.subscriber_count == 999
    c2 = ch.iloc[2]
    assert c2.subscriber_hidden and math.isnan(c2.views_per_subscriber_median)
    assert c2.rep_video_id == "C"                               # 계산 불가 → 조회수 최고 영상
