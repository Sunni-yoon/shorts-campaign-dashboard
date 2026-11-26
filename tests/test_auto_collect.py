"""키 읽기, 자동 수집 판단, 30일 삭제, 최신 실행 선택, 오류 설명 검증 (실제 API 호출 없음)."""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import auto_collect  # noqa: E402
import collect  # noqa: E402
import db  # noqa: E402
import keys  # noqa: E402
import metrics  # noqa: E402
from test_metrics import V2, _run, _video, db_path  # noqa: E402,F401

FAKE_KEY = "FAKE-TEST-KEY-not-a-real-key-0123456789"   # 실제 키 형식(AIza…)과 다르게
NOW = datetime(2026, 11, 26, 12, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------- keys
def test_env_key_first(monkeypatch):
    monkeypatch.setenv(keys.KEY_NAME, "from-env")
    assert keys.get_api_key(dotenv_path=None) == "from-env"


def test_no_key_returns_none(monkeypatch):
    monkeypatch.delenv(keys.KEY_NAME, raising=False)
    assert keys.get_api_key(dotenv_path=None) is None


def test_mask():
    assert keys.mask(f"url?key={FAKE_KEY}&x=1", FAKE_KEY) == "url?key=***&x=1"


# ---------------------------------------------------------------- 판단 로직
def test_needs_collection():
    assert auto_collect.needs_collection(None, NOW)
    assert not auto_collect.needs_collection("2026-11-25T12:00:01Z", NOW)   # 23시간 59분 59초
    assert auto_collect.needs_collection("2026-11-25T12:00:00Z", NOW)       # 정확히 24시간


def _fake_collector(status="ok", error=None, calls=None):
    count = [0]

    def collector(mode, api_key, db_path=None, progress=None):
        if calls is not None:
            calls.append(mode)
        count[0] += 1
        run_id = (NOW + timedelta(seconds=count[0])).strftime("%Y-%m-%dT%H:%M:%SZ")
        conn = db.connect(db_path)
        _run(conn, run_id, "search", V2)
        conn.execute("UPDATE runs SET status = ? WHERE run_id = ?", (status, run_id))
        conn.commit()
        conn.close()
        return {"status": status, "error": error, "run_id": run_id}
    return collector


def test_collects_when_empty_then_fresh(tmp_path):
    path, state, calls = tmp_path / "a.db", auto_collect.CollectorState(), []
    out = auto_collect.ensure_fresh_data(state, path, now=NOW, api_key_getter=lambda: FAKE_KEY,
                                         collector=_fake_collector(calls=calls))
    assert out["action"] == "collected" and calls == ["search"]
    out = auto_collect.ensure_fresh_data(state, path, now=NOW + timedelta(hours=1),
                                         api_key_getter=lambda: FAKE_KEY, collector=_fake_collector(calls=calls))
    assert out["action"] == "none" and calls == ["search"]           # 24시간 안 → 수집 안 함


def test_failure_masked_and_retry_wait(tmp_path):
    path, state, calls = tmp_path / "b.db", auto_collect.CollectorState(), []
    failing = _fake_collector("quota_exceeded", f"HTTP 403 quotaExceeded key={FAKE_KEY}", calls)
    out = auto_collect.ensure_fresh_data(state, path, now=NOW, api_key_getter=lambda: FAKE_KEY, collector=failing)
    assert out["action"] == "failed" and FAKE_KEY not in out["error"] and "***" in out["error"]
    out = auto_collect.ensure_fresh_data(state, path, now=NOW + timedelta(minutes=30),
                                         api_key_getter=lambda: FAKE_KEY, collector=failing)
    assert out["action"] == "retry_wait" and calls == ["search"]     # 60분 안 → 재시도 안 함
    out = auto_collect.ensure_fresh_data(state, path, now=NOW + timedelta(minutes=61),
                                         api_key_getter=lambda: FAKE_KEY, collector=failing)
    assert out["action"] == "failed" and calls == ["search", "search"]


def test_no_key(tmp_path):
    out = auto_collect.ensure_fresh_data(auto_collect.CollectorState(), tmp_path / "c.db", now=NOW,
                                         api_key_getter=lambda: None, collector=_fake_collector())
    assert out["action"] == "no_key"


def test_explain_error():
    assert "할당량" in collect.explain_error("HTTP 403 quotaExceeded: x")
    assert "API 키가 올바르지 않습니다" in collect.explain_error("HTTP 400 keyInvalid: API key not valid")
    assert collect.explain_error(None) == ""


# ---------------------------------------------------------------- 30일 삭제
def test_purge_older_than_30_days(db_path):  # noqa: F811
    conn = db.connect(db_path)
    # fixture 데이터는 2026-11-01 ~ 11-09 수집 → 기준 시각 2026-12-05면 11-05 이전 삭제
    removed = collect.purge_expired(conn, datetime(2026, 12, 5, tzinfo=timezone.utc))
    left_runs = [r[0] for r in conn.execute("SELECT run_id FROM runs ORDER BY run_id")]
    left_videos = {r[0] for r in conn.execute("SELECT video_id FROM videos")}
    conn.close()
    assert removed["runs"] == 1                       # R1(11-01) 삭제, R2(11-05)·R3(11-09) 남음
    assert left_runs == ["2026-11-05T00:00:00Z", "2026-11-09T00:00:00Z"]
    assert left_videos == {"D", "F"}                  # 남은 스냅샷이 있는 영상만


# ---------------------------------------------------------------- 최신 실행만 사용
def test_dashboard_uses_latest_search_run(db_path):  # noqa: F811
    conn = db.connect(db_path)
    r4 = "2026-11-20T00:00:00Z"
    _run(conn, r4, "search", V2)
    _video(conn, "H", "c1", "2026-11-10T00:00:00Z")
    db.link_run_keyword(conn, r4, "k1", "H")
    db.insert_video_snapshot(conn, r4, "H", r4, 100, 1, 1)
    conn.commit()
    conn.close()
    assert set(metrics.load(db_path, V2).video_id) == {"H"}
    assert metrics.data_basis(db_path, V2)["used_search_run"] == r4
