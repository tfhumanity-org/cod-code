"""The runnable examples stay runnable (they double as documentation)."""

from __future__ import annotations

import io
import subprocess
import sys
from pathlib import Path

import pytest

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


@pytest.mark.skipif(sys.platform == "win32", reason="examples use a POSIX shell")
@pytest.mark.parametrize("script", sorted(p.name for p in EXAMPLES.glob("*.py")))
def test_example_runs(script):
    proc = subprocess.run([sys.executable, str(EXAMPLES / script)], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip()


def test_cli_demo_round_trip(tmp_path):
    from harnesslab.cli import main

    out = io.StringIO()
    code = main(["--no-persist", "--cwd", str(tmp_path), "--config-dir", str(tmp_path / "cfg")],
                stdin=io.StringIO("hello\nlist *.txt\nexit\n"), stdout=out)
    text = out.getvalue()
    assert code == 0 and "You said: hello" in text and "-> Glob" in text and text.count("[turn ended: completed]") == 2
