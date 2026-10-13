"""WP4 - file checkpoints and rewind (05 R46-R48, §4.9)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from harnesslab.config import Settings
from harnesslab.transcript import checkpoints as CP
from harnesslab.util.ids import hex16_path


@pytest.fixture
def fh(tmp_path: Path) -> CP.FileHistory:
    return CP.FileHistory(tmp_path / "backups")


def test_copy_on_first_touch_and_null_backup(fh, tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("v1\n")
    os.chmod(f, 0o640)
    fh.before_edit(str(f))
    b = fh.tracked[str(f)]
    assert b.backup_file_name == f"{hex16_path(str(f))}@v1" and b.version == 1
    backup = fh.backup_dir / b.backup_file_name
    assert backup.read_text() == "v1\n" and (backup.stat().st_mode & 0o777) == 0o640
    f.write_text("v2\n")
    fh.before_edit(str(f))  # second touch: no new copy
    assert backup.read_text() == "v1\n"
    new = tmp_path / "new.txt"
    fh.before_edit(str(new))
    assert fh.tracked[str(new)].backup_file_name is None


def test_snapshot_dedup(fh, tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("v1")
    fh.before_edit(str(f))
    s1 = fh.snapshot("m1")
    assert s1.tracked_file_backups[str(f)].version == 1  # unchanged -> reuse v1
    f.write_text("v2")
    s2 = fh.snapshot("m2")
    assert s2.tracked_file_backups[str(f)].version == 2
    s3 = fh.snapshot("m3")
    assert s3.tracked_file_backups[str(f)] is s2.tracked_file_backups[str(f)]
    assert len(list(fh.backup_dir.iterdir())) == 2


def test_rewind_restores_and_deletes(fh, tmp_path):
    a = tmp_path / "a.txt"
    a.write_text("one\ntwo\n")
    fh.snapshot("m1")  # prompt 1 (nothing tracked yet)
    fh.before_edit(str(a))  # first touch during turn 1 -> joins snapshot m1
    a.write_text("one\nTWO\nthree\n")
    created = tmp_path / "created.txt"
    fh.before_edit(str(created))
    created.write_text("new file")
    fh.snapshot("m2")
    a.write_text("changed again\n")

    target = fh.find("m1")
    dry = fh.rewind(target, dry_run=True)
    assert dry.files_changed == 2 and dry.insertions == 2 and dry.deletions == 2
    assert a.read_text() == "changed again\n" and created.exists()  # dry run touches nothing

    stats = fh.rewind(target)
    assert stats.files_changed == 2
    assert a.read_text() == "one\ntwo\n" and not created.exists()


def test_rewind_file_first_touched_later(fh, tmp_path):
    a = tmp_path / "late.txt"
    a.write_text("original")
    fh.snapshot("m1")
    fh.snapshot("m2")
    fh.before_edit(str(a))  # touched first during turn 2
    a.write_text("edited")
    fh.snapshot("m3")
    fh.rewind(fh.find("m1"))
    assert a.read_text() == "original"


def test_link_safety(fh, tmp_path):
    real = tmp_path / "real.txt"
    real.write_text("r")
    link = tmp_path / "link.txt"
    os.symlink(real, link)
    fh.before_edit(str(link))
    hard = tmp_path / "hard.txt"
    hard.write_text("h")
    fh.before_edit(str(hard))
    os.link(hard, tmp_path / "hard2.txt")
    s = fh.snapshot("m1")
    real.write_text("changed")
    hard.write_text("changed")
    stats = fh.rewind(s)
    assert stats.skipped == 2 and hard.read_text() == "changed"


def test_rewind_after_rewind_never_overwrites_backups(fh, tmp_path):
    a = tmp_path / "a.txt"
    a.write_text("v1")
    fh.before_edit(str(a))
    s1 = fh.snapshot("m1")
    a.write_text("v2")
    s2 = fh.snapshot("m2")
    fh.rewind(s1)
    a.write_text("v3")
    fh.snapshot("m3")
    fh.rewind(s2)
    assert a.read_text() == "v2"


def test_snapshot_cap_evicts_unreferenced(fh, tmp_path):
    a = tmp_path / "a.txt"
    a.write_text("0")
    fh.before_edit(str(a))
    for i in range(1, CP.MAX_SNAPSHOTS + 6):
        a.write_text(str(i))
        fh.snapshot(f"m{i}")
    assert len(fh.snapshots) == CP.MAX_SNAPSHOTS
    names = {p.name for p in fh.backup_dir.iterdir()}
    live = {b.backup_file_name for s in fh.snapshots for b in s.tracked_file_backups.values()}
    assert names == live | {fh.tracked[str(a)].backup_file_name}


def test_disabled_and_enablement(ctx, tmp_path):
    off = CP.FileHistory(tmp_path / "b", enabled=False)
    f = tmp_path / "x.txt"
    f.write_text("x")
    off.before_edit(str(f))
    assert off.tracked == {}
    assert CP.checkpointing_enabled(ctx)
    ctx.env = {"CLAUDE_CODE_DISABLE_FILE_CHECKPOINTING": "1"}
    assert not CP.checkpointing_enabled(ctx)
    ctx.env, ctx.is_sdk = {}, True
    assert not CP.checkpointing_enabled(ctx)
    ctx.env = {"CLAUDE_CODE_ENABLE_SDK_FILE_CHECKPOINTING": "1"}
    assert CP.checkpointing_enabled(ctx)
    ctx.is_sdk, ctx.settings = False, Settings.from_dict({"fileCheckpointingEnabled": False})
    assert not CP.checkpointing_enabled(ctx)
    assert CP.FileHistory.for_session(ctx).backup_dir == ctx.file_history_dir


def test_snapshot_logged_and_reloaded(fh, tmp_path):
    rows = []

    class Store:
        def append_metadata(self, t, v):
            rows.append((t, v))

    fh.transcript = Store()
    a = tmp_path / "a.txt"
    a.write_text("v1")
    fh.before_edit(str(a))
    fh.snapshot("m1")
    assert rows[0][0] == "file-history-snapshot" and rows[0][1]["messageId"] == "m1"
    fresh = CP.FileHistory(fh.backup_dir)
    fresh.load_snapshots([v for _, v in rows])
    a.write_text("v2")
    fresh.rewind(fresh.find("m1"))
    assert a.read_text() == "v1"
