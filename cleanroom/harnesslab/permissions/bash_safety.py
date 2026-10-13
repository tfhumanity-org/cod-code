"""Shell command analysis: parser, read-only allowlists, compound checks (WP3).

Implements spec 04 R12 (real shell grammar -> simple commands with argv, env
assignments, redirects; or "too complex"; overlong commands skip read-only
analysis), R13 + §4.3 (read-only fast-allow: PURE_UTILS, REGEX_ALLOW, SAFE_FLAGS
with flag types none|number|string; git ``-c``/``--exec-path``/``--config-env``
never read-only), R14 (compound commands: per-subcommand decisions combined by
severity; forced asks for >1 ``cd``, ``cd`` then git, creating git internals then
git), R15 (prefix suggestions from declarative command specs, depth <= 2, <= 10
steps, no model call), R16 (``dangerousRemoval`` safety check), R17 (sed
write/execute commands, process substitution -> ask). Also used by
``tools.builtin.bash`` for ``is_read_only`` / concurrency safety (03 R43).
Takeaway 04#T4 ("deterministic checks first").

How the parser works (design choice, see WORKPLAN G8)
----------------------------------------------------
1. ``_scan`` walks the raw string once, quote-aware, and cuts it into
   *segments* at the control operators ``; && || | |& &`` and newlines. It also
   pulls out redirections (``2>/dev/null``, ``> out``, ``<<< word``) and records
   hazards it sees on the way: command substitution ``$(...)`` / backticks,
   process substitution ``<(...)``, subshell parentheses, unquoted ``$VAR``.
2. Each segment's remaining words are re-joined and split with
   ``shlex.split`` (POSIX quote removal) into ``argv``; leading ``NAME=value``
   words become ``env``.
3. Anything we do not model - here-docs, unterminated quotes, ``if``/``for``
   blocks, brace groups, ``$((...))`` - makes the AST ``too-complex``. Unknown
   is never read-only (fail safe).

Design choices where the spec is silent
---------------------------------------
* ``has_compound`` in 04 §4.3 is read as *control-flow* compounds (``if``,
  ``for``, ...) which we classify as too complex; plain lists and pipelines of
  read-only commands (``git log | head``) stay read-only.
* ``cd`` is not read-only: it moves the persistent shell cwd (03 R39).
* Output redirects to files are checked against the working directories; a
  redirect outside them asks (WorkingDirReason).
* SAFE_FLAGS / PURE_UTILS are a conservative seed (WORKPLAN G7).
"""

from __future__ import annotations

import os
import re
import shlex
from dataclasses import dataclass, field
from typing import Literal

from harnesslab.permissions.hazards import is_dangerous_removal_target
from harnesslab.permissions.rules import bash_rule_matches
from harnesslab.permissions.types import (
    Decision,
    OtherReason,
    PermissionContext,
    PermissionUpdate,
    Rule,
    RuleReason,
    SafetyCheckReason,
    SubcommandResultsReason,
    WorkingDirReason,
)

MAX_ANALYSIS_LEN = 10_000  # design choice; spec: "overlong commands skip read-only analysis"

PURE_UTILS: frozenset[str] = frozenset(
    {
        "cat", "head", "tail", "wc", "stat", "which", "type", "expr", "seq", "pwd", "true", "false",
        "basename", "dirname", "whoami", "uname", "file", "du", "df", "nl", "rev", "tac", "cut", "tr",
        "comm", "cmp", "diff", "md5sum", "sha1sum", "sha256sum", "shasum", "cksum", "realpath",
        "readlink", "id", "groups", "printenv", "locale", "nproc", "arch", "uptime", "tty", "strings",
        "od", "hexdump", "column", "fold", "paste", "join", "fmt", "expand", "unexpand", "test", "[",
        "printf", "less", "more",
    }
)

#: Environment assignments that cannot change what a read-only command does in
#: a dangerous way (04 §4.3 ``SAFE_ENV_NAMES``; our own list).
SAFE_ENV_NAMES = frozenset({"LANG", "LC_ALL", "LC_CTYPE", "TZ", "NO_COLOR", "TERM", "COLUMNS", "GIT_PAGER", "PAGER", "FORCE_COLOR"})

FlagType = Literal["none", "number", "string"]


@dataclass
class FlagSpec:
    """Allowed flags for one ``SAFE_FLAGS`` entry. ``"-NUM"`` in ``flags`` allows
    numeric shorthands like ``git log -5``."""

    flags: dict[str, FlagType] = field(default_factory=dict)
    allow_positional: bool = True


def _fs(spec: str, *, positional: bool = True) -> FlagSpec:
    """Compact table syntax: ``"-a --all -n=number --format=string"``."""
    flags: dict[str, FlagType] = {}
    for tok in spec.split():
        name, _, typ = tok.partition("=")
        flags[name] = typ or "none"  # type: ignore[assignment]
    return FlagSpec(flags, positional)


_GIT_LOG = "-NUM -n=number --max-count=number --oneline --graph --decorate --all --stat --shortstat --name-only --name-status -p --patch --format=string --pretty=string --since=string --until=string --author=string --grep=string --reverse --no-merges --merges --first-parent --abbrev-commit --color --no-color -S=string -G=string --follow --date=string --skip=number"

#: ``"git log"`` style keys -> allowed flags (04 §4.3 SAFE_FLAGS). Seed subset.
SAFE_FLAGS: dict[str, FlagSpec] = {
    "git status": _fs("-s --short -b --branch --porcelain --long -u --untracked-files=string --ignored -v --no-color"),
    "git log": _fs(_GIT_LOG),
    "git diff": _fs("--stat --shortstat --name-only --name-status --cached --staged -p --patch --no-color --color -U=number --unified=number --word-diff --numstat --check -w --ignore-all-space --exit-code --quiet"),
    "git show": _fs("--stat --name-only --name-status -p --patch --format=string --pretty=string --oneline --no-color --color -s --no-patch"),
    "git branch": _fs("-a --all -r --remotes -v -vv --list --show-current --contains=string --merged --no-merged --no-color", positional=False),
    "git tag": _fs("-l --list -n=number --contains=string --sort=string", positional=False),
    "git remote": _fs("-v --verbose", positional=False),
    "git rev-parse": _fs("--abbrev-ref --show-toplevel --git-dir --is-inside-work-tree --short --verify --symbolic-full-name"),
    "git ls-files": _fs("-m --modified -o --others --exclude-standard -c --cached -d --deleted -s --stage -z"),
    "git blame": _fs("-L=string -w -M -C --porcelain -e -s"),
    "git describe": _fs("--tags --always --abbrev=number --long --dirty"),
    "git shortlog": _fs("-s -n -e --summary --numbered --email"),
    "git reflog": _fs("-NUM -n=number --oneline", positional=False),
    "git stash list": _fs("--oneline --stat"),
    "git worktree list": _fs("--porcelain -v"),
    "git grep": _fs("-n -i -l -c -w -E -F -e=string --count --name-only --line-number --ignore-case"),
    "git cat-file": _fs("-t -s -p -e"),
    "git merge-base": _fs("--is-ancestor --all"),
    "git rev-list": _fs("-NUM -n=number --count --all --max-count=number"),
    "git ls-tree": _fs("-r -t -l --name-only -z"),
    "git show-ref": _fs("--heads --tags -s --verify"),
    "git check-ignore": _fs("-v -q --stdin"),
    "gh pr view": _fs("--json=string --comments -w --web=none"),
    "gh pr list": _fs("--state=string --limit=number -L=number --json=string --author=string --label=string"),
    "gh pr diff": _fs("--name-only --patch --color=string"),
    "gh pr checks": _fs("--json=string --watch=none"),
    "gh pr status": _fs("--json=string"),
    "gh issue view": _fs("--json=string --comments"),
    "gh issue list": _fs("--state=string --limit=number -L=number --json=string --label=string"),
    "gh repo view": _fs("--json=string"),
    "gh run list": _fs("--limit=number -L=number --json=string --workflow=string"),
    "gh run view": _fs("--log --log-failed --json=string"),
    "docker ps": _fs("-a --all -q --quiet --format=string --no-trunc"),
    "docker images": _fs("-a --all -q --quiet --format=string"),
    "docker logs": _fs("--tail=string -n=string --since=string -t --timestamps"),
    "docker inspect": _fs("--format=string -f=string"),
    "docker version": _fs(""),
    "kubectl get": _fs("-n=string --namespace=string -o=string --output=string -A --all-namespaces -l=string --selector=string -w"),
    "kubectl describe": _fs("-n=string --namespace=string -A --all-namespaces -l=string"),
    "kubectl logs": _fs("-n=string --namespace=string --tail=number -f --follow -c=string --container=string --since=string -p --previous"),
    "grep": _fs("-n -i -l -L -c -v -w -x -r -R -E -F -o -h -H -s -q -e=string -A=number -B=number -C=number -m=number --include=string --exclude=string --exclude-dir=string --color=string --line-number --ignore-case --count --recursive --files-with-matches"),
    "egrep": _fs("-n -i -l -c -v -w -r -o -h -H"),
    "fgrep": _fs("-n -i -l -c -v -w -r -o -h -H"),
    "sort": _fs("-n -r -u -k=string -t=string -f -h -V -b -s --numeric-sort --reverse --unique"),
    "tree": _fs("-a -d -L=number -I=string -f --gitignore -C --noreport"),
    "lsof": _fs("-i=string -p=string -n -P -t"),
    "pip list": _fs("--outdated --format=string"),
    "pip show": _fs("-f --files"),
    "pip freeze": _fs(""),
    "npm ls": _fs("--depth=number --all --json --prod --dev"),
    "npm view": _fs("--json"),
    "env": FlagSpec({}, allow_positional=False),  # bare ``env`` prints the environment
}

#: Commands checked by a whole-text regex instead of flag tables (04 §4.3 REGEX_ALLOW).
REGEX_ALLOW: dict[str, re.Pattern[str]] = {
    "echo": re.compile(r"echo(?:\s.*)?", re.DOTALL),
    "ls": re.compile(r"ls(?:\s.*)?", re.DOTALL),
    "find": re.compile(r"find(?!.*\s-(?:delete|exec|execdir|ok|okdir|fprint0?|fprintf|fls)\b)(?:\s.*)?", re.DOTALL),
    "jq": re.compile(
        r"jq(?!.*(?:\s-f\b|--from-file|--rawfile|--slurpfile|\s-L\b|\benv\b|\$ENV|\binput_filename\b|\bimport\b|\binclude\b))(?:\s.*)?",
        re.DOTALL,
    ),
    "uniq": re.compile(r"uniq(?:\s+(?:-[cdiuz]+|--count|--repeated|--ignore-case|--unique))*(?:\s+[^\s-]\S*)?"),
    "date": re.compile(r"date(?:\s+(?:-u|-R|--utc|\+\S+|'\+[^']*'|\"\+[^\"]*\"))*"),
}

#: Commands whose (first) argument is itself a command (R15 ``arg_is_command``);
#: for read-only analysis we look through ``xargs`` to the inner command.
ARG_IS_COMMAND = frozenset({"xargs", "sudo", "env", "nice", "nohup", "time", "timeout", "command"})

CONTROL_KEYWORDS = frozenset({"if", "then", "else", "elif", "fi", "for", "while", "until", "do", "done", "case", "esac", "select", "function", "{", "}", "[[", "]]", "!", "coproc"})

_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_REDIRECT_OPS = ("&>>", "<<<", ">>", ">|", ">&", "<&", "<>", "&>", "<<", ">", "<")


@dataclass
class Redirect:
    op: str  # <, <<, <&, <<<, >, >>, >&, >|, &>, ...
    target: str
    fd: str = ""  # explicit fd before the operator ("2" in 2>/dev/null)

    @property
    def is_output(self) -> bool:
        return ">" in self.op

    def is_safe(self) -> bool:
        """Safe: input redirects, here-strings, ``>/dev/null``, fd duplication (04 §4.3)."""
        if self.op in ("<", "<<<", "<&"):
            return True
        if self.op in (">&", "<&") or (self.op == ">" and self.target.startswith("&")):
            return self.target.lstrip("&").isdigit() or self.target == "-"
        if self.is_output:
            return self.target == "/dev/null"
        return False


@dataclass
class SimpleCommand:
    argv: list[str]
    env: dict[str, str] = field(default_factory=dict)
    redirects: list[Redirect] = field(default_factory=list)
    text: str = ""
    background: bool = False  # followed by a single ``&``
    words: list[str] = field(default_factory=list)  # raw words (quotes kept), env words first
    has_substitution: bool = False  # $(...), backticks or <(...) inside this command
    has_process_substitution: bool = False  # <(...) / >(...)
    has_unquoted_expansion: bool = False

    def rule_text(self, *, strip_env: bool) -> str:
        """The command as rules see it: raw words joined by single spaces, without
        redirects; optionally without leading ``NAME=value`` words."""
        words = self.words
        if strip_env:
            n = 0
            while n < len(words) and _ASSIGN.match(words[n]):
                n += 1
            words = words[n:]
        return " ".join(words)

    @property
    def name(self) -> str:
        return self.argv[0] if self.argv else ""


@dataclass
class ShellAst:
    kind: Literal["ok", "too-complex"]
    commands: list[SimpleCommand] = field(default_factory=list)
    operators: list[str] = field(default_factory=list)  # ;, &&, ||, |, &
    has_subshell: bool = False
    has_command_substitution: bool = False
    has_process_substitution: bool = False
    has_unquoted_expansion: bool = False
    reason: str = ""

    def has(self, name: str) -> bool:
        return any(c.name == name for c in self.commands)


# ------------------------------------------------------------------ scanner


class _TooComplex(Exception):
    pass


@dataclass
class _Seg:
    words: list[str] = field(default_factory=list)
    redirects: list[Redirect] = field(default_factory=list)
    start: int = 0
    end: int = 0
    has_substitution: bool = False
    has_process_substitution: bool = False
    has_unquoted_expansion: bool = False


def _skip_balanced(s: str, i: int) -> int:
    """``s[i] == '('``; return the index just past the matching ``)``, honouring quotes."""
    depth = 0
    while i < len(s):
        c = s[i]
        if c == "\\":
            i += 2
            continue
        if c == "'":
            j = s.find("'", i + 1)
            if j < 0:
                raise _TooComplex("unterminated quote")
            i = j + 1
            continue
        if c == '"':
            i = _skip_double(s, i)
            continue
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    raise _TooComplex("unbalanced parentheses")


def _skip_double(s: str, i: int) -> int:
    """``s[i] == '"'``; return index past the closing quote."""
    i += 1
    while i < len(s):
        c = s[i]
        if c == "\\":
            i += 2
            continue
        if c == '"':
            return i + 1
        if c == "$" and s.startswith("$(", i):
            i = _skip_balanced(s, i + 1)
            continue
        i += 1
    raise _TooComplex("unterminated quote")


def _scan(cmd: str, ast: ShellAst) -> tuple[list[_Seg], list[str]]:
    """Split ``cmd`` into segments and operators (see module docstring)."""
    segs: list[_Seg] = [_Seg(start=0)]
    ops: list[str] = []
    word: list[str] = []
    s = cmd
    i = 0
    pending_redirect: tuple[str, str] | None = None  # (fd, op) awaiting its target word

    def end_word() -> None:
        nonlocal word, pending_redirect
        if not word:
            return
        w = "".join(word)
        word = []
        if pending_redirect is not None:
            fd, op = pending_redirect
            segs[-1].redirects.append(Redirect(op, w, fd))
            pending_redirect = None
        else:
            segs[-1].words.append(w)

    def end_segment(op: str, at: int, width: int) -> None:
        end_word()
        if pending_redirect is not None:
            raise _TooComplex("redirect without target")
        segs[-1].end = at
        ops.append(op)
        segs.append(_Seg(start=at + width))

    while i < len(s):
        c = s[i]
        if c == "\\":
            word.append(s[i : i + 2])
            i += 2
            continue
        if c == "'":
            j = s.find("'", i + 1)
            if j < 0:
                raise _TooComplex("unterminated quote")
            word.append(s[i : j + 1])
            i = j + 1
            continue
        if c == '"':
            j = _skip_double(s, i)
            inner = s[i:j]
            if "$(" in inner or "`" in inner:
                ast.has_command_substitution = True
                segs[-1].has_substitution = True
            word.append(inner)
            i = j
            continue
        if c == "`":
            j = s.find("`", i + 1)
            if j < 0:
                raise _TooComplex("unterminated backtick")
            ast.has_command_substitution = True
            segs[-1].has_substitution = True
            word.append(s[i : j + 1])
            i = j + 1
            continue
        if c == "$":
            if s.startswith("$((", i):
                raise _TooComplex("arithmetic expansion")
            if s.startswith("$(", i):
                j = _skip_balanced(s, i + 1)
                ast.has_command_substitution = True
                segs[-1].has_substitution = True
                word.append(s[i:j])
                i = j
                continue
            nxt = s[i + 1] if i + 1 < len(s) else ""
            if nxt == "{" or nxt.isalnum() or nxt in "_@*#?$!-":
                ast.has_unquoted_expansion = True
                segs[-1].has_unquoted_expansion = True
            word.append(c)
            i += 1
            continue
        if c in "<>" and s.startswith("(", i + 1):
            j = _skip_balanced(s, i + 1)
            ast.has_process_substitution = True
            segs[-1].has_substitution = True
            segs[-1].has_process_substitution = True
            word.append(s[i:j])
            i = j
            continue
        if c == "#" and not word:
            nl = s.find("\n", i)
            i = len(s) if nl < 0 else nl
            continue
        if c in " \t":
            end_word()
            i += 1
            continue
        if c == "\n":
            end_segment(";", i, 1)
            i += 1
            continue
        if c == "(":
            if word:
                raise _TooComplex("unexpected '('")
            ast.has_subshell = True
            if segs[-1].words:
                end_segment(";", i, 1)
            i += 1
            continue
        if c == ")":
            ast.has_subshell = True
            end_segment(";", i, 1)
            i += 1
            continue
        # redirections (checked before & and | so "&>" and ">|" win)
        op = next((o for o in _REDIRECT_OPS if s.startswith(o, i)), None)
        if op is not None:
            fd = ""
            if word and "".join(word).isdigit() and op[0] in "<>":
                fd = "".join(word)
                word = []
            end_word()
            if op == "<<":
                raise _TooComplex("here-document")
            i += len(op)
            # target may follow immediately ("2>&1") or after spaces
            while i < len(s) and s[i] in " \t":
                i += 1
            pending_redirect = (fd, op)
            continue
        for o in ("&&", "||", "|&", ";;", "|", ";", "&"):
            if s.startswith(o, i):
                if o == ";;":
                    raise _TooComplex("case syntax")
                end_segment(o, i, len(o))
                i += len(o)
                break
        else:
            word.append(c)
            i += 1
    end_word()
    if pending_redirect is not None:
        raise _TooComplex("redirect without target")
    segs[-1].end = len(s)
    return segs, ops


def parse_shell(cmd: str) -> ShellAst:
    """Parse ``cmd`` into simple commands (04 R12) - or mark it too complex."""
    ast = ShellAst("ok")
    try:
        segs, ops = _scan(cmd, ast)
        commands: list[SimpleCommand] = []
        kept_ops: list[str] = []
        for idx, seg in enumerate(segs):
            op_after = ops[idx] if idx < len(ops) else None
            if not seg.words and not seg.redirects:
                # empty segments are only legal around subshell parens / trailing ';' / '&'
                op_before = ops[idx - 1] if idx > 0 else None
                binary = ("&&", "||", "|", "|&")
                if (op_after in binary or op_before in binary) and not ast.has_subshell:
                    raise _TooComplex("missing command")
                continue
            try:
                argv = shlex.split(" ".join(seg.words), posix=True) if seg.words else []
            except ValueError as e:
                raise _TooComplex(str(e)) from e
            env: dict[str, str] = {}
            n = 0
            while n < len(argv) and _ASSIGN.match(argv[n]):
                k, _, v = argv[n].partition("=")
                env[k] = v
                n += 1
            argv = argv[n:]
            if argv and argv[0] in CONTROL_KEYWORDS:
                raise _TooComplex(f"control structure '{argv[0]}'")
            commands.append(
                SimpleCommand(
                    argv=argv,
                    env=env,
                    redirects=seg.redirects,
                    text=cmd[seg.start : seg.end].strip(),
                    background=op_after == "&",
                    words=list(seg.words),
                    has_substitution=seg.has_substitution,
                    has_process_substitution=seg.has_process_substitution,
                    has_unquoted_expansion=seg.has_unquoted_expansion,
                )
            )
            if op_after is not None:
                kept_ops.append(op_after)
        ast.commands = commands
        ast.operators = kept_ops
        if not commands:
            raise _TooComplex("empty command")
    except _TooComplex as e:
        return ShellAst(
            "too-complex",
            reason=str(e),
            has_command_substitution=ast.has_command_substitution,
            has_process_substitution=ast.has_process_substitution,
            has_subshell=ast.has_subshell,
        )
    return ast


# ------------------------------------------------------------------ read-only analysis


def _subcommand_key(argv: list[str]) -> tuple[str | None, int]:
    """Longest ``SAFE_FLAGS`` key matching the leading words; returns
    ``(key, words_consumed)``. For git, ``--no-pager`` and ``-C <dir>`` before
    the subcommand are skipped."""
    words = list(argv)
    skipped = 0
    if words and words[0] == "git":
        rest = words[1:]
        while rest and rest[0] in ("--no-pager", "-C", "-P", "--paginate=never"):
            drop = 2 if rest[0] == "-C" else 1
            rest = rest[drop:]
            skipped += drop
        words = ["git"] + rest
    for n in (3, 2, 1):
        if len(words) >= n:
            key = " ".join(words[:n])
            if key in SAFE_FLAGS:
                return key, n + skipped
    return None, 0


def _flag_ok(spec: FlagSpec, args: list[str]) -> bool:
    """Every argument is an allowed flag (with a well-typed value) or an allowed
    positional (04 §4.3 flag types none | number | string)."""
    i = 0
    only_positional = False
    while i < len(args):
        a = args[i]
        if only_positional or not a.startswith("-") or a == "-":
            if not spec.allow_positional:
                return False
            i += 1
            continue
        if a == "--":
            only_positional = True
            i += 1
            continue
        if re.fullmatch(r"-\d+", a):
            if "-NUM" not in spec.flags:
                return False
            i += 1
            continue
        if a.startswith("--"):
            name, eq, value = a.partition("=")
            typ = spec.flags.get(name)
            if typ is None:
                return False
            if typ == "none":
                if eq:
                    return False
            else:
                if not eq:
                    if i + 1 >= len(args):
                        return False
                    i += 1
                    value = args[i]
                if typ == "number" and not value.isdigit():
                    return False
            i += 1
            continue
        # short flag(s): -n5, -n 5, -abc
        name = a[:2]
        typ = spec.flags.get(name)
        if typ is None:
            return False
        if typ == "none":
            for ch in a[2:]:  # combined: every letter must be a no-value flag
                if spec.flags.get("-" + ch) != "none":
                    return False
            i += 1
            continue
        value = a[2:]
        if not value:
            if i + 1 >= len(args):
                return False
            i += 1
            value = args[i]
        if typ == "number" and not value.isdigit():
            return False
        i += 1
    return True


_SED_SAFE_FLAGS = _fs("-n -E -r --quiet --silent --regexp-extended -e=string -s -u -z")


def sed_has_write_or_exec(c: SimpleCommand) -> bool:
    """04 R17: ``w``/``W`` (write file) and ``e`` (execute) sed commands, ``-i`` in
    place editing excluded here (that is just a write). We look at every script
    argument; this is a conservative textual check, not a full sed parser."""
    if c.name != "sed":
        return False
    scripts: list[str] = []
    args = c.argv[1:]
    i = 0
    saw_e = False
    while i < len(args):
        a = args[i]
        if a in ("-e", "--expression") and i + 1 < len(args):
            scripts.append(args[i + 1])
            saw_e = True
            i += 2
            continue
        if a.startswith("--expression="):
            scripts.append(a.split("=", 1)[1])
            saw_e = True
        elif a in ("-f", "--file") or a.startswith("--file="):
            return True  # script from a file: cannot inspect -> treat as risky
        elif not a.startswith("-") and not saw_e and not scripts:
            scripts.append(a)
        i += 1
    for sc in scripts:
        # strip the regex parts of s/// so 'w' inside a pattern doesn't count
        body = re.sub(r"s(.)(?:\\.|(?!\1).)*\1(?:\\.|(?!\1).)*\1", "s", sc)
        if re.search(r"(?:^|[;{}\s\d$/])[wWe](?:\s|$)", body) or re.search(r"s[gpiImMe0-9]*[we]", body):
            return True
    return False


def _sed_is_read_only(c: SimpleCommand) -> bool:
    if any(a.startswith("-i") or a.startswith("--in-place") for a in c.argv[1:]):
        return False
    if sed_has_write_or_exec(c):
        return False
    return _flag_ok(_SED_SAFE_FLAGS, c.argv[1:])


def simple_is_read_only(c: SimpleCommand) -> bool:
    """Is this one simple command read-only by the allowlists (04 §4.3)?"""
    if not c.argv:
        return False
    name = c.argv[0]
    if name in PURE_UTILS:
        return True
    if name == "sed":
        return _sed_is_read_only(c)
    if name == "xargs":
        # look through to the command xargs will run (R15 arg_is_command)
        args = c.argv[1:]
        while args and args[0].startswith("-"):
            if args[0] in ("-0", "-r", "-t", "--null", "--no-run-if-empty"):
                args = args[1:]
            elif args[0] in ("-n", "-L", "-P", "-I") and len(args) > 1:
                args = args[2:]
            else:
                return False
        inner = SimpleCommand(argv=args, words=[shlex.quote(a) for a in args])
        return bool(args) and simple_is_read_only(inner)
    if name in REGEX_ALLOW:
        return REGEX_ALLOW[name].fullmatch(c.rule_text(strip_env=True)) is not None
    key, consumed = _subcommand_key(c.argv)
    if key is None:
        return False
    if name == "git" and any(
        a in ("-c", "--exec-path", "--config-env") or a.startswith(("--exec-path=", "--config-env=", "-c="))
        for a in c.argv
    ):
        return False
    return _flag_ok(SAFE_FLAGS[key], c.argv[consumed:])


def _mentions_git_internals_or_unc(cmd: str) -> bool:
    return "/dev/tcp" in cmd or "/dev/udp" in cmd or "--git-dir" in cmd or "GIT_DIR" in cmd or cmd.lstrip().startswith("\\\\") or " \\\\" in cmd


def is_read_only(cmd: str) -> bool:
    """04 §4.3 ``is_read_only``: every subcommand is on an allowlist and nothing
    in the line can write, run hidden code or escape analysis."""
    if len(cmd) > MAX_ANALYSIS_LEN:
        return False
    ast = parse_shell(cmd)
    if ast.kind != "ok":
        return False
    if (
        ast.has_subshell
        or ast.has_command_substitution
        or ast.has_process_substitution
        or ast.has_unquoted_expansion
        or any(c.background for c in ast.commands)
        or any(set(c.env) - SAFE_ENV_NAMES for c in ast.commands)
        or (ast.has("cd") and ast.has("git"))
        or _mentions_git_internals_or_unc(cmd)
        or any(not r.is_safe() for c in ast.commands for r in c.redirects)
    ):
        return False
    return all(simple_is_read_only(c) for c in ast.commands)


# ------------------------------------------------------------------ full check


_GIT_INTERNAL_PARTS = ("HEAD", "objects", "refs", "hooks")  # bare-repository layout


def _creates_git_internals(c: SimpleCommand) -> bool:
    """A write-ish command whose arguments look like git-internal paths (a bare
    repository layout, or ``.git/...``) - followed by git it could plant hooks."""
    if c.name not in ("mkdir", "touch", "cp", "mv", "tee", "ln", "echo", "printf", "cat"):
        return False
    targets = list(c.argv[1:]) + [r.target for r in c.redirects if r.is_output]
    redirect_targets = {r.target for r in c.redirects if r.is_output}
    for t in targets:
        if c.name in ("echo", "printf", "cat") and t not in redirect_targets:
            continue  # only their redirect targets are written
        parts = [p for p in t.replace("\\", "/").split("/") if p not in ("", ".")]
        if ".git" in parts or (parts and parts[0] in _GIT_INTERNAL_PARTS):
            return True
    return False


def _within(path: str, roots: list[str]) -> bool:
    p = os.path.normpath(path)
    return any(p == os.path.normpath(r) or p.startswith(os.path.normpath(r).rstrip(os.sep) + os.sep) for r in roots)


def _removal_targets(c: SimpleCommand) -> list[str]:
    """Raw (unexpanded) target words of an rm/rmdir command."""
    out: list[str] = []
    only_pos = False
    words = c.words[len(c.env) :][1:]  # raw words after the command name
    for w in words:
        if w == "--" and not only_pos:
            only_pos = True
            continue
        if w.startswith("-") and not only_pos:
            continue
        out.append(w.strip("'\"") if not w.startswith("$") else w)
    return out


def _rule_decision(c: SimpleCommand, rules: list[Rule]) -> Rule | None:
    for r in rules:
        # whole-tool rules are the pipeline's business (04 §4.1 steps 1, 2, 12)
        if r.tool_name not in ("Bash",) or r.content is None:
            continue
        if r.behavior == "allow":
            if c.has_substitution:
                continue
            safe_env = not (set(c.env) - SAFE_ENV_NAMES)
            if bash_rule_matches(r, c.rule_text(strip_env=False)) or (safe_env and bash_rule_matches(r, c.rule_text(strip_env=True))):
                return r
        elif bash_rule_matches(r, c.rule_text(strip_env=False)) or bash_rule_matches(r, c.rule_text(strip_env=True)):
            return r
    return None


def _suggestion(subs: list[SimpleCommand]) -> list[PermissionUpdate]:
    rules: list[Rule] = []
    for c in subs:
        prefix = suggest_prefix(c.argv)
        if prefix:
            r = Rule("Bash", " ".join(prefix) + ":*", "allow")
            if r not in rules:
                rules.append(r)
    return [PermissionUpdate("addRules", "localSettings", rules[:5], "allow")] if rules else []


def check_bash(
    cmd: str,
    perm: PermissionContext,
    *,
    cwd: str,
    home: str,
    allow_rules: list[Rule] | None = None,
    working_dirs: list[str] | None = None,
) -> Decision:
    """04 §4.3 ``check_bash``: per-subcommand decisions, worst severity wins,
    ``SubcommandResultsReason`` attached.

    ``allow_rules`` lets the caller pass the *effective* allow rules (auto mode
    strips broad ones, 04 §4.6); default: every allow rule in ``perm``.
    Per subcommand, in order: deny rule -> dangerous removal (safety check) ->
    sed write/exec or process substitution (ask) -> background ``&`` (safety
    check) -> output redirect outside the working dirs (ask) -> ask rule ->
    read-only (allow) -> allow rule (allow) -> passthrough.
    """
    ast = parse_shell(cmd)
    if ast.kind != "ok":
        return Decision.passthrough(OtherReason(f"command too complex to analyse ({ast.reason})"))
    subs = ast.commands
    roots = [cwd] + list(working_dirs or []) + list(perm.additional_directories)
    names = [c.name for c in subs]
    if names.count("cd") > 1:
        return Decision.ask(OtherReason("multiple directory changes in one command"), "Command changes directory more than once.")
    if "cd" in names and "git" in names[names.index("cd") + 1 :]:
        return Decision.ask(
            OtherReason("cd followed by git may load an untrusted repository's hooks or config"),
            "Command changes directory and then runs git.",
        )
    for i, c in enumerate(subs):
        if _creates_git_internals(c) and "git" in names[i + 1 :]:
            return Decision.ask(OtherReason("creates git-internal files and then runs git"), "Command creates git-internal files, then runs git.")

    deny_ask = [r for r in perm.rules if r.behavior in ("deny", "ask")]
    deny_rules = [r for r in deny_ask if r.behavior == "deny"]
    ask_rules = [r for r in deny_ask if r.behavior == "ask"]
    allows = allow_rules if allow_rules is not None else perm.rules_with("allow")
    analysable = len(cmd) <= MAX_ANALYSIS_LEN
    results: list[Decision] = []
    for c in subs:
        if (r := _rule_decision(c, deny_rules)) is not None:
            results.append(Decision.deny(f"Denied by rule {r}", RuleReason(r)))
        elif c.name in ("rm", "rmdir") and any(
            is_dangerous_removal_target(t, cwd=cwd, home=home) for t in _removal_targets(c) or [""]
        ):
            results.append(
                Decision.ask(
                    SafetyCheckReason(
                        f"'{c.text}' could remove a system, home or workspace directory (or an empty variable path).",
                        classifier_approvable=True,
                        circuit_breaker="dangerousRemoval",
                    )
                )
            )
        elif sed_has_write_or_exec(c) or c.has_process_substitution:
            results.append(Decision.ask(OtherReason("sed write/execute command or process substitution")))
        elif c.background:
            results.append(
                Decision.ask(
                    SafetyCheckReason(
                        "Command runs a process in the background with '&'.",
                        classifier_approvable=True,
                        circuit_breaker="backgroundOperator",
                    )
                )
            )
        elif any(
            r.is_output and not r.is_safe() and not _within(os.path.join(cwd, os.path.expanduser(r.target)), roots)
            for r in c.redirects
        ):
            bad = next(r.target for r in c.redirects if r.is_output and not r.is_safe())
            d = Decision.ask(WorkingDirReason(bad), f"Output redirect to {bad} is outside the working directories.")
            d.blocked_path = bad
            results.append(d)
        elif (r := _rule_decision(c, ask_rules)) is not None:
            results.append(Decision.ask(RuleReason(r), matched_ask_rule=r))
        elif (
            analysable
            and not c.has_substitution
            and not c.has_unquoted_expansion
            and not (set(c.env) - SAFE_ENV_NAMES)
            and all(r.is_safe() for r in c.redirects)
            and simple_is_read_only(c)
        ):
            results.append(Decision.allow(OtherReason("read-only command")))
        elif (r := _rule_decision(c, allows)) is not None:
            results.append(Decision.allow(RuleReason(r)))
        else:
            results.append(Decision.passthrough(OtherReason(f"no rule covers '{c.text}'")))

    if len(results) == 1:
        d = results[0]
        if d.behavior in ("ask", "passthrough"):
            d.suggestions = _suggestion(subs)
        return d
    worst = max(results, key=lambda d: d.severity)
    if worst.behavior == "allow":
        return Decision.allow(SubcommandResultsReason(results))
    out = Decision(
        worst.behavior,
        worst.reason if worst.reason.type in ("safetyCheck", "rule", "workingDir") else SubcommandResultsReason(results),
        message=worst.message,
        matched_ask_rule=worst.matched_ask_rule,
        blocked_path=worst.blocked_path,
    )
    if out.behavior in ("ask", "passthrough"):
        out.suggestions = _suggestion([c for c, d in zip(subs, results) if d.behavior != "allow"])
    return out


# ------------------------------------------------------------------ prefix suggestions


@dataclass
class CommandSpec:
    """Declarative command shape for prefix suggestions (04 R15)."""

    subcommands: dict[str, "CommandSpec | None"] = field(default_factory=dict)
    arg_is_command: bool = False  # sudo, xargs, env ...: the next word starts a command
    positional_is_subcommand: bool = False  # "npm run <script>": the script name is part of the prefix


_LEAF = None
COMMAND_SPECS: dict[str, CommandSpec] = {
    "git": CommandSpec({k: _LEAF for k in "add commit push pull fetch checkout switch branch merge rebase reset restore stash status log diff show tag remote clone init cherry-pick revert worktree".split()}),
    "npm": CommandSpec({"run": CommandSpec(positional_is_subcommand=True), "test": _LEAF, "install": _LEAF, "ci": _LEAF, "exec": _LEAF, "ls": _LEAF}),
    "pnpm": CommandSpec({"run": CommandSpec(positional_is_subcommand=True), "test": _LEAF, "install": _LEAF, "add": _LEAF}),
    "yarn": CommandSpec({"run": CommandSpec(positional_is_subcommand=True), "test": _LEAF, "add": _LEAF, "install": _LEAF}),
    "cargo": CommandSpec({k: _LEAF for k in "build test run check clippy fmt add".split()}),
    "go": CommandSpec({k: _LEAF for k in "build test run vet fmt mod get".split()}),
    "docker": CommandSpec({k: _LEAF for k in "build run ps logs exec compose images pull push".split()}),
    "kubectl": CommandSpec({k: _LEAF for k in "get describe logs apply delete exec".split()}),
    "gh": CommandSpec({"pr": CommandSpec({k: _LEAF for k in "view list create checkout diff merge".split()}), "issue": CommandSpec({k: _LEAF for k in "view list create".split()})}),
    "make": CommandSpec(positional_is_subcommand=True),
    "uv": CommandSpec({"run": CommandSpec(arg_is_command=True), "pip": _LEAF, "sync": _LEAF, "add": _LEAF}),
    "python": CommandSpec({"-m": CommandSpec(positional_is_subcommand=True)}),
    "python3": CommandSpec({"-m": CommandSpec(positional_is_subcommand=True)}),
    "sudo": CommandSpec(arg_is_command=True),
    "xargs": CommandSpec(arg_is_command=True),
    "timeout": CommandSpec(arg_is_command=True),
    "env": CommandSpec(arg_is_command=True),
}

MAX_SUGGEST_DEPTH = 2
MAX_SUGGEST_STEPS = 10


def suggest_prefix(argv: list[str]) -> list[str]:
    """Derive an "always allow" prefix from declarative specs, no model call (04 R15).

    ``git commit -m x`` -> ``git commit``; ``npm run build`` -> ``npm run build``;
    ``sudo npm test`` -> ``sudo npm test``; unknown ``foo bar`` -> ``foo``.
    Recursion is bounded: depth <= 2, <= 10 steps.
    """
    steps = 0

    def walk(words: list[str], spec: CommandSpec | None, depth: int) -> list[str]:
        nonlocal steps
        steps += 1
        if not words or spec is None or depth > MAX_SUGGEST_DEPTH or steps > MAX_SUGGEST_STEPS:
            return []
        if spec.positional_is_subcommand:
            nxt = next((w for w in words if not w.startswith("-")), None)
            return [nxt] if nxt else []
        if spec.arg_is_command:
            rest = [w for w in words if not w.startswith("-") and not _ASSIGN.match(w)]
            if not rest:
                return []
            head_ = rest[0]
            return [head_] + walk(rest[1:], COMMAND_SPECS.get(head_), depth + 1)
        if words[0] in spec.subcommands:
            return [words[0]] + walk(words[1:], spec.subcommands[words[0]], depth + 1)
        return []

    words = [w for w in argv]
    while words and _ASSIGN.match(words[0]):
        words = words[1:]
    if not words:
        return []
    return [words[0]] + walk(words[1:], COMMAND_SPECS.get(words[0]), 0)
