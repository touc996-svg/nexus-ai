"""Regression tests for two cache defects that lost held-out evaluation data.

Defect 1 (lost update): every cache object flushed its whole in-memory dict, so a later flush from one object
overwrote entries written by another object on the same file.
Defect 2 (cleanup dropped fresh entries): cleanup kept only entries that had been READ again, so entries written
during the run, and not yet read back, were deleted.

No test here starts a model; the session guard in conftest.py makes any inference attempt fail.
"""
import json
import multiprocessing
from pathlib import Path

import pytest

from evals import ollama
from evals.ollama import CacheMiss, JsonCache, read_json
from evals.prune_cache import collect_usage, prune_files


@pytest.fixture
def cache_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(ollama, "CACHE_DIR", tmp_path)
    return tmp_path


def on_disk(cache_dir, name="c.json"):
    return json.loads((cache_dir / name).read_text())


# ---- defect 1: several cache objects, one file -----------------------------------------------------------

def test_second_cache_object_does_not_overwrite_the_first_ones_entries(cache_dir):
    """The original reproduction: both objects loaded the same empty file, A flushes, then B flushes."""
    a, b = JsonCache("c.json"), JsonCache("c.json")
    a.put("from-a", 1)
    a.flush()
    b.put("from-b", 2)
    b.flush()
    assert on_disk(cache_dir) == {"from-a": 1, "from-b": 2}


def test_flush_order_does_not_matter(cache_dir):
    a, b, c = (JsonCache("c.json") for _ in range(3))
    for obj, key in ((c, "c"), (a, "a"), (b, "b")):
        obj.put(key, key.upper())
    for obj in (b, c, a):  # arbitrary, like atexit order
        obj.flush()
    assert on_disk(cache_dir) == {"a": "A", "b": "B", "c": "C"}


def test_a_stale_object_with_nothing_pending_cannot_wipe_the_file(cache_dir):
    stale = JsonCache("c.json")  # loaded while the file was empty
    writer = JsonCache("c.json")
    writer.put("kept", 1)
    writer.flush()
    stale.flush()  # exit-time flush of an object that wrote nothing
    assert on_disk(cache_dir) == {"kept": 1}


def test_checkpoint_flushes_do_not_clobber_other_objects(cache_dir):
    a, b = JsonCache("c.json"), JsonCache("c.json")
    for i in range(12):  # crosses the checkpoint threshold several times
        a.put(f"a{i}", i)
        b.put(f"b{i}", i)
    a.flush()
    b.flush()
    assert set(on_disk(cache_dir)) == {f"a{i}" for i in range(12)} | {f"b{i}" for i in range(12)}


def test_after_flush_an_object_sees_the_other_objects_entries(cache_dir):
    a, b = JsonCache("c.json"), JsonCache("c.json")
    a.put("x", 1)
    a.flush()
    b.put("y", 2)
    b.flush()
    assert b.get("x") == 1


def test_a_newer_value_for_a_key_still_wins(cache_dir):
    a = JsonCache("c.json")
    a.put("k", "old")
    a.flush()
    b = JsonCache("c.json")
    b.put("k", "new")
    b.flush()
    assert on_disk(cache_dir)["k"] == "new"


def _write_many(args):
    directory, tag = args
    ollama.CACHE_DIR = Path(directory)
    cache = JsonCache("c.json")
    for i in range(60):
        cache.put(f"{tag}-{i}", i)
    cache.flush()


def test_concurrent_processes_lose_no_entries(cache_dir):
    ctx = multiprocessing.get_context("fork")  # POSIX only, like the file locking it exercises
    with ctx.Pool(4) as pool:
        pool.map(_write_many, [(str(cache_dir), tag) for tag in "wxyz"])
    assert len(on_disk(cache_dir)) == 240


# ---- defect 2: cleanup must keep what was written this run ----------------------------------------------

def test_cleanup_keeps_entries_written_this_run_even_if_never_read_back(cache_dir):
    old = JsonCache("c.json")
    old.put("stale-unused", 0)
    old.put("stale-used", 1)
    old.flush()

    def workload():
        cache = JsonCache("c.json")
        cache.get("stale-used")           # read -> used
        cache.put("fresh-never-read", 2)  # written, not yet flushed, never read back

    usage = collect_usage(workload)
    prune_files(usage, apply=True)
    assert set(on_disk(cache_dir)) == {"stale-used", "fresh-never-read"}


def test_cleanup_immediately_after_writes_without_an_explicit_flush(cache_dir):
    seed = JsonCache("c.json")
    seed.put("old-used", 0)
    seed.put("old-unused", 9)
    seed.flush()
    cache = JsonCache("c.json")

    def workload():
        cache.get("old-used")             # puts this file into the usage report
        for i in range(3):                # fewer than the checkpoint size, so everything is still pending
            cache.put(f"k{i}", i)

    usage = collect_usage(workload)
    assert cache.pending, "precondition: writes are still pending when cleanup starts"
    prune_files(usage, apply=True)
    assert set(on_disk(cache_dir)) == {"old-used", "k0", "k1", "k2"}


def test_cleanup_removes_only_unused_entries_and_reports_counts(cache_dir):
    seed = JsonCache("c.json")
    for i in range(5):
        seed.put(f"k{i}", i)
    seed.flush()
    usage = collect_usage(lambda: JsonCache("c.json").get("k1"))
    assert prune_files(usage, apply=False) == {"c.json": (5, 1)}
    assert len(on_disk(cache_dir)) == 5  # dry run changes nothing
    prune_files(usage, apply=True)
    assert set(on_disk(cache_dir)) == {"k1"}


def test_an_exit_time_flush_after_cleanup_cannot_bring_back_removed_entries(cache_dir):
    seed = JsonCache("c.json")
    seed.put("removed", 0)
    seed.put("kept", 1)
    seed.flush()
    holder = JsonCache("c.json")  # loaded everything, will flush again at exit

    usage = collect_usage(lambda: JsonCache("c.json").get("kept"))
    prune_files(usage, apply=True)
    holder.flush()  # nothing pending: must be a no-op
    assert set(on_disk(cache_dir)) == {"kept"}


def test_an_exit_time_flush_after_cleanup_still_adds_its_own_new_entries(cache_dir):
    seed = JsonCache("c.json")
    seed.put("kept", 1)
    seed.flush()
    late = JsonCache("c.json")
    usage = collect_usage(lambda: JsonCache("c.json").get("kept"))
    prune_files(usage, apply=True)
    late.put("written-after-cleanup", 2)
    late.flush()
    assert set(on_disk(cache_dir)) == {"kept", "written-after-cleanup"}


def test_usage_tracking_is_off_by_default_and_reset_after_a_run(cache_dir):
    assert JsonCache.usage is None
    collect_usage(lambda: None)
    assert JsonCache.usage is None
    with pytest.raises(RuntimeError):
        collect_usage(lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    assert JsonCache.usage is None


# ---- the cleanup tool refuses to act on an incomplete cache ------------------------------------------------

def test_prune_cli_refuses_and_changes_nothing_when_the_workload_cannot_be_replayed(cache_dir, monkeypatch, capsys):
    from evals import prune_cache

    (cache_dir / "keep.json").write_text('{"a":1}\n')
    monkeypatch.setattr(prune_cache, "full_workload", lambda: (_ for _ in ()).throw(CacheMiss("missing entry")))
    assert prune_cache.main(["--apply"]) == 1
    assert "Nothing was changed" in capsys.readouterr().err
    assert read_json(cache_dir / "keep.json") == {"a": 1}


# ---- model inference is blocked during tests -----------------------------------------------------------------

def test_inference_is_blocked_in_the_test_session():
    assert ollama.INFERENCE_BLOCKED is True
    with pytest.raises(CacheMiss):
        ollama._post("/api/chat", {"model": "x"})


def test_session_guard_points_every_endpoint_at_a_closed_port():
    import os

    assert "127.0.0.1:9" in os.environ["OLLAMA_BASE_URL"]
    assert "127.0.0.1:9" in os.environ["EVALS_OLLAMA_URL"]
