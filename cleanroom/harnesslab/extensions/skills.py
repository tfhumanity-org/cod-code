"""Skills and commands: one record type, discovery, budgeted listing, resolution (WP5).

Implements spec 07 §2.1 (``Command`` record; model channel = budgeted listing +
Skill tool; user channel = slash commands), §2.2 (SKILL.md frontmatter), R1
(discovery sources; dedupe by real path), R2 (names from directories,
``:``-joined namespaces, ``plugin:`` prefixes; SKILL.md wins in legacy dirs), R3
(>1 MB skipped), R4/R5 (dynamic discovery near touched files; ``paths``-scoped
skills), R6 + §4.1 (listing budget = context_window x 4 x 1 % chars ~ 8000,
``SLASH_COMMAND_TOOL_CHAR_BUDGET``; description = ``description - when_to_use``
capped at 1536; over budget lowest-priority entries degrade to name-only, bundled
keep descriptions), R7 + §4.2 (resolution: strip ``/``; exact, alias,
``:suffix``; ambiguity lists candidates; edit distance <= 2 suggestions;
failed plugin named; fork recursion refused; ``disable-model-invocation``),
R11 (``/X`` mention vs invocation attachment); also 02 R37; takeaways 07#T1,
07#T2, 07#T4; P25.

Design choices (spec silent)
----------------------------
* Priority under budget pressure (07 §8 / WORKPLAN G12): most recently used first
  (``Command.last_used``), then name. Bundled skills are pinned with descriptions.
* When two roots produce the *same name*, the first root in the ``roots`` list
  wins (callers pass roots in priority order: policy, user, project, ...).
* A skill body without ``$ARGUMENTS`` gets the arguments appended on their own
  line so they are never silently dropped.
* R15: a plugin skill with ``PreToolUse`` / ``PermissionRequest`` keys at the top
  level of its frontmatter is loaded stripped of every hook and every tool grant.
* ``description`` falls back to the first non-empty body line.

Deferred (documented gaps)
--------------------------
* Embedded ``!`` shell blocks and the ``shell`` key, typed ``arguments``, skill
  ``hooks`` activation, the ``commands_changed`` host push of R4 (``discover_near``
  returns the new skills; announcing them is the caller's job), synced/host-pushed
  catalogs and the policy-skills directory switch of R1 (callers pass roots).
"""

from __future__ import annotations

import fnmatch
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from harnesslab.types import ATTACHMENT_RENDERERS, Attachment
from harnesslab.util.frontmatter import as_list, parse_frontmatter

MAX_SKILL_FILE_BYTES = 1_000_000
DESCRIPTION_CAP = 1536
LISTING_FRACTION = 0.01
SUGGEST_MAX_DISTANCE = 2
SECURITY_SENSITIVE_TOP_LEVEL = ("PreToolUse", "PermissionRequest")


@dataclass
class Command:
    name: str
    description: str
    kind: Literal["prompt", "local", "local-ui"] = "prompt"
    when_to_use: str | None = None
    body: str = ""
    allowed_tools: list[str] = field(default_factory=list)
    disallowed_tools: list[str] = field(default_factory=list)
    context: Literal["inline", "fork"] = "inline"
    agent: str | None = None
    model: str | None = None
    effort: str | int | None = None
    paths: list[str] = field(default_factory=list)
    disable_model_invocation: bool = False
    user_invocable: bool = True
    background: bool = False
    source: str = "project"  # policy | user | project | plugin | bundled | mcp
    loaded_from: str = "skills"
    path: Path | None = None
    base_dir: Path | None = None
    bundled: bool = False
    aliases: list[str] = field(default_factory=list)
    # ---- WP5 additions
    plugin: str | None = None
    hooks: dict[str, Any] = field(default_factory=dict)
    last_used: float = 0.0
    has_files: bool = False


class SkillResolutionError(LookupError):
    def __init__(self, kind: str, message: str, candidates: list[str] | None = None) -> None:
        super().__init__(message)
        self.kind = kind  # ambiguous | unknown | plugin_failed | already_running | user_only
        self.candidates = candidates or []


# ------------------------------------------------------------------ parsing & discovery


def _truthy(v: Any) -> bool:
    return v is True or (isinstance(v, str) and v.strip().lower() in ("true", "yes", "1"))


def parse_skill_file(path: Path, *, base: Path, source: str, plugin: str | None = None) -> Command | None:
    """Parse ``SKILL.md`` (or a legacy command ``*.md``). ``None`` = skipped (R3, unreadable)."""
    path = Path(path)
    try:
        if path.stat().st_size > MAX_SKILL_FILE_BYTES:
            return None
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    fm = parse_frontmatter(text)
    d = fm.data
    is_skill_md = path.name == "SKILL.md"
    rel = (path.parent if is_skill_md else path.with_suffix("")).relative_to(base)
    parts = [p for p in rel.parts if p not in ("", ".")]
    if not parts:  # a SKILL.md directly in the base directory: use the directory name
        parts = [base.name]
    name = ":".join(parts)
    if plugin:
        name = f"{plugin}:{name}"

    description = d.get("description")
    if not isinstance(description, str) or not description.strip():
        description = next((ln.strip().lstrip("# ") for ln in fm.body.splitlines() if ln.strip()), "")
    aliases = as_list(d.get("aliases"))
    display = d.get("name")
    if isinstance(display, str) and display.strip() and display.strip() != name and display.strip() not in aliases:
        aliases.append(display.strip())

    allowed = as_list(d.get("allowed-tools"))
    hooks = d.get("hooks") if isinstance(d.get("hooks"), dict) else {}
    if plugin and any(k in d for k in SECURITY_SENSITIVE_TOP_LEVEL):
        allowed, hooks = [], {}  # misplaced security hooks: load with no grants at all (R15)
    context = d.get("context") if d.get("context") in ("inline", "fork") else "inline"
    base_dir = path.parent
    has_files = is_skill_md and any(p.name != "SKILL.md" for p in base_dir.iterdir())
    return Command(
        name=name,
        description=description.strip(),
        when_to_use=(d.get("when_to_use") or None),
        body=fm.body,
        allowed_tools=allowed,
        disallowed_tools=as_list(d.get("disallowed-tools") or d.get("disallowedTools")),
        context=context,
        agent=d.get("agent") or None,
        model=d.get("model") or None,
        effort=d.get("effort") if d.get("effort") not in ("", None) else None,
        paths=as_list(d.get("paths")),
        disable_model_invocation=_truthy(d.get("disable-model-invocation")),
        user_invocable=not (d.get("user-invocable") is False or str(d.get("user-invocable")).lower() == "false"),
        background=_truthy(d.get("background")),
        source="plugin" if plugin else source,
        loaded_from="skills" if is_skill_md else "commands_DEPRECATED",
        path=path,
        base_dir=base_dir,
        bundled=source == "bundled",
        aliases=aliases,
        plugin=plugin,
        hooks=hooks,
        has_files=has_files,
    )


def _candidate_files(root: Path) -> list[Path]:
    """SKILL.md files anywhere under ``root``; in a legacy ``commands`` root also
    ``*.md`` files, except in directories that contain a ``SKILL.md`` (R2)."""
    if not root.is_dir():
        return []
    files = sorted(root.rglob("SKILL.md"))
    if root.name == "commands":
        skill_dirs = {f.parent for f in files}
        files += sorted(p for p in root.rglob("*.md") if p.name != "SKILL.md" and p.parent not in skill_dirs)
    return files


def discover_skills(roots: list[tuple[Path, str]], *, plugin_roots: dict[str, Path] | None = None) -> list[Command]:
    """Load every root in order; dedupe by real path, then by name (first wins) (R1-R3)."""
    seen_paths: set[str] = set()
    seen_names: set[str] = set()
    out: list[Command] = []
    jobs: list[tuple[Path, str, str | None]] = [(Path(r), src, None) for r, src in roots]
    for plugin, proot in (plugin_roots or {}).items():
        jobs += [(Path(proot) / "skills", "plugin", plugin), (Path(proot) / "commands", "plugin", plugin)]
    for root, source, plugin in jobs:
        for f in _candidate_files(root):
            real = os.path.realpath(f)
            if real in seen_paths:
                continue
            seen_paths.add(real)
            cmd = parse_skill_file(f, base=root, source=source, plugin=plugin)
            if cmd is None or cmd.name in seen_names:
                continue
            seen_names.add(cmd.name)
            out.append(cmd)
    return out


def project_skill_roots(cwd: Path, stop_at: Path | None = None) -> list[Path]:
    """``.claude/skills`` in every directory from ``cwd`` up to ``stop_at`` (or the
    first directory containing ``.git``, or the filesystem root) (R1)."""
    roots: list[Path] = []
    cur = Path(cwd).resolve()
    stop = Path(stop_at).resolve() if stop_at else None
    while True:
        roots.append(cur / ".claude" / "skills")
        if (stop is not None and cur == stop) or (stop is None and (cur / ".git").exists()) or cur.parent == cur:
            return roots
        cur = cur.parent


def session_skill_roots(cwd: Path, config_dir: Path, additional_dirs: list[Path] | None = None) -> list[tuple[Path, str]]:
    """R1 subset used by ``session.create_session_context``: user skills
    (``<config>/skills``), ``--add-dir`` directories, project ``.claude/skills``
    from cwd upwards, and the legacy ``.claude/commands`` directory. Managed,
    synced and host-pushed catalogs and bundled skills are Deferred."""
    roots: list[tuple[Path, str]] = [(Path(config_dir) / "skills", "user")]
    roots += [(Path(d) / ".claude" / "skills", "project") for d in additional_dirs or []]
    roots += [(r, "project") for r in project_skill_roots(cwd)]
    roots.append((Path(cwd) / ".claude" / "commands", "project"))
    return roots


def discover_near(touched: list[str], known: list[Command], cwd: Path) -> list[Command]:
    """Dynamic discovery (R4): walk up from each touched file (inside ``cwd``) looking
    for ``.claude/skills`` and return skills not already ``known``."""
    cwd = Path(cwd).resolve()
    known_paths = {os.path.realpath(c.path) for c in known if c.path}
    known_names = {c.name for c in known}
    roots: list[Path] = []
    for t in touched:
        cur = Path(t).resolve().parent
        while cur != cwd and cwd in cur.parents:
            cand = cur / ".claude" / "skills"
            if cand.is_dir() and cand not in roots:
                roots.append(cand)
            cur = cur.parent
    found = []
    for c in discover_skills([(r, "project") for r in roots]):
        if c.path and os.path.realpath(c.path) in known_paths or c.name in known_names:
            continue
        found.append(c)
    return found


def path_scoped_active(cmd: Command, touched: list[str], cwd: Path) -> bool:
    """R5: a skill with ``paths`` is only listed once a matching file was touched."""
    if not cmd.paths:
        return True
    for t in touched:
        try:
            rel = str(Path(t).resolve().relative_to(Path(cwd).resolve()))
        except ValueError:
            rel = t
        if any(fnmatch.fnmatch(rel, pat) or fnmatch.fnmatch(Path(rel).name, pat) for pat in cmd.paths):
            return True
    return False


# ------------------------------------------------------------------ budgeted listing (§4.1)


def listing_budget(context_window: int, env_budget: int | None = None, fraction: float = LISTING_FRACTION) -> int:
    """Characters available for the listing: ``env_budget`` or 1 % of the window x 4 chars/token."""
    if env_budget:
        return int(env_budget)
    return int(context_window * 4 * fraction)


def _ellipsize(text: str, cap: int) -> str:
    return text if len(text) <= cap else text[: cap - 1].rstrip() + "…"


def listing_line(cmd: Command, *, with_desc: bool = True) -> str:
    if not with_desc:
        return f"- {cmd.name}"
    desc = cmd.description + (f" - {cmd.when_to_use}" if cmd.when_to_use else "")
    return f"- {cmd.name}: {_ellipsize(desc, DESCRIPTION_CAP)}" if desc else f"- {cmd.name}"


LISTING_HEADER = (
    "Skills you can load with the Skill tool (name: what it is for). "
    "Call the Skill tool with the exact name when one of them fits the task:\n"
)


def skill_listing(skills: list[Command], *, budget: int) -> str:
    """Render the listing within ``budget`` characters, degrading - never dropping -
    lower-priority entries to name-only (07 §4.1, 07#T2)."""
    visible = [s for s in skills if not s.disable_model_invocation]
    full = [listing_line(s) for s in visible]
    if len("\n".join(full)) <= budget:
        return LISTING_HEADER + "\n".join(full)
    pinned = [s for s in visible if s.bundled]
    rest = sorted((s for s in visible if not s.bundled), key=lambda s: (-s.last_used, s.name))
    out = [listing_line(s) for s in pinned]
    used = sum(len(line) + 1 for line in out)
    for s in rest:
        line = listing_line(s)
        if used + len(line) + 1 <= budget:
            out.append(line)
            used += len(line) + 1
        else:
            out.append(listing_line(s, with_desc=False))
            used += len(s.name) + 3
    return LISTING_HEADER + "\n".join(out)


# ------------------------------------------------------------------ resolution (§4.2)


def edit_distance(a: str, b: str) -> int:
    """Classic Levenshtein distance (insert / delete / substitute = 1)."""
    if a == b:
        return 0
    if not a or not b:
        return len(a) or len(b)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def resolve_skill(name: str, skills: list[Command], *, running_skill: str | None = None, failed_plugins: set[str] | None = None) -> Command:
    """07 R7 / §4.2. Every failure is a ``SkillResolutionError`` with a model-readable message."""
    name = name.strip().lstrip("/")
    cands = [s for s in skills if s.name == name]
    if not cands:
        cands = [s for s in skills if name in s.aliases]
    if not cands:
        cands = [s for s in skills if s.name.endswith(":" + name)]
    if len(cands) > 1:
        names = sorted(c.name for c in cands)
        raise SkillResolutionError("ambiguous", f"Skill {name!r} is ambiguous. Use one of the full names: {', '.join(names)}.", names)
    if not cands:
        prefix = name.split(":", 1)[0]
        if failed_plugins and (prefix in failed_plugins or name in failed_plugins):
            raise SkillResolutionError(
                "plugin_failed",
                f"Skill {name!r} belongs to plugin {prefix!r}, which failed to load. Tell the user the plugin needs fixing.",
            )
        sugg = sorted(
            {s.name for s in skills if min(edit_distance(name, s.name), edit_distance(name, s.name.rsplit(":", 1)[-1])) <= SUGGEST_MAX_DISTANCE}
        )
        hint = f" Did you mean: {', '.join(sugg)}?" if sugg else " Only skills from the listing (or typed by the user) can be invoked."
        raise SkillResolutionError("unknown", f"Unknown skill {name!r}.{hint}", sugg)
    s = cands[0]
    if running_skill is not None and running_skill == s.name:
        raise SkillResolutionError(
            "already_running",
            f"You are already running inside skill {s.name!r}. Do not invoke it again; follow its instructions that are already in your context.",
        )
    if s.disable_model_invocation:
        raise SkillResolutionError("user_only", f"Skill {s.name!r} can only be run by the user (as /{s.name}), not by the model.")
    return s


def render_body(cmd: Command, args: str = "", *, plugin_root: Path | None = None, plugin_data: Path | None = None) -> str:
    """``$ARGUMENTS``, ``${CLAUDE_SKILL_DIR}``, ``${CLAUDE_PLUGIN_ROOT}``, ``${CLAUDE_PLUGIN_DATA}``;
    bundled skills with files get a base-directory line."""
    body = cmd.body
    if "$ARGUMENTS" in body:
        body = body.replace("$ARGUMENTS", args)
    elif args.strip():
        body = body.rstrip() + f"\n\nARGUMENTS: {args}"
    if cmd.base_dir is not None:
        body = body.replace("${CLAUDE_SKILL_DIR}", str(cmd.base_dir))
    if plugin_root is not None:
        body = body.replace("${CLAUDE_PLUGIN_ROOT}", str(plugin_root))
    if plugin_data is not None:
        body = body.replace("${CLAUDE_PLUGIN_DATA}", str(plugin_data))
    if cmd.bundled and cmd.has_files and cmd.base_dir is not None:
        body = f"Base directory for this skill: {cmd.base_dir}\n\n{body}"
    return body


# ------------------------------------------------------------------ /X mentions (R11)

_SLASH = re.compile(r"(?:^|\s)/([A-Za-z0-9][A-Za-z0-9:_-]*)")


def skill_mentions(text: str, skills: list[Command]) -> list[str]:
    names = {s.name for s in skills} | {a for s in skills for a in s.aliases}
    return [m for m in dict.fromkeys(_SLASH.findall(text)) if m in names]


def mention_attachment(text: str, skills: list[Command]) -> Attachment | None:
    found = skill_mentions(text, skills)
    return Attachment("skill_mention", {"names": found}) if found else None


def _render_mention(payload: dict[str, Any]) -> str:
    names = ", ".join(f"/{n}" for n in payload.get("names", []))
    return (
        f"The user's message refers to {names}. Invoke the matching skill with the Skill tool only if the "
        "user is asking you to run it; if it is merely mentioned, do not invoke it."
    )


ATTACHMENT_RENDERERS.setdefault("skill_mention", _render_mention)
