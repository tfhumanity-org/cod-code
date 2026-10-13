"""Edit tool (WP2).

Implements spec 03 §2.2 (Edit row), R3 (``old_str``/``new_str``/``path``
coercion), R17-R20 (ledger staleness incl. stale-recovered warning), R27-R33
(matching via ``tools.edit_match``; 1 GiB cap; BOM / UTF-8 / UTF-16LE / line
endings preserved; ``.ipynb`` refused), 04 §4.5 (classifier projection:
file_path + added/removed lines). Calls ``ctx.file_history.before_edit``
(05 R46) before writing.

Design choices (spec silent)
----------------------------
* Relative paths are resolved against the session cwd.
* Matching happens on text with ``\\n`` line endings: CRLF files are normalised
  before matching (and ``old_string``/``new_string`` too) and converted back on
  write, so the model never has to type ``\\r``.
* "Auto-allowed by permissions" (R19) = the session is in ``acceptEdits`` or
  ``bypassPermissions`` mode, or a whole-tool ``Edit`` allow rule exists. Path-
  specific rule matching belongs to WP3 and is not consulted here.
* Validation pre-computes the edit so a doomed edit (not found / ambiguous)
  fails before anyone is asked for permission; ``call`` recomputes on the
  then-current file.
"""

from __future__ import annotations

import os
from typing import Any

from harnesslab.permissions.types import PermissionMode
from harnesslab.runtime import SessionContext
from harnesslab.tools.base import Tool, ToolCallContext, ToolErrorCode, ToolOutput, ValidationResult
from harnesslab.tools.builtin.read import TextFile, encode_text, read_text_file
from harnesslab.tools.edit_match import EditOutcome, apply_edit_text
from harnesslab.tools.ledger import StalenessVerdict, verdict_message

MAX_EDIT_BYTES = 1024**3
MARKDOWN_EXTENSIONS = (".md", ".mdx", ".markdown")
_ALIASES = {"old_str": "old_string", "new_str": "new_string", "path": "file_path", "filename": "file_path"}


# ------------------------------------------------- helpers shared with Write


def resolve_path(path: str, ctx: SessionContext) -> str:
    p = os.path.expanduser(path)
    if not os.path.isabs(p):
        p = os.path.join(str(ctx.cwd), p)
    return os.path.abspath(p)


def edits_auto_allowed(tool_name: str, ctx: SessionContext) -> bool:
    perm = ctx.permissions
    if perm.mode in (PermissionMode.ACCEPT_EDITS, PermissionMode.BYPASS):
        return True
    return any(r.tool_name == tool_name and r.content is None for r in perm.rules_with("allow"))


def checkpoint(ctx: SessionContext, path: str) -> None:
    """05 R46: back the file up before the first change in this snapshot."""
    fh = ctx.file_history
    if fh is not None and hasattr(fh, "before_edit"):
        fh.before_edit(path)


def note_touched(ctx: SessionContext, path: str) -> None:
    if path not in ctx.touched_paths:
        ctx.touched_paths.append(path)


def to_lf(s: str) -> str:
    return s.replace("\r\n", "\n")


class EditTool(Tool):
    name = "Edit"
    input_schema = {
        "type": "object",
        "properties": {
            "file_path": {"type": "string"},
            "old_string": {"type": "string"},
            "new_string": {"type": "string"},
            "replace_all": {"type": "boolean"},
        },
        "required": ["file_path", "old_string", "new_string"],
        "additionalProperties": False,
    }
    rule_content_field = "file_path"
    kind = "edit"
    search_hint = "replace text in a file"

    def description(self, ctx: Any = None) -> str:
        return "Replace an exact string in a file."

    def prompt(self, model_id: str = "", lean: bool = True) -> str:
        text = (
            "Performs an exact string replacement in a file you have already Read in this conversation. "
            "old_string must match the file text exactly (without Read's line-number prefixes) and must be "
            "unique unless replace_all is true; include surrounding lines to make it unique. Keep the "
            "file's indentation. Prefer editing existing files over creating new ones."
        )
        if lean:
            return text
        return text + (
            "\n\nExample: to rename a variable everywhere in one file, call Edit with "
            '{"file_path": "/repo/a.py", "old_string": "count", "new_string": "total", "replace_all": true}.\n'
            "If the edit fails because the file changed, Read it again and retry with the new text."
        )

    def coerce_input(self, raw: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
        """R3: accept other harnesses' field names, and say what was rewritten."""
        out, notes = dict(raw), []
        for alias, real in _ALIASES.items():
            if alias in out and real not in out:
                out[real] = out.pop(alias)
                notes.append(f"Note: the input field '{alias}' was treated as '{real}'.")
        if isinstance(out.get("replace_all"), str) and out["replace_all"].lower() in ("true", "false"):
            out["replace_all"] = out["replace_all"].lower() == "true"
            notes.append("Note: replace_all was given as a string and converted to a boolean.")
        return out, notes

    def get_path(self, input: dict[str, Any]) -> str | None:
        v = input.get("file_path")
        return v if isinstance(v, str) else None

    # ----------------------------------------------------------- validation
    def _load(self, path: str) -> TextFile:
        return read_text_file(path)

    def _compute(self, input: dict[str, Any], tf: TextFile, path: str) -> EditOutcome:
        return apply_edit_text(
            to_lf(tf.text),
            to_lf(input["old_string"]),
            to_lf(input["new_string"]),
            replace_all=bool(input.get("replace_all", False)),
            is_markdown=path.lower().endswith(MARKDOWN_EXTENSIONS),
        )

    def _staleness(self, path: str, input: dict[str, Any], ctx: SessionContext, exists: bool) -> StalenessVerdict:
        if ctx.ledger is None:
            return StalenessVerdict.OK
        return ctx.ledger.check_before_write(
            path, input["old_string"], is_new_file=not exists, auto_allowed=edits_auto_allowed(self.name, ctx)
        )

    def validate_input(self, input: dict[str, Any], call_ctx: ToolCallContext) -> ValidationResult:
        ctx = call_ctx.session
        path = resolve_path(input["file_path"], ctx)
        if path.lower().endswith(".ipynb"):
            return ValidationResult.fail(
                "Jupyter notebooks (.ipynb) must be changed with the NotebookEdit tool, not Edit.",
                ToolErrorCode.NOTEBOOK_REQUIRES_NOTEBOOK_EDIT,
            )
        if input["old_string"] == input["new_string"]:
            return ValidationResult.fail(
                "old_string and new_string are identical, so there is nothing to change.", ToolErrorCode.NO_CHANGE
            )
        exists = os.path.exists(path)
        if not exists:
            if input["old_string"] == "":
                return ValidationResult()  # creating a new file
            return ValidationResult.fail(
                f"File does not exist: {path}. Current working directory: {ctx.cwd}. To create a file, use "
                "Write (or Edit with an empty old_string).",
                ToolErrorCode.FILE_NOT_FOUND,
            )
        if os.path.isdir(path):
            return ValidationResult.fail(f"{path} is a directory, not a file.", ToolErrorCode.INPUT_VALIDATION)
        if os.path.getsize(path) > MAX_EDIT_BYTES:
            return ValidationResult.fail(f"{path} is larger than 1 GiB and cannot be edited.", ToolErrorCode.TOO_LARGE)
        verdict = self._staleness(path, input, ctx, exists)
        if verdict is StalenessVerdict.NOT_READ:
            return ValidationResult.fail(verdict_message(verdict, path), ToolErrorCode.NOT_READ_FIRST)
        if verdict is StalenessVerdict.STALE:
            return ValidationResult.fail(verdict_message(verdict, path), ToolErrorCode.STALE_FILE)
        try:
            tf = self._load(path)
        except UnicodeDecodeError:
            return ValidationResult.fail(f"{path} is not a text file and cannot be edited.", ToolErrorCode.BINARY_FILE)
        r = self._compute(input, tf, path)
        if not r.ok:
            return ValidationResult.fail(r.message, r.error_code)
        return ValidationResult()

    # ----------------------------------------------------------------- call
    def call(self, input: dict[str, Any], call_ctx: ToolCallContext) -> ToolOutput:
        ctx = call_ctx.session
        path = resolve_path(input["file_path"], ctx)
        exists = os.path.exists(path)
        verdict = self._staleness(path, input, ctx, exists)
        if verdict in (StalenessVerdict.NOT_READ, StalenessVerdict.STALE):
            return ToolOutput(data=verdict_message(verdict, path), is_error=True)
        if exists:
            tf = self._load(path)
        else:
            tf = TextFile("", "utf-8", False, "\n")
        r = self._compute(input, tf, path)
        if not r.ok:
            return ToolOutput(data=r.message, is_error=True)
        new_text = r.content.replace("\n", "\r\n") if tf.eol == "\r\n" else r.content
        checkpoint(ctx, path)
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "wb") as f:
            f.write(encode_text(new_text, tf.encoding, tf.bom))
        if ctx.ledger is not None:
            ctx.ledger.after_write(path, new_text, model_saw_full=False)
        note_touched(ctx, path)

        verb = "created" if not exists else "updated"
        plural = "s" if r.replacements != 1 else ""
        msg = f"The file {path} has been {verb} ({r.replacements} replacement{plural})."
        notes = ["Your view of this file is current; there is no need to Read it again to confirm the change."]
        if verdict is StalenessVerdict.OK_STALE_RECOVERED:
            notes.append(verdict_message(verdict, path))
        if r.used_quote_folding:
            notes.append("Note: matched after normalising curly quotes; the replacement keeps the file's quote style.")
        return ToolOutput(
            data=msg,
            notes=notes,
        )

    def classifier_projection(self, input: dict[str, Any]) -> str:
        """04 §4.5: file path plus removed (-) and added (+) lines."""
        old = str(input.get("old_string", ""))
        new = str(input.get("new_string", ""))
        lines = [str(input.get("file_path", ""))]
        lines += [f"- {ln}" for ln in old.splitlines()]
        lines += [f"+ {ln}" for ln in new.splitlines()]
        return "\n".join(lines)
