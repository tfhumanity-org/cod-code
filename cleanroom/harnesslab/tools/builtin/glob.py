"""Glob tool (WP2).

Implements spec 03 §2.2 (Glob row: <= 100 paths), R36 (sorted by mtime, footer
with exact or lower-bound totals), 04 R9 (file-pattern permission rules) and
04 §4.5 (projection = pattern only). Pure ``pathlib`` implementation; VCS dirs
(.git .svn .hg .bzr .jj .sl) excluded like Grep (03 R35).

Design choices (spec silent)
----------------------------
* Newest first (most recently modified at the top), ties broken by path.
* Counting stops at ``COUNT_CAP`` matches; beyond that the footer gives a lower
  bound ("at least N").
* An absolute pattern is split into its literal directory prefix (used as the
  search root) and the remaining glob.
* Paths matching a ``Read`` deny rule are dropped (simple ``fnmatch`` on the
  absolute and cwd-relative path; full rule semantics are WP3's).
"""

from __future__ import annotations

import fnmatch
import glob as _glob
import os
from pathlib import Path
from typing import Any

from harnesslab.tools.base import Tool, ToolCallContext, ToolOutput

MAX_RESULTS = 100
COUNT_CAP = 10_000
VCS_DIRS = frozenset({".git", ".svn", ".hg", ".bzr", ".jj", ".sl"})


def read_deny_patterns(ctx: Any) -> list[str]:
    """Content of ``Read(...)`` deny rules, used to hide files (03 R35 / 04 R9)."""
    return [r.content for r in ctx.permissions.rules_with("deny") if r.tool_name == "Read" and r.content]


def denied_by(path: str, patterns: list[str], cwd: str) -> bool:
    if not patterns:
        return False
    rel = os.path.relpath(path, cwd)
    for pat in patterns:
        p = pat
        if p.startswith("//"):
            p = p[1:]  # "//abs/path" = absolute path form
        elif p.startswith("~/"):
            p = os.path.expanduser(p)
        if fnmatch.fnmatch(path, p) or fnmatch.fnmatch(rel, p.lstrip("./")) or fnmatch.fnmatch(os.path.basename(path), p):
            return True
    return False


def split_absolute_pattern(pattern: str) -> tuple[str, str]:
    """``/a/b/**/*.py`` -> (``/a/b``, ``**/*.py``)."""
    parts = Path(pattern).parts
    root: list[str] = []
    for i, part in enumerate(parts):
        if _glob.has_magic(part):
            return str(Path(*root)) if root else "/", str(Path(*parts[i:]))
        root.append(part)
    return str(Path(*parts[:-1])), parts[-1]


class GlobTool(Tool):
    name = "Glob"
    input_schema = {
        "type": "object",
        "properties": {"pattern": {"type": "string"}, "path": {"type": "string"}},
        "required": ["pattern"],
        "additionalProperties": False,
    }
    rule_content_field = "path"
    kind = "search"
    search_hint = "find files by glob pattern"

    def description(self, ctx: Any = None) -> str:
        return "Find files by name pattern (e.g. **/*.py)."

    def prompt(self, model_id: str = "", lean: bool = True) -> str:
        text = (
            "Fast file-name matching with glob patterns such as `**/*.ts` or `src/**/test_*.py`. Returns "
            f"up to {MAX_RESULTS} absolute paths, most recently modified first. Use Grep to search file "
            "contents. Run several searches in parallel when exploring."
        )
        return text if lean else text + "\n\nExample: {\"pattern\": \"**/*.md\", \"path\": \"/repo/docs\"}"

    def is_read_only(self, input: dict[str, Any]) -> bool:
        return True

    def is_concurrency_safe(self, input: dict[str, Any]) -> bool:
        return True

    def call(self, input: dict[str, Any], call_ctx: ToolCallContext) -> ToolOutput:
        ctx = call_ctx.session
        pattern: str = input["pattern"]
        base = input.get("path") or str(ctx.cwd)
        if not os.path.isabs(base):
            base = os.path.join(str(ctx.cwd), base)
        if os.path.isabs(pattern):
            base, pattern = split_absolute_pattern(pattern)
        if not os.path.isdir(base):
            return ToolOutput(data=f"Directory does not exist: {base}", is_error=True)
        deny = read_deny_patterns(ctx)
        found: list[tuple[float, str]] = []
        total = 0
        capped = False
        for p in Path(base).glob(pattern):
            if call_ctx.abort.aborted:
                break
            if any(part in VCS_DIRS for part in p.parts):
                continue
            try:
                if not p.is_file():
                    continue
                mtime = p.stat().st_mtime
            except OSError:
                continue
            sp = str(p.absolute())
            if denied_by(sp, deny, str(ctx.cwd)):
                continue
            total += 1
            found.append((mtime, sp))
            if total >= COUNT_CAP:
                capped = True
                break
        if not found:
            return ToolOutput(data="No files found")
        found.sort(key=lambda t: (-t[0], t[1]))
        shown = [p for _, p in found[:MAX_RESULTS]]
        text = "\n".join(shown)
        if total > MAX_RESULTS:
            count = f"at least {total}" if capped else str(total)
            text += (
                f"\n(Showing {MAX_RESULTS} of {count} matching files, newest first. Use a more specific "
                "pattern or path to narrow the results.)"
            )
        return ToolOutput(data=text)

    def classifier_projection(self, input: dict[str, Any]) -> str:
        return str(input.get("pattern", ""))
