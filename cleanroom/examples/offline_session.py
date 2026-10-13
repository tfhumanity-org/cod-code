"""A complete offline session: a scripted "model" reads, edits and checks a file.

Everything except the model is real: the loop, the streaming tool executor, the
permission pipeline (with an auto-answering "human"), attachments, the
transcript and file checkpoints. Run from the ``cleanroom`` directory::

    uv run python examples/offline_session.py

What to look for in the output:

* ``Glob`` / ``Read`` / ``cat`` run without a prompt (read-only fast path);
  ``Edit`` asks first (default permission mode, 04 §2.1).
* Tool results arrive right after the assistant row that asked for them.
* The transcript is a JSONL file of uuid-linked rows (05 §2.3).
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from harnesslab.model.scripted import ScriptedModel, call, say, turn  # noqa: E402
from harnesslab.permissions.types import PermissionAnswer  # noqa: E402
from harnesslab.session import Engine, create_session_context  # noqa: E402
from harnesslab.types import MessageEvent, SystemNoticeEvent, TurnEndEvent  # noqa: E402


def main() -> None:
    root = Path(tempfile.mkdtemp(prefix="harnesslab-demo-"))
    project, config = root / "project", root / "config"
    project.mkdir()
    target = project / "greeting.py"
    target.write_text('print("hello world")\n')

    model = ScriptedModel([
        turn("Let me find the Python files.", call("Glob", {"pattern": "*.py"})),
        turn("Reading the greeting.", call("Read", {"file_path": str(target)})),
        turn("Changing the greeting.", call("Edit", {"file_path": str(target), "old_string": "world", "new_string": "harness"})),
        turn("Checking the result.", call("Bash", {"command": f"cat {target}", "description": "Show the file"})),
        say("Done: the greeting now says hello harness."),
    ])

    def human(req):
        print(f"    [prompt] allow {req.tool_name}? ({req.decision_reason_text or 'needs approval'}) -> yes")
        return PermissionAnswer("allow")

    ctx = create_session_context(model, cwd=project, config_dir=config, prompter=human)
    engine = Engine(ctx)
    for ev in engine.submit("Please change the greeting to say hello harness."):
        if isinstance(ev, MessageEvent):
            m = ev.message
            if m.role == "assistant":
                print(f"assistant: {m.text()}")
                for b in m.tool_uses():
                    print(f"  -> {b.name} {json.dumps(b.input)}")
            elif m.role == "user" and m.tool_results():
                for r in m.tool_results():
                    first = (r.text().splitlines() or [""])[0]
                    print(f"  <- {first[:100]}")
            elif m.role == "user" and not m.is_meta:
                print(f"user: {m.text()}")
        elif isinstance(ev, SystemNoticeEvent) and ev.text:
            print(f"[{ev.subtype}] {ev.text}")
        elif isinstance(ev, TurnEndEvent):
            print(f"[turn ended: {ev.reason.value}]")

    print(f"\nfile now reads: {target.read_text().strip()}")
    rows = [json.loads(line) for line in ctx.transcript_path.read_text().splitlines()]
    print(f"transcript: {ctx.transcript_path} ({len(rows)} rows)")
    print(f"model calls made: {len(model.requests)}")


if __name__ == "__main__":
    main()
