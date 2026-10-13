"""Instruction-file (CLAUDE.md) discovery, imports, conditional rules (WP4).

Implements spec 02 §2.5, R15 (load order Managed -> User -> Project/Local root
to cwd -> --add-dir (opt-in env) -> AutoMem; worktree skip rule), R16
(preamble + per-file provenance labels; delivered as an ``instructions``
attachment; re-reads send only changed / removed files), R17 (``@path`` imports
from GFM text nodes only, never code spans/blocks; HTML comments stripped;
``\\ `` escapes; ``#fragment`` dropped; ``./``, ``~/``, absolute, bare
``name.ext``; depth <= 5; text-extension allowlist; cycle detection by real
path; external includes need approval except from User files), R18 (4 MiB
skip; warn-only size limits), R19 (``paths:`` frontmatter rules deferred until a
tool touches a matching path, gitignore semantics), R20 (nested instruction
files near touched paths -> ``nested_memory``; permission-denied ->
``withheld_memory``), R21 (``claudeMdExcludes``; ``CLAUDE_CODE_DISABLE_CLAUDE_MDS``;
AGENTS.md detected but not loaded); takeaway 02#T6.

Design choices:

* A small line-based markdown scanner (fenced code blocks and inline code spans
  skipped) replaces a full GFM parser.
* Seams for tests and hosts, all in ``ctx.extra``: ``home_dir`` (instead of the
  real home), ``managed_dir``, ``managed_claude_md`` (remote string),
  ``instructions_root`` (where the root->cwd walk starts; default filesystem
  root), ``read_denied`` (callable ``path -> bool`` for R20 withheld files),
  ``approve_external_includes`` (bool, R17 one-time approval).
* Imports from a ``Project`` file are typed ``Project``; ``Local`` stays
  ``Local``; the import inherits its parent's provenance.
* The TEXT_EXTENSIONS allowlist is a representative subset of the ~120 named
  in the spec.
"""

from __future__ import annotations

import fnmatch
import hashlib
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Literal

from harnesslab.config import SettingsSource, env_truthy
from harnesslab.runtime import SessionContext
from harnesslab.types import Attachment
from harnesslab.util.frontmatter import as_list, parse_frontmatter

MAX_IMPORT_DEPTH = 5
HARD_SKIP_BYTES = 4 * 1024 * 1024
PER_FILE_WARN_CHARS = 40_000
TOTAL_WARN_CHARS = 120_000
TEXT_EXTENSIONS = frozenset(
    {
        ".md", ".txt", ".markdown", ".rst", ".adoc", ".org", ".json", ".jsonc", ".yaml", ".yml", ".toml",
        ".ini", ".cfg", ".conf", ".env", ".xml", ".csv", ".tsv", ".py", ".pyi", ".js", ".mjs", ".cjs", ".ts",
        ".tsx", ".jsx", ".sh", ".bash", ".zsh", ".fish", ".ps1", ".rb", ".go", ".rs", ".java", ".kt", ".kts",
        ".swift", ".c", ".h", ".cc", ".cpp", ".hpp", ".cs", ".php", ".pl", ".lua", ".r", ".sql", ".graphql",
        ".proto", ".html", ".htm", ".css", ".scss", ".less", ".vue", ".svelte", ".dart", ".scala", ".clj",
        ".ex", ".exs", ".erl", ".hs", ".ml", ".nix", ".tf", ".hcl", ".gradle", ".mk", ".cmake", ".dockerfile",
    }
)
INSTRUCTION_NAMES = ("CLAUDE.md", ".claude/CLAUDE.md")
LOCAL_NAME = "CLAUDE.local.md"

InstructionType = Literal["Managed", "User", "Project", "Local", "AutoMem"]

LABELS: dict[str, str] = {
    "Project": "project instructions, checked into the codebase",
    "Local": "the user's private project instructions, not checked in",
    "AutoMem": "the user's auto-memory index, persists across conversations",
    "Managed": "organisation-wide policy instructions",
    "User": "the user's private global instructions for all projects",
}

PREAMBLE = (
    "Instructions from the files below take precedence over default behaviour. "
    "Follow them exactly as written. Later files are more specific than earlier ones."
)


@dataclass
class InstructionFile:
    path: Path
    type: InstructionType
    content: str
    conditional_globs: list[str] = field(default_factory=list)
    children: list["InstructionFile"] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def flatten(self) -> list["InstructionFile"]:
        """Parent first, then imports depth-first (an import appears right after the
        file that included it)."""
        out = [self]
        for c in self.children:
            out += c.flatten()
        return out


# ================================================================ @imports (02 R17)

_HTML_COMMENT = re.compile(r"<!--.*?-->", re.S)
_FENCE = re.compile(r"^\s{0,3}(`{3,}|~{3,})")
_CODE_SPAN = re.compile(r"(`+)(?:.+?)\1", re.S)
_AT_TOKEN = re.compile(r"(?:(?<=\s)|^)@((?:\\ |[^\s])+)")


def at_tokens(markdown: str) -> list[str]:
    """``@path`` tokens from text nodes only (02 R17)."""
    text = _HTML_COMMENT.sub("", markdown)
    tokens: list[str] = []
    fence: str | None = None
    for line in text.splitlines():
        m = _FENCE.match(line)
        if fence is not None:
            if m and m.group(1)[0] == fence[0] and len(m.group(1)) >= len(fence):
                fence = None
            continue
        if m:
            fence = m.group(1)
            continue
        if line.startswith("    ") or line.startswith("\t"):  # indented code block
            continue
        line = _CODE_SPAN.sub(" ", line)
        for raw in _AT_TOKEN.findall(line):
            tok = raw.replace("\\ ", " ").split("#", 1)[0]
            tok = tok.rstrip(".,;:!?)")
            if tok:
                tokens.append(tok)
    return tokens


def _resolve_token(tok: str, base: Path, home: Path) -> Path | None:
    if tok.startswith("~/"):
        return home / tok[2:]
    if tok.startswith("/"):
        return Path(tok)
    if tok.startswith("./") or tok.startswith("../"):
        return base / tok
    # bare ``name.ext`` (possibly with sub-directories)
    if "." in Path(tok).name and not tok.startswith("@"):
        return base / tok
    return None


def _inside(p: Path, root: Path) -> bool:
    try:
        rp, rr = p.resolve(), root.resolve()
    except OSError:
        return False
    return rp == rr or rr in rp.parents


def _read_text(p: Path, warnings: list[str]) -> str | None:
    try:
        if p.stat().st_size > HARD_SKIP_BYTES:
            warnings.append(f"skipped {p}: larger than 4 MiB")
            return None
        return p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def expand_imports(
    f: InstructionFile,
    *,
    cwd: Path,
    depth: int = 0,
    seen: set[str] | None = None,
    approve_external: bool = False,
    home: Path | None = None,
) -> InstructionFile:
    """Recursively attach ``@imports`` as children (depth <= 5, cycle-safe by real path)."""
    seen = set() if seen is None else seen
    real = os.path.realpath(f.path)
    if depth > MAX_IMPORT_DEPTH or real in seen:
        return f
    seen.add(real)
    home = home or Path.home()
    for tok in at_tokens(f.content):
        p = _resolve_token(tok, f.path.parent, home)
        if p is None:
            continue
        if p.suffix.lower() not in TEXT_EXTENSIONS:
            continue
        if not p.is_file():
            continue
        if os.path.realpath(p) in seen:
            continue  # cycle
        if depth + 1 > MAX_IMPORT_DEPTH:
            f.warnings.append(f"import {tok} not loaded: more than {MAX_IMPORT_DEPTH} nested imports")
            continue
        if not _inside(p, cwd) and f.type != "User" and not approve_external:
            f.warnings.append(f"import {tok} is outside the project and needs approval")
            continue
        text = _read_text(p, f.warnings)
        if text is None:
            continue
        child = InstructionFile(p, f.type, text)
        f.children.append(expand_imports(child, cwd=cwd, depth=depth + 1, seen=seen, approve_external=approve_external, home=home))
    return f


# ================================================================ discovery (02 R15)


def _normalize_globs(globs: list[str]) -> list[str]:
    out = []
    for g in globs:
        g = g.strip()
        while g.endswith("/**"):
            g = g[:-3]
        if g:
            out.append(g)
    return out


def _load_file(p: Path, t: InstructionType, warnings: list[str]) -> InstructionFile | None:
    if not p.is_file():
        return None
    text = _read_text(p, warnings)
    if text is None:
        return None
    fm = parse_frontmatter(text)
    globs = _normalize_globs(as_list(fm.data.get("paths"))) if fm.present else []
    return InstructionFile(p, t, fm.body if fm.present else text, globs)


def _load_rules(rules_dir: Path, t: InstructionType, warnings: list[str]) -> list[InstructionFile]:
    if not rules_dir.is_dir():
        return []
    out = []
    for p in sorted(rules_dir.rglob("*.md")):
        f = _load_file(p, t, warnings)
        if f is not None:
            out.append(f)
    return out


def _worktree_roots(cwd: Path) -> tuple[Path, Path] | None:
    """``(worktree_root, main_repo_root)`` when cwd is inside a linked git worktree."""
    for d in [cwd, *cwd.parents]:
        g = d / ".git"
        if g.is_dir():
            return None
        if g.is_file():
            try:
                text = g.read_text(encoding="utf-8").strip()
            except OSError:
                return None
            if not text.startswith("gitdir:"):
                return None
            gitdir = Path(text.split(":", 1)[1].strip())
            if not gitdir.is_absolute():
                gitdir = (d / gitdir).resolve()
            parts = gitdir.parts
            if ".git" not in parts:
                return None
            return d, Path(*parts[: parts.index(".git")])
    return None


def _path_chain(root: Path, cwd: Path) -> list[Path]:
    """Directories from ``root`` down to ``cwd`` (root first)."""
    chain = [cwd, *cwd.parents]
    chain.reverse()
    try:
        idx = chain.index(root)
    except ValueError:
        idx = 0
    return chain[idx:]


def _project_files(d: Path, warnings: list[str], *, include_conditional: bool = False) -> list[InstructionFile]:
    out: list[InstructionFile] = []
    for name in INSTRUCTION_NAMES:
        f = _load_file(d / name, "Project", warnings)
        if f is not None:
            out.append(f)
    out += [r for r in _load_rules(d / ".claude" / "rules", "Project", warnings) if include_conditional or not r.conditional_globs]
    f = _load_file(d / LOCAL_NAME, "Local", warnings)
    if f is not None:
        out.append(f)
    return out


def _excluded(f: InstructionFile, patterns: list[str]) -> bool:
    if f.type not in ("User", "Project", "Local"):
        return False
    s = str(f.path)
    return any(fnmatch.fnmatch(s, pat) or fnmatch.fnmatch(os.path.realpath(s), pat) for pat in patterns)


def _home(ctx: SessionContext, home: Path | None) -> Path:
    if home is not None:
        return home
    h = ctx.extra.get("home_dir")
    return Path(h) if h else Path.home()


def warn_limits(context_window: int = 200_000) -> tuple[int, int]:
    """02 R18: ``max(40 000, 5 % of window x 4 bytes/token)`` and ``max(120 000, ...)``."""
    five_pct = int(context_window * 0.05 * 4)
    return max(PER_FILE_WARN_CHARS, five_pct), max(TOTAL_WARN_CHARS, five_pct * 3)


def discover_instruction_files(ctx: SessionContext, *, home: Path | None = None, managed_dir: Path | None = None) -> list[InstructionFile]:
    """02 §4 ``discover_instruction_files``; size warnings land in ``f.warnings``
    (never truncation, R18) and AGENTS.md sightings in ``ctx.extra["agents_md_detected"]``."""
    if env_truthy(ctx.env, "CLAUDE_CODE_DISABLE_CLAUDE_MDS"):
        return []
    from harnesslab.context.memory import INDEX_NAME, load_index, memory_dir, memory_enabled

    home = _home(ctx, home)
    managed = managed_dir or (Path(ctx.extra["managed_dir"]) if ctx.extra.get("managed_dir") else None)
    warnings: list[str] = []
    files: list[InstructionFile] = []

    # 1. Managed
    if managed is not None:
        f = _load_file(managed / "CLAUDE.md", "Managed", warnings)
        if f:
            files.append(f)
    remote = ctx.extra.get("managed_claude_md")
    if isinstance(remote, str) and remote.strip():
        files.append(InstructionFile(Path("<remote-managed>"), "Managed", remote))
    policy_md = ctx.settings.get_from(SettingsSource.POLICY, "claudeMd")
    if isinstance(policy_md, str) and policy_md.strip():
        files.append(InstructionFile(Path("<policy-settings:claudeMd>"), "Managed", policy_md))
    if managed is not None:
        files += [r for r in _load_rules(managed / ".claude" / "rules", "Managed", warnings) if not r.conditional_globs]

    # 2. User
    f = _load_file(home / ".claude" / "CLAUDE.md", "User", warnings)
    if f:
        files.append(f)
    files += [r for r in _load_rules(home / ".claude" / "rules", "User", warnings) if not r.conditional_globs]

    # 3. Project + Local, root -> cwd
    cwd = Path(ctx.cwd)
    root = Path(ctx.extra.get("instructions_root") or cwd.anchor)
    wt = _worktree_roots(cwd)
    agents_md: list[str] = []
    for d in _path_chain(root, cwd):
        if wt is not None:
            wt_root, main_root = wt
            if _inside(d, main_root) and not _inside(d, wt_root):
                continue  # inside the main checkout but outside this worktree
        files += _project_files(d, warnings)
        if (d / "AGENTS.md").is_file():
            agents_md.append(str(d / "AGENTS.md"))  # detected, deliberately not loaded (R21)

    # 4. --add-dir (opt-in)
    if env_truthy(ctx.env, "CLAUDE_CODE_ADDITIONAL_DIRECTORIES_CLAUDE_MD"):
        for d in ctx.additional_dirs:
            files += _project_files(Path(d), warnings)

    # 5. AutoMem
    if memory_enabled(ctx):
        text, _ = load_index(ctx)
        if text.strip():
            files.append(InstructionFile(memory_dir(ctx) / INDEX_NAME, "AutoMem", text))

    excludes = [str(x) for x in as_list(ctx.settings.get("claudeMdExcludes"))]
    files = [f for f in files if not _excluded(f, excludes)]  # an excluded parent drops its imports too
    approve = bool(ctx.extra.get("approve_external_includes"))
    out = [expand_imports(f, cwd=cwd, approve_external=approve, home=home) for f in files]

    per_file, total_limit = warn_limits(int(ctx.extra.get("context_window", 200_000)))
    total = 0
    for f in out:
        for x in f.flatten():
            total += len(x.content)
            if len(x.content) > per_file:
                x.warnings.append(f"{x.path} is {len(x.content)} characters (over {per_file}); it may crowd the context")
    if total > total_limit and out:
        out[-1].warnings.append(f"instruction files total {total} characters (over {total_limit})")
    if warnings and out:
        out[0].warnings.extend(warnings)
    ctx.extra["agents_md_detected"] = agents_md
    return out


# ================================================================ conditional rules (02 R19)


def conditional_rules(ctx: SessionContext) -> list[InstructionFile]:
    """Every rule file with ``paths:`` frontmatter (managed, user, project chain)."""
    if env_truthy(ctx.env, "CLAUDE_CODE_DISABLE_CLAUDE_MDS"):
        return []
    home = _home(ctx, None)
    warnings: list[str] = []
    out: list[InstructionFile] = []
    managed = ctx.extra.get("managed_dir")
    if managed:
        out += _load_rules(Path(managed) / ".claude" / "rules", "Managed", warnings)
    out += _load_rules(home / ".claude" / "rules", "User", warnings)
    cwd = Path(ctx.cwd)
    root = Path(ctx.extra.get("instructions_root") or cwd.anchor)
    for d in _path_chain(root, cwd):
        out += _load_rules(d / ".claude" / "rules", "Project", warnings)
    return [r for r in out if r.conditional_globs]


def gitignore_match(pattern: str, rel_path: str) -> bool:
    """Gitignore-style matching of ``pattern`` against a ``/``-separated relative path.

    * a pattern without ``/`` (other than a trailing one) matches a name at any depth;
    * a leading ``/`` or an inner ``/`` anchors it to the base directory;
    * ``**`` spans directories, ``*`` / ``?`` stay within one segment;
    * matching a directory matches everything below it.
    """
    pat = pattern.strip()
    if not pat:
        return False
    anchored = pat.startswith("/") or "/" in pat.rstrip("/")
    pat = pat.strip("/")
    rx = ""
    i = 0
    while i < len(pat):
        c = pat[i]
        if pat.startswith("**/", i):
            rx += "(?:.*/)?"
            i += 3
        elif pat.startswith("**", i):
            rx += ".*"
            i += 2
        elif c == "*":
            rx += "[^/]*"
            i += 1
        elif c == "?":
            rx += "[^/]"
            i += 1
        else:
            rx += re.escape(c)
            i += 1
    prefix = "" if anchored else "(?:.*/)?"
    return re.fullmatch(f"{prefix}{rx}(?:/.*)?", rel_path) is not None


def _rule_base(rule: InstructionFile) -> Path:
    """Directory a rule's globs are relative to: the one holding ``.claude/rules``."""
    parts = rule.path.parts
    for i in range(len(parts) - 2, -1, -1):
        if parts[i] == ".claude" and i + 1 < len(parts) and parts[i + 1] == "rules":
            return Path(*parts[:i]) if i else Path(rule.path.anchor)
    return rule.path.parent


def rules_for_touched_path(rules: list[InstructionFile], path: str, *, cwd: Path) -> list[InstructionFile]:
    """Rules whose ``paths:`` globs match ``path`` (02 R19). Project rules match
    relative to the directory that owns them; user/managed rules relative to ``cwd``."""
    out = []
    ap = Path(path) if os.path.isabs(path) else cwd / path
    for r in rules:
        base = _rule_base(r) if r.type == "Project" else cwd
        try:
            rel = ap.resolve().relative_to(base.resolve()).as_posix()
        except ValueError:
            continue
        if any(gitignore_match(g, rel) for g in r.conditional_globs):
            out.append(r)
    return out


# ================================================================ nested files (02 R20)


def nested_memory_attachments(ctx: SessionContext, touched: list[str]) -> list[Attachment]:
    """Instruction files between a touched file and the cwd, plus matching
    conditional rules. Each file is sent once per session (``ctx.extra["nested_sent"]``)."""
    if env_truthy(ctx.env, "CLAUDE_CODE_DISABLE_CLAUDE_MDS") or not touched:
        return []
    sent: set[str] = ctx.extra.setdefault("nested_sent", set())
    denied: Callable[[str], bool] = ctx.extra.get("read_denied") or (lambda _p: False)
    cwd = Path(ctx.cwd).resolve()
    rules = conditional_rules(ctx)
    found: list[Path] = []
    for t in touched:
        tp = Path(t) if os.path.isabs(t) else cwd / t
        tp = tp.resolve()
        if cwd not in tp.parents:
            continue
        # walk from the file's directory up to (not including) cwd; root-most first
        dirs = []
        d = tp.parent
        while d != cwd and cwd in d.parents:
            dirs.append(d)
            d = d.parent
        for d in reversed(dirs):
            for name in (*INSTRUCTION_NAMES, LOCAL_NAME):
                if (d / name).is_file():
                    found.append(d / name)
            rules_dir = d / ".claude" / "rules"
            if rules_dir.is_dir():
                for p in sorted(rules_dir.rglob("*.md")):
                    fm = parse_frontmatter(p.read_text(encoding="utf-8", errors="replace"))
                    if not (fm.present and fm.data.get("paths")):
                        found.append(p)
        found += [r.path for r in rules_for_touched_path(rules, str(tp), cwd=cwd)]
    out: list[Attachment] = []
    for p in dict.fromkeys(found):
        key = str(p)
        if key in sent:
            continue
        sent.add(key)
        if denied(key):
            out.append(Attachment("withheld_memory", {"path": key}))
            continue
        try:
            text = p.read_text(encoding="utf-8")
        except PermissionError:
            out.append(Attachment("withheld_memory", {"path": key}))
            continue
        except OSError:
            continue
        fm = parse_frontmatter(text)
        out.append(Attachment("nested_memory", {"path": key, "content": fm.body if fm.present else text}))
    return out


# ================================================================ rendering (02 R16)


def _file_dicts(files: list[InstructionFile]) -> list[dict[str, str]]:
    return [{"path": str(x.path), "type": x.type, "content": x.content} for f in files for x in f.flatten()]


def render_instruction_payload(payload: dict) -> str:
    """Render an ``instructions`` attachment payload (also used by the renderer)."""
    parts = [PREAMBLE]
    if payload.get("changed"):
        parts.append("The files below changed since they were last shown; these versions replace the earlier copies.")
    for f in payload.get("files", []):
        label = LABELS.get(f.get("type", ""), "instructions")
        parts.append(f"Contents of {f['path']} ({label}):\n\n{f['content'].rstrip()}")
    removed = payload.get("removed") or []
    if removed:
        parts.append("These instruction files no longer exist; disregard what they said:\n" + "\n".join(f"- {p}" for p in removed))
    return "\n\n".join(parts)


def render_instructions(files: list[InstructionFile], *, removed: list[str] | None = None, changed: bool = False) -> str:
    return render_instruction_payload({"files": _file_dicts(files), "removed": removed or [], "changed": changed})


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


class InstructionTracker:
    """Remembers what was sent so re-reads produce deltas (02 R16, 02#T2)."""

    def __init__(self) -> None:
        self.sent: dict[str, str] = {}  # path -> content hash

    def delta_attachment(self, files: list[InstructionFile], *, reason: str) -> Attachment | None:
        flat = _file_dicts(files)
        current = {f["path"]: _hash(f["content"]) for f in flat}
        if not self.sent:
            self.sent = current
            if not flat:
                return None
            return Attachment("instructions", {"files": flat, "reason": reason})
        changed = [f for f in flat if self.sent.get(f["path"]) != current[f["path"]]]
        removed = [p for p in self.sent if p not in current]
        self.sent = current
        if not changed and not removed:
            return None
        return Attachment("instructions", {"files": changed, "removed": removed, "changed": True, "reason": reason})
