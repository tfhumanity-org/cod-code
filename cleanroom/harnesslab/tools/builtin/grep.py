"""Grep tool (WP2).

Implements spec 03 §2.2 (Grep row, 20k result cap), R3 (``-l``/``-c`` ->
``output_mode``, ``file_path`` -> ``path``, drop ``-r``), R35 (hidden files
included, VCS dirs excluded, max column 500, ``-e`` when the pattern starts with
``-``, brace globs kept intact when splitting glob lists, ``head_limit`` default
250 / 0 = unlimited + ``offset`` with a footer naming the limit, read-deny rules
pushed into ignore globs and results re-filtered).

Design choice: uses ``rg`` when on PATH, else a pure-Python ``re`` walker with
the same output modes, so tests run anywhere. ``ctx.extra["grep_backend"] =
"python"`` (or env ``HARNESSLAB_GREP_BACKEND=python``) forces the fallback.

Other design choices (spec silent)
----------------------------------
* Default ``output_mode`` is ``files_with_matches``; in ``content`` mode line
  numbers are on unless ``-n`` is false.
* ``files_with_matches`` lists newest-modified files first (like Glob).
* The Python fallback understands the regex dialect of Python's ``re`` (close
  to ripgrep's for everyday patterns), skips files with NUL bytes in the first
  8 KiB (binary), and expands ``{a,b}`` braces in globs itself.
"""

from __future__ import annotations

import fnmatch
import os
import re
import shutil
import subprocess
from typing import Any, Iterator

from harnesslab.tools.base import Tool, ToolCallContext, ToolOutput
from harnesslab.tools.builtin.glob import denied_by, read_deny_patterns

DEFAULT_HEAD_LIMIT = 250
MAX_COLUMNS = 500
VCS_DIRS = (".git", ".svn", ".hg", ".bzr", ".jj", ".sl")
RG_TIMEOUT_S = 60

#: Minimal ``type`` -> globs table for the Python fallback.
TYPE_GLOBS: dict[str, list[str]] = {
    "py": ["*.py", "*.pyi"],
    "js": ["*.js", "*.mjs", "*.cjs", "*.jsx"],
    "ts": ["*.ts", "*.tsx", "*.mts", "*.cts"],
    "rust": ["*.rs"],
    "go": ["*.go"],
    "java": ["*.java"],
    "c": ["*.c", "*.h"],
    "cpp": ["*.cpp", "*.cc", "*.cxx", "*.hpp", "*.hh", "*.h"],
    "md": ["*.md", "*.markdown", "*.mdx"],
    "json": ["*.json"],
    "yaml": ["*.yaml", "*.yml"],
    "toml": ["*.toml"],
    "sh": ["*.sh", "*.bash", "*.zsh"],
    "html": ["*.html", "*.htm"],
    "css": ["*.css", "*.scss", "*.less"],
    "rb": ["*.rb"],
    "swift": ["*.swift"],
    "kotlin": ["*.kt", "*.kts"],
    "txt": ["*.txt"],
}


def split_glob_list(globs: str) -> list[str]:
    """Split on commas/spaces but keep ``{a,b}`` brace groups intact (03 R35)."""
    out: list[str] = []
    cur: list[str] = []
    depth = 0
    for ch in globs:
        if ch == "{":
            depth += 1
        elif ch == "}" and depth:
            depth -= 1
        if depth == 0 and (ch == "," or ch.isspace()):
            if cur:
                out.append("".join(cur))
                cur = []
            continue
        cur.append(ch)
    if cur:
        out.append("".join(cur))
    return out


def expand_braces(pattern: str) -> list[str]:
    """``*.{ts,tsx}`` -> ``['*.ts', '*.tsx']`` (nested groups supported)."""
    m = re.search(r"\{([^{}]*)\}", pattern)
    if not m:
        return [pattern]
    out: list[str] = []
    for alt in m.group(1).split(","):
        out += expand_braces(pattern[: m.start()] + alt + pattern[m.end() :])
    return out


class GrepTool(Tool):
    name = "Grep"
    input_schema = {
        "type": "object",
        "properties": {
            "pattern": {"type": "string"},
            "path": {"type": "string"},
            "glob": {"type": "string"},
            "output_mode": {"type": "string", "enum": ["content", "files_with_matches", "count"]},
            "-A": {"type": "integer"},
            "-B": {"type": "integer"},
            "-C": {"type": "integer"},
            "context": {"type": "integer"},
            "-n": {"type": "boolean"},
            "-i": {"type": "boolean"},
            "-o": {"type": "boolean"},
            "type": {"type": "string"},
            "head_limit": {"type": "integer", "minimum": 0},
            "offset": {"type": "integer", "minimum": 0},
            "multiline": {"type": "boolean"},
        },
        "required": ["pattern"],
        "additionalProperties": False,
    }
    max_result_chars = 20_000
    rule_content_field = "path"
    kind = "search"
    search_hint = "search file contents with regex"

    def description(self, ctx: Any = None) -> str:
        return "Search file contents with a regular expression (ripgrep)."

    def prompt(self, model_id: str = "", lean: bool = True) -> str:
        text = (
            "Searches file contents with a regular expression (ripgrep syntax). output_mode: "
            "files_with_matches (default), content (matching lines; supports -A/-B/-C, -n, -o) or count. "
            "Filter with glob (e.g. \"*.{ts,tsx}\") or type (e.g. py). Results are limited by head_limit "
            f"(default {DEFAULT_HEAD_LIMIT}, 0 = unlimited) with offset for paging. Use this tool instead "
            "of running grep or rg through Bash."
        )
        return text if lean else text + (
            "\n\nExamples:\n- Which files mention a function: {\"pattern\": \"def load_config\"}\n"
            "- Show matches with context: {\"pattern\": \"TODO\", \"output_mode\": \"content\", \"-C\": 2}\n"
            "Literal braces must be escaped: `interface\\{\\}`."
        )

    def is_read_only(self, input: dict[str, Any]) -> bool:
        return True

    def is_concurrency_safe(self, input: dict[str, Any]) -> bool:
        return True

    def classifier_projection(self, input: dict[str, Any]) -> str:
        return f"{input.get('pattern', '')} in {input.get('path', '.')}"

    def coerce_input(self, raw: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
        """R3: ``-l``/``-c`` -> output_mode, ``file_path`` -> path, ``-r`` dropped."""
        out, notes = dict(raw), []
        if out.pop("-l", None) is True and "output_mode" not in out:
            out["output_mode"] = "files_with_matches"
            notes.append("Note: '-l' was treated as output_mode='files_with_matches'.")
        if out.pop("-c", None) is True and "output_mode" not in out:
            out["output_mode"] = "count"
            notes.append("Note: '-c' was treated as output_mode='count'.")
        if "file_path" in out and "path" not in out:
            out["path"] = out.pop("file_path")
            notes.append("Note: 'file_path' was treated as 'path'.")
        for flag in ("-r", "-R", "recursive"):
            if flag in out:
                out.pop(flag)
                notes.append(f"Note: '{flag}' was ignored; searches are always recursive.")
        return out, notes

    # ----------------------------------------------------------------- call
    def call(self, input: dict[str, Any], call_ctx: ToolCallContext) -> ToolOutput:
        ctx = call_ctx.session
        base = input.get("path") or str(ctx.cwd)
        if not os.path.isabs(base):
            base = os.path.join(str(ctx.cwd), base)
        if not os.path.exists(base):
            return ToolOutput(data=f"Path does not exist: {base}", is_error=True)
        mode = input.get("output_mode") or "files_with_matches"
        deny = read_deny_patterns(ctx)
        backend = ctx.extra.get("grep_backend") or ctx.env.get("HARNESSLAB_GREP_BACKEND")
        rg = shutil.which("rg") if backend != "python" else None
        try:
            entries = self._run_rg(rg, input, base, mode, deny) if rg else list(self._run_python(input, base, mode, call_ctx))
        except re.error as e:
            return ToolOutput(data=f"Invalid regular expression: {e}", is_error=True)
        except RuntimeError as e:
            return ToolOutput(data=str(e), is_error=True)
        # re-filter against read-deny rules (R35)
        if deny:
            cwd = str(ctx.cwd)
            entries = [e for e in entries if e == "--" or not denied_by(_entry_path(e, mode), deny, cwd)]
        if mode == "files_with_matches":
            entries.sort(key=lambda p: (-_mtime(p), p))

        offset = int(input.get("offset") or 0)
        head_limit = input.get("head_limit")
        head_limit = DEFAULT_HEAD_LIMIT if head_limit is None else int(head_limit)
        page = entries[offset:]
        limited = False
        if head_limit and len(page) > head_limit:
            page, limited = page[:head_limit], True

        if not page:
            return ToolOutput(data="No files found" if mode == "files_with_matches" else "No matches found")
        if mode == "files_with_matches":
            body = f"Found {len(entries)} file{'s' if len(entries) != 1 else ''}\n" + "\n".join(page)
        elif mode == "count":
            total = sum(int(e.rsplit(":", 1)[1]) for e in page if ":" in e)
            body = "\n".join(page) + f"\n\nFound {total} total occurrences across {len(page)} files."
        else:
            body = "\n".join(page)
        if limited or offset:
            body += (
                f"\n\n[Results limited: head_limit={head_limit}, offset={offset}, {len(entries)} entries in total. "
                "Use a larger offset to see more, or head_limit=0 for everything.]"
            )
        return ToolOutput(data=body)

    # ------------------------------------------------------------ ripgrep
    def _run_rg(self, rg: str, input: dict[str, Any], base: str, mode: str, deny: list[str]) -> list[str]:
        args = [rg, "--hidden", "--max-columns", str(MAX_COLUMNS), "--no-config", "--color", "never"]
        for d in VCS_DIRS:
            args += ["--glob", f"!{d}"]
        for pat in deny:
            args += ["--glob", f"!{pat.lstrip('/')}"]
        if input.get("-i"):
            args.append("-i")
        if input.get("multiline"):
            args += ["-U", "--multiline-dotall"]
        if input.get("type"):
            args += ["--type", input["type"]]
        for g in split_glob_list(input.get("glob") or ""):
            args += ["--glob", g]
        if mode == "files_with_matches":
            args.append("-l")
        elif mode == "count":
            args.append("-c")
        else:
            if input.get("-n", True):
                args.append("-n")
            if input.get("-o"):
                args.append("-o")
            ctxn = input.get("-C", input.get("context"))
            if ctxn is not None:
                args += ["-C", str(ctxn)]
            if input.get("-A") is not None:
                args += ["-A", str(input["-A"])]
            if input.get("-B") is not None:
                args += ["-B", str(input["-B"])]
        pattern = input["pattern"]
        args += ["-e", pattern] if pattern.startswith("-") else [pattern]
        args.append(base)
        proc = subprocess.run(args, capture_output=True, text=True, timeout=RG_TIMEOUT_S, errors="replace")
        if proc.returncode not in (0, 1):
            raise RuntimeError(f"ripgrep failed: {proc.stderr.strip()}")
        lines = proc.stdout.splitlines()
        if mode == "count" and os.path.isfile(base):
            lines = [f"{base}:{ln}" for ln in lines]
        return lines

    # ------------------------------------------------------ python fallback
    def _iter_files(self, base: str, input: dict[str, Any]) -> Iterator[str]:
        globs = [g for raw in split_glob_list(input.get("glob") or "") for g in expand_braces(raw)]
        type_globs = TYPE_GLOBS.get(input.get("type") or "", [])
        if input.get("type") and not type_globs:
            raise RuntimeError(f"Unknown file type: {input['type']}")

        def wanted(path: str) -> bool:
            rel = os.path.relpath(path, base) if os.path.isdir(base) else os.path.basename(path)
            name = os.path.basename(path)
            if type_globs and not any(fnmatch.fnmatch(name, g) for g in type_globs):
                return False
            pos = [g for g in globs if not g.startswith("!")]
            neg = [g[1:] for g in globs if g.startswith("!")]
            match = lambda g: fnmatch.fnmatch(rel, g) if "/" in g else fnmatch.fnmatch(name, g)  # noqa: E731
            if any(match(g) for g in neg):
                return False
            return not pos or any(match(g) for g in pos)

        if os.path.isfile(base):
            if wanted(base):
                yield base
            return
        for root, dirs, files in os.walk(base):
            dirs[:] = sorted(d for d in dirs if d not in VCS_DIRS)
            for f in sorted(files):
                p = os.path.join(root, f)
                if wanted(p):
                    yield p

    def _run_python(self, input: dict[str, Any], base: str, mode: str, call_ctx: ToolCallContext) -> Iterator[str]:
        flags = re.MULTILINE
        if input.get("-i"):
            flags |= re.IGNORECASE
        if input.get("multiline"):
            flags |= re.DOTALL
        rx = re.compile(input["pattern"], flags)
        number = input.get("-n", True)
        ctxn = input.get("-C", input.get("context"))
        after = input.get("-A", ctxn) or 0
        before = input.get("-B", ctxn) or 0
        for path in self._iter_files(base, input):
            if call_ctx.abort.aborted:
                return
            try:
                with open(path, "rb") as fh:
                    raw = fh.read()
            except OSError:
                continue
            if b"\x00" in raw[:8192]:
                continue
            text = raw.decode("utf-8", "replace")
            if mode == "files_with_matches":
                if rx.search(text):
                    yield path
                continue
            if mode == "count":
                n = len(rx.findall(text)) if input.get("multiline") else sum(1 for ln in text.splitlines() if rx.search(ln))
                if n:
                    yield f"{path}:{n}"
                continue
            yield from self._content_lines(path, text, rx, number, before, after, bool(input.get("-o")))

    @staticmethod
    def _content_lines(path: str, text: str, rx: re.Pattern, number: bool, before: int, after: int, only: bool) -> Iterator[str]:
        lines = text.splitlines()
        hits = [i for i, ln in enumerate(lines) if rx.search(ln)]
        if not hits:
            return
        show: dict[int, bool] = {}
        for i in hits:
            for j in range(max(0, i - before), min(len(lines), i + after + 1)):
                show.setdefault(j, False)
            show[i] = True
        last = None
        for j in sorted(show):
            if last is not None and j > last + 1 and (before or after):
                yield "--"
            last = j
            is_match = show[j]
            sep = ":" if is_match else "-"
            line = lines[j]
            if only and is_match:
                for m in rx.finditer(line):
                    yield f"{path}{sep}{j + 1}{sep}{m.group(0)}" if number else f"{path}{sep}{m.group(0)}"
                continue
            if len(line) > MAX_COLUMNS:
                line = f"[Omitted long line: {len(line)} characters]"
            yield f"{path}{sep}{j + 1}{sep}{line}" if number else f"{path}{sep}{line}"


def _entry_path(entry: str, mode: str) -> str:
    if mode == "files_with_matches":
        return entry
    if entry == "--":
        return ""
    # "path:..." - split at the first ':' or '-' that follows an existing path
    for i, ch in enumerate(entry):
        if ch in ":-" and os.path.exists(entry[:i]) and not os.path.isdir(entry[:i]):
            return entry[:i]
    return entry.split(":", 1)[0]


def _mtime(path: str) -> float:
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0.0
