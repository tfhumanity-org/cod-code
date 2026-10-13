"""Permission pipeline playground: one table, every mode, no model needed.

Runs a handful of tool calls through the real ``permissions.pipeline.decide``
(hooks -> rules -> tool check / shell analysis -> mode, 04 §4.1) and prints
what each permission mode does with them. ``ask`` means a human (or the host)
would be prompted. Run from the ``cleanroom`` directory::

    uv run python examples/permission_playground.py

Things worth noticing:

* read-only calls are allowed everywhere (04#T4);
* ``rm -rf ~`` stays ``ask`` even in bypass mode - a bypass-immune safety
  check (04 R16, R19) - and ``dontAsk`` turns every ask into a deny;
* the deny rule ``Bash(git push:*)`` wins in every mode (deny > ask > allow);
* auto mode would send the remaining asks to the classifier (not run here:
  the auto column is omitted because it needs a model).
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from harnesslab.model.scripted import ScriptedModel  # noqa: E402
from harnesslab.permissions.pipeline import decide, describe_reason  # noqa: E402
from harnesslab.permissions.rules import parse_rule  # noqa: E402
from harnesslab.permissions.types import PermissionMode  # noqa: E402
from harnesslab.session import create_session_context  # noqa: E402
from harnesslab.tools.base import ToolCallContext  # noqa: E402
from harnesslab.util.abort import AbortController  # noqa: E402

MODES = [PermissionMode.DEFAULT, PermissionMode.ACCEPT_EDITS, PermissionMode.PLAN, PermissionMode.DONT_ASK, PermissionMode.BYPASS]


def main() -> None:
    root = Path(tempfile.mkdtemp(prefix="harnesslab-perms-"))
    project, config = root / "project", root / "config"
    project.mkdir()
    ctx = create_session_context(ScriptedModel(), cwd=project, config_dir=config, persist=False)
    ctx.permissions.rules.append(parse_rule("Bash(git push:*)", "deny"))
    tools = {t.name: t for t in ctx.tools}

    calls = [
        ("Read", {"file_path": str(project / "app.py")}),
        ("Grep", {"pattern": "TODO"}),
        ("Read", {"file_path": "/etc/hosts"}),
        ("Edit", {"file_path": str(project / "app.py"), "old_string": "a", "new_string": "b"}),
        ("Write", {"file_path": str(project / ".git" / "config"), "content": "x"}),
        ("Bash", {"command": "git log --oneline -5"}),
        ("Bash", {"command": "mkdir build"}),
        ("Bash", {"command": "npm install"}),
        ("Bash", {"command": "git push origin main"}),
        ("Bash", {"command": "rm -rf ~"}),
    ]

    width = 44
    print("call".ljust(width) + "".join(m.value.ljust(18) for m in MODES))
    print("-" * (width + 18 * len(MODES)))
    reasons: dict[str, str] = {}
    for name, inp in calls:
        label = f"{name}({inp.get('command') or inp.get('file_path') or inp.get('pattern')})"
        row = label[: width - 2].ljust(width)
        for mode in MODES:
            ctx.permissions.mode = mode
            cc = ToolCallContext(session=ctx, tool_use_id="t", abort=AbortController().signal)
            d = decide(tools[name], inp, cc)
            row += d.behavior.ljust(18)
            if mode == PermissionMode.DEFAULT:
                reasons[label] = describe_reason(d)
        print(row)
    print("\nwhy (default mode):")
    for label, why in reasons.items():
        print(f"  {label[:50]:<50} {why[:90]}")


if __name__ == "__main__":
    main()
