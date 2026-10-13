"""Write tool (WP2).

Implements spec 03 §2.2 (Write row), R3 (``file_text``/``file_content``/``text``/
``body`` coerced to ``content``), R17-R20 (ledger: existing files must have been
read fully; result says the view is current), R34 (subagent may not write
REPORT*/SUMMARY*/FINDINGS*/ANALYSIS* markdown files), R33 (.ipynb must go through
NotebookEdit; preserve encoding/EOL when overwriting). Calls
``ctx.file_history.before_edit(path)`` first when checkpointing is on (05 R46).

Design choices (spec silent)
----------------------------
* Overwriting keeps the existing file's encoding and BOM; if the old file used
  CRLF and the new content has bare ``\\n`` line endings, they are converted.
* Since the model supplied the whole content, the ledger records it as seen in
  full (``model_saw_full=True``).
"""

from __future__ import annotations

import os
from typing import Any

from harnesslab.tools.base import Tool, ToolCallContext, ToolErrorCode, ToolOutput, ValidationResult
from harnesslab.tools.builtin.edit import checkpoint, note_touched, resolve_path
from harnesslab.tools.builtin.read import encode_text, read_text_file
from harnesslab.tools.ledger import StalenessVerdict, verdict_message

CONTENT_ALIASES = ("file_text", "file_content", "text", "body")
REPORT_PREFIXES = ("REPORT", "SUMMARY", "FINDINGS", "ANALYSIS")
PROJECTION_MAX_CHARS = 4000


def is_report_file(path: str) -> bool:
    base = os.path.basename(path)
    return base.lower().endswith((".md", ".markdown")) and base.upper().startswith(REPORT_PREFIXES)


class WriteTool(Tool):
    name = "Write"
    input_schema = {
        "type": "object",
        "properties": {"file_path": {"type": "string"}, "content": {"type": "string"}},
        "required": ["file_path", "content"],
        "additionalProperties": False,
    }
    rule_content_field = "file_path"
    kind = "edit"
    search_hint = "create or overwrite a file"

    def description(self, ctx: Any = None) -> str:
        return "Create a new file or overwrite an existing one."

    def prompt(self, model_id: str = "", lean: bool = True) -> str:
        text = (
            "Writes the full content of a file, creating it (and parent directories) if needed. An "
            "existing file must be Read first in this conversation. Prefer Edit for changing part of a "
            "file. Do not create documentation or README files unless asked."
        )
        if lean:
            return text
        return text + (
            '\n\nExample: {"file_path": "/repo/src/util.py", "content": "def add(a, b):\\n    return a + b\\n"}'
        )

    def coerce_input(self, raw: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
        out, notes = dict(raw), []
        if "content" not in out:
            for alias in CONTENT_ALIASES:
                if alias in out:
                    out["content"] = out.pop(alias)
                    notes.append(f"Note: the input field '{alias}' was treated as 'content'.")
                    break
        if "file_path" not in out and "path" in out:
            out["file_path"] = out.pop("path")
            notes.append("Note: the input field 'path' was treated as 'file_path'.")
        return out, notes

    def validate_input(self, input: dict[str, Any], call_ctx: ToolCallContext) -> ValidationResult:
        ctx = call_ctx.session
        path = resolve_path(input["file_path"], ctx)
        if path.lower().endswith(".ipynb"):
            return ValidationResult.fail(
                "Jupyter notebooks (.ipynb) must be changed with the NotebookEdit tool, not Write.",
                ToolErrorCode.NOTEBOOK_REQUIRES_NOTEBOOK_EDIT,
            )
        if not ctx.is_main_agent and is_report_file(path):
            return ValidationResult.fail(
                f"Subagents must not write report files such as {os.path.basename(path)}. Return the "
                "content as text in your final message instead; the agent that launched you reads that.",
                ToolErrorCode.REPORT_FILE_FROM_SUBAGENT,
            )
        if os.path.isdir(path):
            return ValidationResult.fail(f"{path} is a directory, not a file.", ToolErrorCode.INPUT_VALIDATION)
        if os.path.exists(path) and ctx.ledger is not None:
            verdict = ctx.ledger.check_before_write(path, None, is_new_file=False, auto_allowed=False)
            if verdict is StalenessVerdict.NOT_READ:
                return ValidationResult.fail(verdict_message(verdict, path), ToolErrorCode.NOT_READ_FIRST)
            if verdict is StalenessVerdict.STALE:
                return ValidationResult.fail(verdict_message(verdict, path), ToolErrorCode.STALE_FILE)
        return ValidationResult()

    def call(self, input: dict[str, Any], call_ctx: ToolCallContext) -> ToolOutput:
        ctx = call_ctx.session
        path = resolve_path(input["file_path"], ctx)
        content: str = input["content"]
        exists = os.path.exists(path)
        encoding, bom = "utf-8", False
        if exists:
            try:
                old = read_text_file(path)
                encoding, bom = old.encoding, old.bom
                if old.eol == "\r\n" and "\r\n" not in content:
                    content = content.replace("\n", "\r\n")
            except (UnicodeDecodeError, OSError):
                pass
        checkpoint(ctx, path)
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "wb") as f:
            f.write(encode_text(content, encoding, bom))
        if ctx.ledger is not None:
            ctx.ledger.after_write(path, content, model_saw_full=True)
        note_touched(ctx, path)
        if exists:
            msg = f"The file {path} has been overwritten."
        else:
            msg = f"File created successfully at: {path}"
        return ToolOutput(data=msg, notes=["Your view of this file is current; no need to Read it again."])

    def classifier_projection(self, input: dict[str, Any]) -> str:
        """04 §4.5: ``file_path: content`` (content capped)."""
        content = str(input.get("content", ""))
        if len(content) > PROJECTION_MAX_CHARS:
            content = content[:PROJECTION_MAX_CHARS] + f"... [{len(content) - PROJECTION_MAX_CHARS} more characters]"
        return f"{input.get('file_path', '')}: {content}"
