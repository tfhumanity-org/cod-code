"""Auto-compaction end to end, then resume from the transcript.

The scripted model reports a large prompt size on its first answer, so the next
round crosses the (deliberately lowered) auto-compaction threshold. The
compactor asks the model for a summary, rewrites the conversation as
boundary + summary + kept rows (05 R22) and the loop appends every new row to
the transcript. A second engine then resumes the session and only sees the
post-boundary history (05 §4.7). Run from the ``cleanroom`` directory::

    uv run python examples/compaction_demo.py
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from harnesslab.model.scripted import ScriptedModel, say  # noqa: E402
from harnesslab.session import Engine, create_session_context  # noqa: E402
from harnesslab.tokens import context_tokens  # noqa: E402
from harnesslab.types import SystemNoticeEvent, Usage  # noqa: E402

ENV = {"CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "20"}  # compact at 20% of the effective window


def main() -> None:
    root = Path(tempfile.mkdtemp(prefix="harnesslab-compact-"))
    project, config = root / "project", root / "config"
    project.mkdir()
    model = ScriptedModel([
        say("I looked at the parser; it is slow on long inputs.", usage=Usage(input_tokens=60_000, output_tokens=40)),
        say("<analysis>private notes</analysis><summary>1. Goal: speed up the parser.\n2. Found: quadratic loop in tokenize().\n3. Next: rewrite it.</summary>"),
        say("Continuing: rewriting tokenize() now."),
    ])
    ctx = create_session_context(model, cwd=project, config_dir=config, env=ENV)
    engine = Engine(ctx)

    engine.run("Why is the parser slow?")
    print(f"after turn 1 the context holds ~{context_tokens(engine.items):,} tokens (anchored on the reported usage)")
    for ev in engine.submit("Fix it."):
        if isinstance(ev, SystemNoticeEvent) and ev.subtype == "compact_boundary":
            print(f"[compact_boundary] {ev.data}")

    print("\nmodel calls:", [r.query_source for r in model.requests])
    print("\ntranscript rows:")
    for line in ctx.transcript_path.read_text().splitlines():
        row = json.loads(line)
        kind = row.get("subtype") or row.get("type")
        text = ""
        msg = row.get("message") or {}
        content = msg.get("content") if isinstance(msg, dict) else None
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            text = " ".join(b.get("text", "") for b in content if isinstance(b, dict))
        if row.get("type") == "attachment":
            text = f"<{row['attachment'].get('type')}>"
        flag = " (summary)" if row.get("isCompactSummary") else ""
        print(f"  {str(kind):<20}{flag:<10} {text[:70]!r}")

    resumed_ctx = create_session_context(ScriptedModel([say("Back again.")]), cwd=project, config_dir=config, env=ENV)
    resumed_ctx.session_id = ctx.session_id
    resumed = Engine.resume(resumed_ctx)
    kinds = [getattr(i, "subtype", None) or getattr(i, "type", None) or i.role for i in resumed.items]
    print(f"\nresumed with {len(resumed.items)} items: {kinds}")
    print("summary present:", any(getattr(i, "is_compact_summary", False) for i in resumed.items))


if __name__ == "__main__":
    main()
