"""Tests of the per-item dedup (watch) — PURE logic, domain-agnostic.

The pure file is imported by path (without dragging in FastAPI/SQLAlchemy).
Runnable: `python3 tests/test_watch_lifecycle.py` from assets/backend/.
"""
import importlib.util
import pathlib

_WL = pathlib.Path(__file__).resolve().parent.parent / "app" / "watch_lifecycle.py"
_spec = importlib.util.spec_from_file_location("watch_lifecycle", _WL)
wl = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(wl)

diff = wl.diff_watch_items


def test_new_item_is_fresh():
    d = diff({}, [{"key": "A", "sig": "1"}], "key", "sig")
    assert [i["key"] for i in d["fresh"]] == ["A"]
    assert d["ongoing"] == [] and d["resolved"] == []


def test_same_item_is_ongoing_not_refire():
    stored = {"A": {"signature": "1", "state": "open"}}
    d = diff(stored, [{"key": "A", "sig": "1"}], "key", "sig")
    assert d["fresh"] == [] and d["escalated"] == []
    assert d["ongoing"] == ["A"]


def test_signature_change_is_escalated():
    stored = {"A": {"signature": "1", "state": "open"}}
    d = diff(stored, [{"key": "A", "sig": "2"}], "key", "sig")
    assert [i["key"] for i in d["escalated"]] == ["A"]
    assert d["fresh"] == []


def test_disappeared_is_resolved():
    stored = {"A": {"signature": "1", "state": "open"}}
    d = diff(stored, [], "key", "sig")
    assert d["resolved"] == ["A"]


def test_reappeared_after_resolved_is_fresh_anti_fn():
    stored = {"A": {"signature": "1", "state": "resolved"}}
    d = diff(stored, [{"key": "A", "sig": "1"}], "key", "sig")
    assert [i["key"] for i in d["fresh"]] == ["A"], d


def test_no_signature_field_dedup_by_key_only():
    stored = {"A": {"signature": None, "state": "open"}}
    d = diff(stored, [{"key": "A", "x": 99}], "key", None)
    assert d["ongoing"] == ["A"] and d["fresh"] == []


def test_missing_key_is_skipped():
    d = diff({}, [{"nokey": 1}, {"key": "B"}], "key", None)
    assert [i["key"] for i in d["fresh"]] == ["B"]


def test_key_ignored_exact_and_prefix():
    assert wl.key_ignored("presenton", ["presenton"]) is True
    assert wl.key_ignored("demo|metabase|hourly", ["demo|metabase*"]) is True   # prefijo
    assert wl.key_ignored("demo|42P01|x", ["demo|42P01*"]) is True
    assert wl.key_ignored("demo|real-bug", ["presenton", "demo|metabase*"]) is False
    assert wl.key_ignored(None, ["x"]) is False


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
    print(f"OK watch_lifecycle: {len(fns)}/{len(fns)}")
