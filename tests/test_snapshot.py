"""스냅샷 만들기·받기·30일 판단·토큰 가리기 (네트워크 호출 없음, 가짜 fetcher 사용)."""
import hashlib
import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import config  # noqa: E402
import db  # noqa: E402
import keys  # noqa: E402
import make_snapshot  # noqa: E402
import metrics  # noqa: E402
import snapshot  # noqa: E402
from test_metrics import V2, db_path  # noqa: E402,F401

FAKE_TOKEN = "github_pat_FAKE0123456789abcdefFAKE"     # 실제 토큰이 아님


def _getter(values):
    return lambda name: values.get(name)


def _build(db_path, tmp_path):  # noqa: F811
    out = tmp_path / "snap.db"
    conn = db.connect(db_path)                   # fixture에 댓글 1개 넣어 본문 제거 확인
    conn.execute("INSERT INTO comment_fetches (video_id, status, comment_count, fetched_at) "
                 "VALUES ('A', 'ok', 1, '2026-11-09T00:00:00Z')")
    conn.execute("INSERT INTO video_comments (comment_key, video_id, rank, text, like_count, collected_at) "
                 "VALUES ('k1', 'A', 1, 'secret text', 0, '2026-11-09T00:00:00Z')")
    conn.commit()
    conn.close()
    manifest = make_snapshot.build(db_path, out, criteria=V2)
    return out, manifest


def test_build_keeps_only_latest_criteria_run(db_path, tmp_path):  # noqa: F811
    out, manifest = _build(db_path, tmp_path)
    c = sqlite3.connect(out)
    assert c.execute("SELECT DISTINCT criteria FROM runs").fetchall() == [(V2,)]
    assert c.execute("SELECT run_id FROM runs ORDER BY run_id").fetchall() == [
        ("2026-11-01T00:00:00Z",), ("2026-11-05T00:00:00Z",)]                 # search + 이후 refresh, R3(v1) 제외
    assert c.execute("SELECT text FROM video_comments").fetchall() == [("",)]  # 댓글 본문 남기지 않음
    c.close()
    assert manifest["run_id"] == manifest["collected_at"] and manifest["comment_text_included"] is False
    pd_a = metrics.summary_by_format(metrics.load(db_path, V2))
    pd_b = metrics.summary_by_format(metrics.load(out, V2))
    assert pd_a.equals(pd_b)                                                  # 같은 결과


def _fake_repo(snap_path: Path, sha: str | None = None, fail: Exception | None = None):
    data = snap_path.read_bytes()
    manifest = {"file": "shorts_snapshot.db", "sha256": sha or hashlib.sha256(data).hexdigest(), "criteria": V2}
    calls = []

    def fetcher(repo, token, path):
        calls.append((repo, path))
        if fail:
            raise fail
        return json.dumps(manifest).encode() if path == "snapshot.json" else data
    return fetcher, calls


def test_ensure_snapshot_downloads_when_empty(db_path, tmp_path, monkeypatch):  # noqa: F811
    monkeypatch.setattr(config, "CRITERIA", V2)
    out, _ = _build(db_path, tmp_path)
    target = tmp_path / "deploy" / "shorts.db"
    fetcher, calls = _fake_repo(out)
    g = _getter({"DATA_REPO": "owner/popow-demo-data", "DATA_TOKEN": FAKE_TOKEN})
    r = snapshot.ensure_snapshot(target, getter=g, fetcher=fetcher)
    assert r["action"] == "downloaded" and snapshot.has_data(target)
    assert [p for _, p in calls] == ["snapshot.json", "shorts_snapshot.db"]
    r = snapshot.ensure_snapshot(target, getter=g, fetcher=fetcher)
    assert r["action"] == "none" and len(calls) == 2                          # 이미 있으면 다시 받지 않음


def test_ensure_snapshot_rejects_bad_hash_and_masks_token(db_path, tmp_path, monkeypatch):  # noqa: F811
    monkeypatch.setattr(config, "CRITERIA", V2)
    out, _ = _build(db_path, tmp_path)
    target = tmp_path / "deploy" / "shorts.db"
    g = _getter({"DATA_REPO": "owner/popow-demo-data", "DATA_TOKEN": FAKE_TOKEN})
    fetcher, _ = _fake_repo(out, sha="0" * 64)
    r = snapshot.ensure_snapshot(target, getter=g, fetcher=fetcher)
    assert r["action"] == "failed" and not target.exists() and "해시" in r["error"]
    fetcher, _ = _fake_repo(out, fail=RuntimeError(f"HTTP Error 401 Authorization: Bearer {FAKE_TOKEN}"))
    r = snapshot.ensure_snapshot(target, getter=g, fetcher=fetcher)
    assert r["action"] == "failed" and FAKE_TOKEN not in r["error"] and "***" in r["error"]
    assert "읽기 권한" in snapshot.explain_error(r["error"])


def test_ensure_snapshot_settings(tmp_path):
    target = tmp_path / "shorts.db"
    assert snapshot.ensure_snapshot(target, getter=_getter({}))["action"] == "no_key"
    bad = _getter({"DATA_REPO": "not a repo/../x", "DATA_TOKEN": FAKE_TOKEN})
    assert snapshot.ensure_snapshot(target, getter=bad)["action"] == "bad_repo"


def test_mask_github_tokens():
    assert FAKE_TOKEN not in keys.mask(f"x {FAKE_TOKEN} y")
    assert "ghp_" not in keys.mask("token ghp_ABCDEFGHIJKLMNOP1234")
    assert keys.mask("Authorization: Bearer abc.def") == "Authorization: Bearer ***"


def test_expiry_boundary():
    t0 = datetime(2026, 11, 26, 16, 26, 19, tzinfo=timezone.utc)
    assert not snapshot.is_expired(t0, t0 + timedelta(days=30) - timedelta(hours=1))
    assert snapshot.is_expired(t0, t0 + timedelta(days=30))
    assert snapshot.is_expired(None)


def test_snapshot_mode_flag():
    assert snapshot.is_snapshot_mode(_getter({"SNAPSHOT_MODE": "1"}))
    assert not snapshot.is_snapshot_mode(_getter({}))
    assert not snapshot.is_snapshot_mode(_getter({"SNAPSHOT_MODE": "0"}))
