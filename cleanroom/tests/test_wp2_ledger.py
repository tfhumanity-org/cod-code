"""WP2 read ledger: 03 §2.3, R17-R22, §4.2 (03#T3-T5)."""

from __future__ import annotations

import os

from harnesslab.runtime import SessionContext
from harnesslab.tools.ledger import (
    VERBATIM_MAX_BYTES,
    LedgerEntry,
    ReadLedger,
    StalenessVerdict,
    content_hash,
    floor_mtime,
)


def bump_mtime(path, seconds=5):
    st = os.stat(path)
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + seconds * 1_000_000_000))


def full_read(ledger, path):
    ledger.record_read(str(path), path.read_text(), offset=None, limit=None, partial=False)


def test_hash_and_mtime_helpers(tmp_path):
    assert content_hash("abc") == content_hash("abc") != content_hash("abd")
    p = tmp_path / "f"
    p.write_text("x")
    assert isinstance(floor_mtime(str(p)), int)


def test_not_read_refusal_and_exemptions(tmp_path):
    led = ReadLedger()
    p = tmp_path / "a.py"
    p.write_text("x = 1\n")
    assert led.check_before_write(str(p), "x", is_new_file=False, auto_allowed=False) == StalenessVerdict.NOT_READ
    assert led.check_before_write(str(p), "", is_new_file=False, auto_allowed=False) == StalenessVerdict.OK
    assert led.check_before_write(str(tmp_path / "new.py"), "x", is_new_file=True, auto_allowed=False) == StalenessVerdict.OK
    led.record_read(str(p), p.read_text(), offset=None, limit=None, partial=True)
    assert led.check_before_write(str(p), "x", is_new_file=False, auto_allowed=False) == StalenessVerdict.NOT_READ
    full_read(led, p)
    assert led.check_before_write(str(p), "x", is_new_file=False, auto_allowed=False) == StalenessVerdict.OK


def test_touch_only_mtime_with_same_hash_is_ok(tmp_path):
    led = ReadLedger()
    p = tmp_path / "a.py"
    p.write_text("x = 1\n")
    full_read(led, p)
    bump_mtime(p)  # touched / formatter wrote identical bytes
    assert led.check_before_write(str(p), "x = 1", is_new_file=False, auto_allowed=False) == StalenessVerdict.OK
    assert led.get(str(p)).mtime == floor_mtime(str(p))  # refreshed


def test_stale_recovered_and_real_stale(tmp_path):
    led = ReadLedger()
    p = tmp_path / "a.py"
    p.write_text("x = 1\ny = 2\n")
    full_read(led, p)
    p.write_text("x = 1\ny = 3\n")
    bump_mtime(p)
    # unique match + auto-allowed -> recovered
    assert led.check_before_write(str(p), "x = 1", is_new_file=False, auto_allowed=True) == StalenessVerdict.OK_STALE_RECOVERED
    # not auto-allowed -> stale
    assert led.check_before_write(str(p), "x = 1", is_new_file=False, auto_allowed=False) == StalenessVerdict.STALE
    # old string no longer present -> stale
    assert led.check_before_write(str(p), "y = 2", is_new_file=False, auto_allowed=True) == StalenessVerdict.STALE
    # Write (no old_string) on a changed file -> stale
    assert led.check_before_write(str(p), None, is_new_file=False, auto_allowed=True) == StalenessVerdict.STALE


def test_ranged_read_cannot_vouch_by_hash(tmp_path):
    led = ReadLedger()
    p = tmp_path / "a.py"
    p.write_text("a\nb\n")
    led.record_read(str(p), p.read_text(), offset=2, limit=1, partial=False)
    bump_mtime(p)  # same content, but the model only saw line 2
    assert led.check_before_write(str(p), "a", is_new_file=False, auto_allowed=False) == StalenessVerdict.STALE


def test_after_write_updates_entry(tmp_path):
    led = ReadLedger()
    p = tmp_path / "a.py"
    p.write_text("new")
    led.after_write(str(p), "new", model_saw_full=False)
    e = led.get(str(p))
    assert e.content == "new" and e.content_hash == content_hash("new") and e.not_in_model_context
    assert e.mtime == floor_mtime(str(p))
    big = "x" * (VERBATIM_MAX_BYTES + 1)
    p.write_text(big)
    led.after_write(str(p), big, model_saw_full=True)
    assert led.get(str(p)).content is None and not led.get(str(p)).not_in_model_context


def test_read_dedup_stub(tmp_path):
    led = ReadLedger()
    p = tmp_path / "a.md"
    p.write_text("hello")
    assert led.read_dedup(str(p), None, None) is None
    full_read(led, p)
    assert led.read_dedup(str(p), None, None) == "unchanged"
    assert led.read_dedup(str(p), 1, 10) is None  # different range
    bump_mtime(p)
    assert led.read_dedup(str(p), None, None) is None  # changed on disk
    led.record_read(str(p), "hello", offset=None, limit=None, partial=False, seeded=True)
    assert led.read_dedup(str(p), None, None) == "already_in_context"
    led.after_write(str(p), "hello", model_saw_full=False)
    assert led.read_dedup(str(p), None, None) is None  # model never saw the whole file


def test_formatter_note_lists_at_most_five(tmp_path):
    led = ReadLedger()
    files = []
    for i in range(7):
        p = tmp_path / f"f{i}.py"
        p.write_text("x")
        full_read(led, p)
        files.append(p)
    assert led.after_shell("ls -la") is None
    assert led.after_shell("black .") is None  # nothing changed yet
    for p in files:
        bump_mtime(p)
    note = led.after_shell("ruff format src && echo done")
    assert note.startswith("Note: this command modified 7 file(s)")
    assert note.count(".py") == 5 and "(and 2 more)" in note and "Read them again" in note
    for cmd in ("npx prettier --write .", "cargo fmt", "go fmt ./...", "isort x.py", "eslint --fix .", "sed -i 's/a/b/' f"):
        assert led.after_shell(cmd) is not None, cmd
    assert led.after_shell("blackbox run") is None


def test_lru_caps_by_count_and_bytes():
    led = ReadLedger(max_entries=3, max_total_bytes=100)
    for i in range(4):
        led.put(f"/p{i}", LedgerEntry(None, "h", 0, size=10))
    assert [p for p, _ in led.items()] == ["/p1", "/p2", "/p3"]
    led.get("/p1")  # touch -> most recent
    led.put("/big", LedgerEntry(None, "h", 0, size=85))
    # count cap drops /p2, byte cap (10+10+85 > 100) then drops /p3
    assert [p for p, _ in led.items()] == ["/p1", "/big"]
    assert led.total_bytes == 95


def test_snapshot_is_most_recent_first_copy():
    led = ReadLedger()
    led.put("/a", LedgerEntry("a", "h", 1))
    led.put("/b", LedgerEntry("b", "h", 1))
    snap = led.snapshot()
    assert [p for p, _ in snap] == ["/b", "/a"]
    snap[0][1].content = "mutated"
    assert led.get("/b").content == "b"


def test_subagent_clone_drops_seeded(tmp_path, config_dir):
    led = ReadLedger()
    led.put("/seeded", LedgerEntry("x", "h", 1, seeded_from_context=True))
    led.put("/read", LedgerEntry("y", "h", 1))
    ctx = SessionContext(cwd=tmp_path, config_dir=config_dir, ledger=led)
    child = ctx.child(agent_id="a1", agent_type="general-purpose")
    assert child.ledger is not led
    assert [p for p, _ in child.ledger.items()] == ["/read"]
    child.ledger.put("/childonly", LedgerEntry("z", "h", 1))
    assert led.get("/childonly") is None
