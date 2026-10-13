"""Hazard data: circuit-breaker table and sensitive paths (WP3).

Implements spec 04 §2.4 (circuit-breaker table), 04 R16 (dangerous removal),
04 R18 (sensitive write paths, classifier-approvable even in acceptEdits;
exempt ``.claude/{skills,agents,commands,worktrees}``), 04 R19 (apply breaker
properties), 04 §2.1 (acceptEdits shell commands).
Also the ``claudeSettingsFile`` human-only hazard (04 §2.4) for
``.claude/settings*.json``. Takeaway 04#T3: "keep the
list of dangerous things in a table rather than in code".

The table values below are transcribed from the spec and are the source of
truth; WP3 implements the lookup helpers.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass


@dataclass(frozen=True)
class CircuitBreaker:
    bypass_immune: bool
    classifier_routed: bool
    auto_mode_deny: bool
    host_person_only: bool


CIRCUIT_BREAKERS: dict[str, CircuitBreaker] = {
    "dangerousRemoval": CircuitBreaker(True, True, True, False),
    "backgroundOperator": CircuitBreaker(False, True, False, False),
    "suspiciousWindowsPath": CircuitBreaker(False, True, False, False),
    "restrictedMode": CircuitBreaker(True, False, False, False),
    "outsideReadsBlocked": CircuitBreaker(True, False, False, False),
    "isolatePeerMachines": CircuitBreaker(True, False, False, False),
    "claudeSettingsFile": CircuitBreaker(False, False, False, True),
    "internalHardDeny": CircuitBreaker(False, False, False, False),
}

SENSITIVE_DIRS = (".git", ".vscode", ".idea", ".claude", ".husky", ".cargo", ".devcontainer", ".yarn", ".mvn", ".config/git")
SENSITIVE_FILES = (
    ".gitconfig",
    ".gitmodules",
    ".bashrc",
    ".zshrc",
    ".profile",
    ".envrc",
    ".npmrc",
    ".mcp.json",
    ".claude.json",
    ".pre-commit-config.yaml",
    "lefthook.yml",
    "gradle-wrapper.properties",
)
EXEMPT_DIRS = (".claude/skills", ".claude/agents", ".claude/commands", ".claude/worktrees")
ACCEPT_EDITS_SHELL_COMMANDS = frozenset({"mkdir", "touch", "rm", "rmdir", "mv", "cp", "sed"})


#: Absolute directories whose removal would wreck the machine (04 R16). Design
#: choice: the spec says "system roots"; this is our own conservative list.
SYSTEM_ROOTS = frozenset(
    {
        "/", "/bin", "/sbin", "/usr", "/etc", "/var", "/lib", "/lib64", "/opt", "/boot",
        "/dev", "/proc", "/sys", "/home", "/root", "/srv", "/private", "/System",
        "/Library", "/Applications", "/Users", "/Volumes", "/usr/local", "/usr/bin",
    }
)

#: Paths under ``.claude`` that hold the agent's own instructions and settings.
#: Writing them is a "claudeSettingsFile" hazard that only a human may approve
#: (04 §2.4 ``host_person_only``). Design choice: settings files only.
SETTINGS_FILE_NAMES = frozenset({"settings.json", "settings.local.json"})


def breaker(name: str | None) -> CircuitBreaker | None:
    """Look up a circuit breaker by key (04 §2.4). Unknown or ``None`` -> ``None``.

    Callers ask the *table* how a hazard is treated instead of hard-coding the
    answer in pipeline code (04#T3).
    """
    if not name:
        return None
    return CIRCUIT_BREAKERS.get(name)


def _components(path: str, *, cwd: str) -> list[str]:
    """Absolute, normalised path split into components (no symlink resolution:
    we judge the path the model *named*)."""
    full = os.path.normpath(os.path.join(cwd, os.path.expanduser(path)))
    return [c for c in full.split(os.sep) if c]


def _contains_sequence(parts: list[str], seq: list[str]) -> int:
    """Index where ``seq`` occurs as consecutive components of ``parts``, or -1."""
    n = len(seq)
    for i in range(len(parts) - n + 1):
        if parts[i : i + n] == seq:
            return i
    return -1


def is_sensitive_path(path: str, *, cwd: str) -> bool:
    """True when writing ``path`` could change how tools, shells or the agent
    itself behave (04 R18): anything inside a sensitive directory (``.git``,
    ``.vscode``, ``.claude`` ...) or a sensitive file name (``.bashrc``,
    ``.gitconfig`` ...), anywhere on the path. The ``.claude/{skills,agents,
    commands,worktrees}`` sub-trees are exempt because editing them is normal
    project work.
    """
    parts = _components(path, cwd=cwd)
    if not parts:
        return False
    for exempt in EXEMPT_DIRS:
        if _contains_sequence(parts, exempt.split("/")) >= 0:
            return False
    if parts[-1] in SENSITIVE_FILES:
        return True
    return any(_contains_sequence(parts, d.split("/")) >= 0 for d in SENSITIVE_DIRS)


def is_agent_settings_file(path: str, *, cwd: str) -> bool:
    """``.claude/settings*.json`` - the harness's own permission settings. A write
    here could grant the agent new powers, so it is a human-only hazard
    (``claudeSettingsFile`` breaker, 04 §2.4)."""
    parts = _components(path, cwd=cwd)
    return len(parts) >= 2 and parts[-2] == ".claude" and parts[-1] in SETTINGS_FILE_NAMES


_LEADING_EXPANSION = re.compile(r"^(?:\$\{?[A-Za-z_][A-Za-z0-9_]*|\$\{?[0-9@*#?]|`|\$\()")


def is_dangerous_removal_target(target: str, *, cwd: str, home: str) -> bool:
    """System roots, home, workspace root, or a target that may expand to empty.

    ``target`` is the *unexpanded* word from the command line (04 R16):

    * ``$UNSET/`` or ``${DIR}/x`` - if the variable is empty the path collapses
      to ``/`` or ``/x``, so any target that *starts* with an expansion is
      treated as dangerous;
    * ``/``, ``/usr``, ``~``, ``$HOME``-equivalents, ``.`` (the workspace root)
      and any ancestor of the workspace or home directory;
    * the same paths followed by ``/*`` (removing everything inside them).
    """
    t = target.strip()
    if not t:
        return True
    if _LEADING_EXPANSION.match(t):
        return True
    # strip a trailing "/*" or "/" - "rm -rf /usr/*" is as bad as "rm -rf /usr"
    while len(t) > 1 and (t.endswith("/*") or t.endswith("/")):
        t = t[:-2] if t.endswith("/*") else t[:-1]
    if t in ("*", ".*"):
        t = "."  # every entry of the current directory = the workspace itself
    if t == "~" or t.startswith("~/"):
        expanded = home + t[1:]
    elif t.startswith("~"):
        return True  # ~otheruser: another user's home directory
    else:
        expanded = t
    full = os.path.normpath(os.path.join(cwd, expanded))
    home_n = os.path.normpath(home)
    cwd_n = os.path.normpath(cwd)
    if full in SYSTEM_ROOTS or full == home_n or full == cwd_n:
        return True
    # ancestors of the workspace or home ("rm -rf .." from the project root)
    for protected in (cwd_n, home_n):
        if protected.startswith(full.rstrip(os.sep) + os.sep):
            return True
    return False
