"""Auto-memory: location, index truncation, write lint, recall (WP4).

Implements spec 02 §2.6 (memory file frontmatter: name, description,
metadata.type in user|feedback|project|reference; MEMORY.md index of one-line
links ~150 chars), R22 (location ``<configDir>/projects/<slug>/memory/``,
``autoMemoryDirectory`` override precedence), R23 (enablement:
``CLAUDE_CODE_DISABLE_AUTO_MEMORY`` truthy off / falsy force-on,
``autoMemoryEnabled``; off in bare mode), R24 (index truncated at 200 lines or
25 000 bytes with a warning), R25 (memory section content -
``memory_prompt_section``, used by ``context.prompt``), R26 (team stores under
``memory/team/<mount>/``; reject null bytes, encoded ``..``, normalisation
tricks, symlink escapes), R27 (write lint: refuse invisible/control chars or
harness-tag-shaped text; background writers strip instead), R28 (recall <= 5
files per turn, 4096 bytes / 200 lines each, 61 440-byte session budget, path +
age header; selector = local index search here), R29 (external edits -> stale
reminder, ``MemoryWatcher``); takeaways 02#T8, 02#T9.

Design choices (spec silent / uncertain, 02 §8):

* Index search scoring (02 R28a) = count of distinct query words (>= 3 chars,
  minus stop words) found in a memory's name + description, with a relevance
  floor of 1; ties broken by recency (architect gap G12).
* Remote mode is signalled by ``CLAUDE_CODE_REMOTE`` (truthy).
* The side-model selector (02 R28b) is Deferred; ``MemoryRecall.selector`` is an
  injectable callable for hosts that want one.
* The "canonical git root" for the default location walks up from the original
  cwd to the first ``.git`` (a worktree's ``.git`` file is followed to the main
  repository so all worktrees share one memory).
"""

from __future__ import annotations

import os
import re
import tempfile
import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Literal
from urllib.parse import unquote

from harnesslab.config import SettingsSource, env_falsy, env_truthy
from harnesslab.runtime import SessionContext
from harnesslab.types import Attachment
from harnesslab.util.frontmatter import parse_frontmatter
from harnesslab.util.ids import project_slug
from harnesslab.util.text import head

INDEX_NAME = "MEMORY.md"
INDEX_MAX_LINES = 200
INDEX_MAX_BYTES = 25_000
INDEX_LINE_CHARS = 150
RECALL_PER_TURN = 5
RECALL_FILE_MAX_BYTES = 4096
RECALL_FILE_MAX_LINES = 200
RECALL_SESSION_BUDGET = 61_440

MemoryType = Literal["user", "feedback", "project", "reference"]
MEMORY_TYPES = ("user", "feedback", "project", "reference")


@dataclass
class MemoryFile:
    path: Path
    name: str
    description: str
    type: MemoryType
    body: str


class MemoryLintError(ValueError):
    pass


# ================================================================ enablement & location


def memory_enabled(ctx: SessionContext) -> bool:
    """02 R23."""
    if env_truthy(ctx.env, "CLAUDE_CODE_DISABLE_AUTO_MEMORY"):
        return False
    if env_falsy(ctx.env, "CLAUDE_CODE_DISABLE_AUTO_MEMORY"):
        return True  # explicitly forced on
    if env_truthy(ctx.env, "CLAUDE_CODE_SIMPLE"):
        return False
    if env_truthy(ctx.env, "CLAUDE_CODE_REMOTE") and not ctx.env.get("CLAUDE_CODE_REMOTE_MEMORY_DIR"):
        return False
    return ctx.settings.get("autoMemoryEnabled", True) is not False


def _home(ctx: SessionContext) -> Path:
    h = ctx.extra.get("home_dir")
    return Path(h) if h else Path.home()


def canonical_git_root(start: Path) -> Path | None:
    """Nearest ancestor containing ``.git``; worktrees resolve to the main repo."""
    for d in [start, *start.parents]:
        g = d / ".git"
        if g.is_dir():
            return d
        if g.is_file():
            try:
                text = g.read_text(encoding="utf-8").strip()
            except OSError:
                return d
            if text.startswith("gitdir:"):
                gitdir = Path(text.split(":", 1)[1].strip())
                if not gitdir.is_absolute():
                    gitdir = (d / gitdir).resolve()
                parts = gitdir.parts
                if ".git" in parts:  # <main>/.git/worktrees/<name>
                    return Path(*parts[: parts.index(".git")])
            return d
    return None


def memory_dir(ctx: SessionContext) -> Path:
    """02 R22: ``autoMemoryDirectory`` (policy > flag > local > project > user), the
    remote override, else ``<configDir>/projects/<slug(git root)>/memory``."""
    for src in (SettingsSource.POLICY, SettingsSource.FLAG, SettingsSource.LOCAL, SettingsSource.PROJECT, SettingsSource.USER):
        v = ctx.settings.get_from(src, "autoMemoryDirectory")
        if isinstance(v, str) and v.strip():
            p = Path(v.strip())
            if v.strip().startswith("~"):
                p = _home(ctx) / v.strip()[1:].lstrip("/")
            return p
    remote = ctx.env.get("CLAUDE_CODE_REMOTE_MEMORY_DIR")
    if remote:
        return Path(remote)
    base = Path(ctx.original_cwd or ctx.cwd)
    root = canonical_git_root(base) or base
    return ctx.config_dir / "projects" / project_slug(str(root)) / "memory"


_ENCODED_TRAVERSAL = re.compile(r"%(2e|2f|5c|00)", re.I)


def safe_memory_path(base: Path, relative: str) -> Path:
    """Resolve under ``base`` or raise (02 R26)."""
    if "\x00" in relative:
        raise MemoryLintError("path contains a null byte")
    if _ENCODED_TRAVERSAL.search(relative) or unquote(relative) != relative:
        raise MemoryLintError("path contains percent-encoded characters")
    if unicodedata.normalize("NFKC", relative) != relative:
        raise MemoryLintError("path is not in normalized Unicode form")
    if os.path.isabs(relative) or relative.startswith("~"):
        raise MemoryLintError("path must be relative to the memory directory")
    parts = re.split(r"[\\/]", relative)
    if not relative or any(p in ("..", ".") for p in parts):
        raise MemoryLintError("path must not contain '.' or '..' segments")
    base_real = base.resolve()
    target = (base / relative).resolve()  # follows symlinks: an escaping link is caught here
    if target != base_real and base_real not in target.parents:
        raise MemoryLintError("path escapes the memory directory")
    return target


def team_store_dirs(ctx: SessionContext) -> list[Path]:
    """02 R26: ``CLAUDE_MEMORY_STORES`` = comma-separated mount names."""
    raw = ctx.env.get("CLAUDE_MEMORY_STORES", "")
    out: list[Path] = []
    base = memory_dir(ctx)
    for mount in (m.strip() for m in raw.split(",")):
        if mount:
            out.append(safe_memory_path(base, f"team/{mount}"))
    return out


# ================================================================ index


def load_index(ctx: SessionContext) -> tuple[str, bool]:
    """``(text, truncated)`` with an appended warning when truncated (02 R24)."""
    p = memory_dir(ctx) / INDEX_NAME
    try:
        text = p.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return "", False
    excerpt, truncated = head(text, max_bytes=INDEX_MAX_BYTES, max_lines=INDEX_MAX_LINES)
    if truncated:
        excerpt = excerpt.rstrip("\n") + (
            f"\n\n[Warning: {INDEX_NAME} is longer than {INDEX_MAX_LINES} lines / {INDEX_MAX_BYTES} bytes; "
            "only the beginning was loaded. Keep index entries to one short line and move detail into the "
            "memory files.]\n"
        )
    return excerpt, truncated


# ================================================================ write lint (02 R27, 02#T9)

#: Tag names the harness itself uses to frame trusted content. A memory that
#: contains them could later masquerade as harness output.
HARNESS_TAGS = (
    "system-reminder",
    "system",
    "task-notification",
    "tool_use_error",
    "persisted-output",
    "truncated-output",
    "total_tokens",
    "command-name",
    "command-message",
    "command-args",
    "local-command-stdout",
    "user-prompt-submit-hook",
    "function_calls",
    "function_results",
    "invoke",
    "parameter",
)
_TAG_RE = re.compile(r"</?\s*(?:" + "|".join(re.escape(t) for t in HARNESS_TAGS) + r")\b[^>]*>", re.I)


def _is_invisible(ch: str) -> bool:
    if ch in "\n\t\r":
        return False
    cat = unicodedata.category(ch)
    return cat in ("Cc", "Cf", "Co", "Cs") or ch in "  "


def lint_memory_write(text: str, *, background: bool = False) -> str:
    """Return text to write (stripped if background) or raise ``MemoryLintError``."""
    bad_chars = sorted({f"U+{ord(c):04X}" for c in text if _is_invisible(c)})
    tags = _TAG_RE.findall(text)
    if not background:
        if bad_chars:
            raise MemoryLintError(f"memory text contains invisible or control characters ({', '.join(bad_chars[:5])}); remove them")
        if tags:
            raise MemoryLintError(f"memory text contains harness-style tags ({tags[0]}); rephrase without angle-bracket tags")
        return text
    cleaned = "".join(c for c in text if not _is_invisible(c))
    return _TAG_RE.sub("", cleaned)


def parse_memory_file(path: Path) -> MemoryFile | None:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    fm = parse_frontmatter(text)
    if not fm.present or not fm.data.get("name"):
        return None
    meta = fm.data.get("metadata") if isinstance(fm.data.get("metadata"), dict) else {}
    mtype = meta.get("type", fm.data.get("type", "project"))
    if mtype not in MEMORY_TYPES:
        mtype = "project"
    return MemoryFile(path, str(fm.data["name"]), str(fm.data.get("description", "")), mtype, fm.body)


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-", suffix=".md")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def write_memory(
    ctx: SessionContext,
    relative: str,
    *,
    name: str,
    description: str,
    type: MemoryType,
    body: str,
    background: bool = False,
) -> Path:
    """Write one memory file (one fact per file, 02 §2.6) atomically and add/refresh
    its one-line index entry. Lint runs on every field (02 R27)."""
    if type not in MEMORY_TYPES:
        raise MemoryLintError(f"type must be one of {', '.join(MEMORY_TYPES)}")
    name = lint_memory_write(name, background=background).strip()
    description = lint_memory_write(description, background=background).strip().replace("\n", " ")
    body = lint_memory_write(body, background=background)
    base = memory_dir(ctx)
    path = safe_memory_path(base, relative)
    text = f"---\nname: {name}\ndescription: {description}\nmetadata:\n  type: {type}\n---\n{body.rstrip()}\n"
    _atomic_write(path, text)
    rel = path.relative_to(base.resolve()).as_posix()
    line = f"- [{name}]({rel}) - {description}"
    if len(line) > INDEX_LINE_CHARS:
        line = line[: INDEX_LINE_CHARS - 3].rstrip() + "..."
    idx = base / INDEX_NAME
    lines = idx.read_text(encoding="utf-8").splitlines() if idx.exists() else []
    lines = [ln for ln in lines if f"]({rel})" not in ln] + [line]
    _atomic_write(idx, "\n".join(lines) + "\n")
    return path


# ================================================================ prompt section (02 R25)


def memory_prompt_section(directory: str) -> str:
    """The memory instructions for the system prompt (own wording)."""
    return f"""# Memory

You have a persistent memory directory at `{directory}`. Its index, `{INDEX_NAME}`, is loaded into every session; individual memory files are recalled when relevant.

How to save a memory:
- One fact per file, as markdown with frontmatter: `name`, `description` (one line), and `metadata.type` set to one of user, feedback, project or reference.
- For feedback and project memories add a line starting "Why:" and a line starting "How to apply:".
- Link related memories with [[name]].
- Add a single short line (about 150 characters) to `{INDEX_NAME}` for every file: a link plus a hook saying when it matters.

Do not save, even when asked: code patterns or structure (read the code instead), git history, step-by-step fix recipes, anything already in instruction files, or the state of the current task. When the user asks you to remember one of these, ask yourself what surprising, durable insight is behind the request and save that instead.

Memories age. One that names a file, function or flag was true when written; check that it still exists before relying on it."""


# ================================================================ recall (02 R28)

_STOP = frozenset(
    "the and for with that this from have what when where which will would should could about into your you are was were not but can how why use using".split()
)


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9][a-z0-9_-]{2,}", text.lower()) if w not in _STOP}


def _age(seconds: float) -> str:
    days = int(seconds // 86400)
    if days >= 1:
        return f"{days} day{'s' if days != 1 else ''} ago"
    hours = int(seconds // 3600)
    return f"{hours} hour{'s' if hours != 1 else ''} ago" if hours else "less than an hour ago"


Selector = Callable[[str, list[MemoryFile], set[str]], list[MemoryFile]]


@dataclass
class MemoryRecall:
    bytes_used: int = 0
    already: set[str] = field(default_factory=set)
    selector: Selector | None = None
    now: Callable[[], float] = time.time

    def candidates(self, ctx: SessionContext) -> list[MemoryFile]:
        base = memory_dir(ctx)
        if not base.is_dir():
            return []
        out = []
        for p in sorted(base.rglob("*.md")):
            if p.name == INDEX_NAME:
                continue
            mf = parse_memory_file(p)
            if mf is not None:
                out.append(mf)
        return out

    def index_search(self, query: str, files: list[MemoryFile]) -> list[MemoryFile]:
        """02 R28(a): keyword overlap with a floor of 1; fetch 5 + min(surfaced + 10, 20)."""
        q = _words(query)
        limit = RECALL_PER_TURN + min(len(self.already) + 10, 20)
        scored = []
        for f in files:
            score = len(q & _words(f"{f.name} {f.description}"))
            if score >= 1:
                try:
                    mtime = f.path.stat().st_mtime
                except OSError:
                    mtime = 0.0
                scored.append((score, mtime, f))
        scored.sort(key=lambda t: (t[0], t[1]), reverse=True)
        return [f for _, _, f in scored[:limit]]

    def recall(self, ctx: SessionContext, query: str) -> list[Attachment]:
        """``relevant_memories`` attachment(s) for this user turn (02 R28)."""
        if not memory_enabled(ctx) or self.bytes_used >= RECALL_SESSION_BUDGET or not query.strip():
            return []
        files = self.candidates(ctx)
        picks = self.selector(query, files, set(self.already)) if self.selector else self.index_search(query, files)
        picks = [f for f in picks if str(f.path) not in self.already][:RECALL_PER_TURN]
        out = []
        for f in picks:
            try:
                raw = f.path.read_text(encoding="utf-8")
            except OSError:
                continue
            body, cut = head(raw, max_bytes=RECALL_FILE_MAX_BYTES, max_lines=RECALL_FILE_MAX_LINES)
            if cut:
                body += f"\n[Truncated; read {f.path} for the rest.]"
            size = len(body.encode("utf-8"))
            if self.bytes_used + size > RECALL_SESSION_BUDGET:
                break
            self.bytes_used += size
            self.already.add(str(f.path))
            try:
                age = _age(max(0.0, self.now() - f.path.stat().st_mtime))
            except OSError:
                age = "unknown"
            out.append({"path": str(f.path), "age": age, "content": body})
        return [Attachment("relevant_memories", {"memories": out})] if out else []


# ================================================================ staleness (02 R29)


class MemoryWatcher:
    """Notices external edits to the memory directory after it was loaded."""

    def __init__(self) -> None:
        self.seen: dict[str, float] = {}

    def _scan(self, ctx: SessionContext) -> dict[str, float]:
        base = memory_dir(ctx)
        if not base.is_dir():
            return {}
        return {str(p): p.stat().st_mtime for p in base.rglob("*.md")}

    def prime(self, ctx: SessionContext) -> None:
        self.seen = self._scan(ctx)

    def check(self, ctx: SessionContext) -> list[Attachment]:
        now = self._scan(ctx)
        changed = sorted(p for p, m in now.items() if self.seen.get(p) not in (None, m))
        removed = sorted(p for p in self.seen if p not in now)
        self.seen = now
        if not changed and not removed:
            return []
        return [Attachment("memory_stale", {"changed": changed, "removed": removed})]
