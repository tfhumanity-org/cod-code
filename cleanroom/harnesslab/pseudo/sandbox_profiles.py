"""[PSEUDO] OS sandbox profiles and egress policy (WP3).

LABEL: this module is *documented pseudocode in Python form*. The profile
*generators* are real, testable string builders, but nothing here executes
``sandbox-exec`` or ``bwrap``; ``SimulatedSandbox`` stands in for enforcement
(path checks in Python). Real enforcement is OS-specific and out of scope for a
stdlib reference implementation.

Implements spec 04 R41-R46 and §4.8-§4.9; takeaway 04#T10; P20.

* ``darwin_profile``: deny-default SBPL text: process exec/fork, mach-lookup
  allowances, network restricted to the local proxy ports or ``(allow
  network*)``, ``file-read*`` allowed minus ``deny_read``, ``file-write*`` only
  in ``allow_write`` minus protected literals, parents of protected paths not
  unlinkable; command tagged ``CMD64_<b64>_END`` for violation attribution (R46).
* ``linux_bwrap_args``: ``bwrap`` argv with ``--unshare-net`` + proxy sockets,
  ro/rw binds, protected overlays, optional seccomp helper; proxy env
  ``HTTP_PROXY=http://localhost:3128``, ``ALL_PROXY=socks5://localhost:1080``.
* ``PROTECTED_NAMES``: always write-protected even inside writable roots (R42).
* ``NetworkPolicy.allow_host``: deny list -> allow list -> strict -> per-call
  allowed domains -> ask callback (§4.9).
* Credential masking (R44) is represented by ``CredentialMask`` sentinel mapping.
* ``sandbox_wrap`` dispatches by platform and raises ``SandboxUnavailable``
  elsewhere (fatal when ``failIfUnavailable``, R45).
* ``sandbox_decision`` (R45): ``autoAllowBashIfSandboxed``, ``excludedCommands``
  and ``dangerouslyDisableSandbox`` -> whether a call runs sandboxed and whether
  it may skip prompts.

Deferred: TLS interception, AWS SigV4 re-signing (R44), the seccomp syscall list
(spec 04 §8), Windows sandboxing, the real ``log stream`` watcher (we decode tags
from given lines instead).
"""

from __future__ import annotations

import base64
import binascii
import fnmatch
import os
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

PROTECTED_NAMES = (
    ".gitconfig",
    ".gitmodules",
    ".bashrc",
    ".bash_profile",
    ".zshrc",
    ".zprofile",
    ".profile",
    ".ripgreprc",
    ".mcp.json",
    ".vscode",
    ".idea",
    ".claude/commands",
    ".claude/agents",
    ".git/hooks",
    ".git/config",  # unless allowGitConfig
)
#: Entries of ``PROTECTED_NAMES`` that are directories (protected recursively).
PROTECTED_DIR_SUFFIXES = ("/.vscode", "/.idea", "/.claude/commands", "/.claude/agents", "/.git/hooks")
LINUX_HTTP_PROXY_PORT = 3128
LINUX_SOCKS_PROXY_PORT = 1080
SCAN_DEPTH = 3

#: Mach services a typical command-line program needs on macOS (our own list).
MACH_SERVICES = (
    "com.apple.system.logger",
    "com.apple.system.notification_center",
    "com.apple.system.opendirectoryd.libinfo",
    "com.apple.system.opendirectoryd.membership",
    "com.apple.SecurityServer",
    "com.apple.trustd.agent",
    "com.apple.coreservices.launchservicesd",
    "com.apple.lsd.mapdb",
    "com.apple.CoreServices.coreservicesd",
    "com.apple.FSEvents",
    "com.apple.diagnosticd",
    "com.apple.logd",
    "com.apple.cfprefsd.daemon",
    "com.apple.cfprefsd.agent",
    "com.apple.dnssd.service",
)

_TAG_RE = re.compile(r"CMD64_([A-Za-z0-9+/=]+)_END")


class SandboxUnavailable(RuntimeError):
    """No sandbox mechanism on this platform (04 R45 ``failIfUnavailable``)."""


@dataclass
class SandboxConfig:
    allow_write: list[str] = field(default_factory=list)
    deny_read: list[str] = field(default_factory=list)
    deny_within_allow: list[str] = field(default_factory=list)
    network_restricted: bool = True
    http_port: int = LINUX_HTTP_PROXY_PORT
    socks_port: int = LINUX_SOCKS_PROXY_PORT
    allow_git_config: bool = False
    allow_pty: bool = False
    allow_all_unix_sockets: bool = False
    log_tag: str = "harnesslab-sandbox"
    enable_weaker_network_isolation: bool = False
    enable_weaker_nested_sandbox: bool = False
    allow_apple_events: bool = False
    http_socket: str = "/tmp/harnesslab-http-proxy.sock"  # Linux: host proxy bridged into the netns
    socks_socket: str = "/tmp/harnesslab-socks-proxy.sock"


class SandboxBackend(Protocol):
    def wrap(self, command: str, cfg: SandboxConfig) -> tuple[list[str], dict[str, str]]:
        """Return ``(argv, extra_env)`` to execute ``command`` confined."""
        ...


# ------------------------------------------------------------------ command tags (R46)


def tag_command(cmd: str) -> str:
    """``CMD64_<base64(cmd)>_END``: appended to the sandboxed shell line as a
    comment so a violation log line can be traced back to its command (R46).
    Standard base64 has no ``_``, so the terminator is unambiguous."""
    return "CMD64_" + base64.b64encode(cmd.encode("utf-8")).decode("ascii") + "_END"


def decode_command_tag(line: str) -> str | None:
    """Recover the originating command from any text containing a tag."""
    m = _TAG_RE.search(line)
    if not m:
        return None
    try:
        return base64.b64decode(m.group(1), validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError):
        return None


# ------------------------------------------------------------------ protected paths (R42)


def _protected_names(allow_git_config: bool) -> list[str]:
    return [n for n in PROTECTED_NAMES if not (allow_git_config and n == ".git/config")]


def scan_protected(roots: list[str], *, depth: int = SCAN_DEPTH, allow_git_config: bool = False) -> list[str]:
    """Paths that stay write-protected inside writable roots (R42).

    Each root gets every protected name directly below it (whether or not it
    exists yet - denying creation matters too), plus any occurrence found by a
    bounded-depth directory walk (nested repositories, sub-projects). Large
    vendored trees are not descended into.
    """
    names = _protected_names(allow_git_config)
    found: set[str] = set()
    skip_dirs = {"node_modules", ".venv", "venv", "__pycache__", "target", "dist", "build"}
    for root in roots:
        root = os.path.normpath(root)
        for n in names:
            found.add(os.path.join(root, n))
        if not os.path.isdir(root):
            continue
        base_depth = root.count(os.sep)
        for dirpath, dirnames, _files in os.walk(root):
            if dirpath.count(os.sep) - base_depth >= depth:
                dirnames[:] = []
            dirnames[:] = [d for d in dirnames if d not in skip_dirs and d != ".git"]
            for n in names:
                cand = os.path.join(dirpath, n)
                if os.path.lexists(cand):
                    found.add(cand)
    return sorted(found)


def _parents(paths: list[str]) -> list[str]:
    """Every ancestor directory of the protected paths: renaming or unlinking an
    ancestor would move the protected file out from under its rule (R42)."""
    out: set[str] = set()
    for p in paths:
        d = os.path.dirname(os.path.normpath(p))
        while d and d != os.sep:
            out.add(d)
            d = os.path.dirname(d)
    return sorted(out)


def _sbpl(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


# ------------------------------------------------------------------ macOS (§4.8)


def darwin_profile(cmd: str, cfg: SandboxConfig, protected: list[str]) -> str:
    """Seatbelt (SBPL) profile text for one command. Deny by default; reads are
    broadly allowed except ``deny_read``; writes only under ``allow_write``
    minus protected literals; network only to the local proxy when restricted.
    ``cmd`` is not embedded in the profile (it goes on the command line with its
    tag) but is accepted so profiles could be specialised per command."""
    prof: list[str] = [
        "(version 1)",
        f'(deny default (with message "{_sbpl(cfg.log_tag)}"))',
        "(allow process-exec process-fork)",
        "(allow signal (target same-sandbox))",
        *[f'(allow mach-lookup (global-name "{s}"))' for s in MACH_SERVICES],
        "(allow sysctl-read)",
        "(allow iokit-open (iokit-user-client-class \"RootDomainUserClient\"))",
        "(allow ipc-posix-shm-read-data ipc-posix-shm-write-data ipc-posix-sem)",
    ]
    if cfg.network_restricted:
        prof += [
            f'(allow network-outbound (remote ip "localhost:{cfg.http_port}"))',
            f'(allow network-outbound (remote ip "localhost:{cfg.socks_port}"))',
            '(allow network-bind (local ip "localhost:*"))',
            '(allow network-inbound (local ip "localhost:*"))',
            '(allow network-outbound (remote unix-socket (path-literal "/private/var/run/mDNSResponder")))',
        ]
        if cfg.allow_all_unix_sockets:
            prof.append("(allow network* (remote unix-socket))")
    else:
        prof.append("(allow network*)")
    prof.append("(allow file-read*)")
    prof += [f'(deny file-read* (subpath "{_sbpl(p)}"))' for p in cfg.deny_read]
    prof.append('(allow file-write* (literal "/dev/null") (literal "/dev/stdout") (literal "/dev/stderr"))')
    prof += [f'(allow file-write* (subpath "{_sbpl(p)}"))' for p in cfg.allow_write]
    all_protected = sorted(set(cfg.deny_within_allow) | set(protected))
    prof += [f'(deny file-write* (literal "{_sbpl(p)}"))' for p in all_protected]
    # protected *directories* are closed as whole sub-trees, not just the entry itself
    prof += [f'(deny file-write* (subpath "{_sbpl(p)}"))' for p in all_protected if p.endswith(PROTECTED_DIR_SUFFIXES)]
    prof += [f'(deny file-write-unlink (literal "{_sbpl(d)}"))' for d in _parents(all_protected)]
    if cfg.allow_pty:
        prof += ["(allow pseudo-tty)", '(allow file-read* file-write* file-ioctl (regex #"^/dev/ttys[0-9]+$"))']
    if cfg.enable_weaker_network_isolation:
        prof.append('(allow mach-lookup (global-name "com.apple.trustd"))')
    if cfg.allow_apple_events:
        prof.append("(allow appleevent-send)")
    return "\n".join(prof)


def proxy_env(cfg: SandboxConfig) -> dict[str, str]:
    """Environment pointing every common client at the host-side proxy (R43)."""
    http = f"http://localhost:{cfg.http_port}"
    socks = f"socks5h://localhost:{cfg.socks_port}"
    return {
        "HTTP_PROXY": http,
        "HTTPS_PROXY": http,
        "http_proxy": http,
        "https_proxy": http,
        "ALL_PROXY": socks,
        "all_proxy": socks,
        "NO_PROXY": "localhost,127.0.0.1,::1",
    }


def darwin_wrap(cmd: str, cfg: SandboxConfig, protected: list[str]) -> tuple[list[str], dict[str, str]]:
    env = proxy_env(cfg) if cfg.network_restricted else {}
    argv = ["/usr/bin/sandbox-exec", "-p", darwin_profile(cmd, cfg, protected), "bash", "-c", f"{cmd} # {tag_command(cmd)}"]
    return argv, env


# ------------------------------------------------------------------ Linux (§4.8)


def linux_bwrap_args(cmd: str, cfg: SandboxConfig, protected: list[str]) -> tuple[list[str], dict[str, str]]:
    """bubblewrap argv: new namespaces, read-only root, writable roots bound
    read-write, protected paths re-bound read-only on top, unreadable paths
    hidden behind an empty tmpfs. With network restriction the command gets no
    network namespace except two unix sockets bridged (by socat) to the host
    proxy, exposed inside as localhost:3128 / :1080."""
    args = ["bwrap", "--new-session", "--die-with-parent", "--unshare-pid", "--unshare-user", "--dev", "/dev"]
    args += ["--bind", "/proc", "/proc"] if cfg.enable_weaker_nested_sandbox else ["--proc", "/proc"]
    args += ["--ro-bind", "/", "/"]
    for p in cfg.allow_write:
        args += ["--bind", p, p]
    for p in sorted(set(cfg.deny_within_allow) | set(protected)):
        if os.path.lexists(p):  # bwrap cannot bind a missing path
            args += ["--ro-bind", p, p]
    for p in cfg.deny_read:
        args += ["--tmpfs", p]
    inner_cmd = cmd
    if cfg.network_restricted:
        args += ["--unshare-net"]
        args += ["--bind", cfg.http_socket, cfg.http_socket, "--bind", cfg.socks_socket, cfg.socks_socket]
        bridges = (
            f"socat TCP-LISTEN:{LINUX_HTTP_PROXY_PORT},fork,reuseaddr UNIX-CONNECT:{cfg.http_socket} & "
            f"socat TCP-LISTEN:{LINUX_SOCKS_PROXY_PORT},fork,reuseaddr UNIX-CONNECT:{cfg.socks_socket} & "
        )
        inner_cmd = bridges + cmd
    inner = [] if cfg.allow_all_unix_sockets else ["apply-seccomp", "--block-af-unix"]
    env = (
        {"HTTP_PROXY": f"http://localhost:{LINUX_HTTP_PROXY_PORT}", "HTTPS_PROXY": f"http://localhost:{LINUX_HTTP_PROXY_PORT}", "ALL_PROXY": f"socks5://localhost:{LINUX_SOCKS_PROXY_PORT}"}
        if cfg.network_restricted
        else {}
    )
    return args + ["--", *inner, "bash", "-c", f"{inner_cmd} # {tag_command(cmd)}"], env


def sandbox_wrap(cmd: str, cfg: SandboxConfig, *, platform: str) -> tuple[list[str], dict[str, str]]:
    """Dispatch per platform (04 §4.8 ``sandbox_wrap``). Never executes anything."""
    protected = scan_protected(cfg.allow_write, allow_git_config=cfg.allow_git_config)
    if platform == "darwin":
        return darwin_wrap(cmd, cfg, protected)
    if platform.startswith("linux"):
        return linux_bwrap_args(cmd, cfg, protected)
    raise SandboxUnavailable(f"no sandbox available on {platform}")


def config_from_settings(sandbox: dict[str, Any], *, cwd: str, tmp: str = "/tmp") -> SandboxConfig:
    """Build a ``SandboxConfig`` from ``settings.sandbox`` (key nesting partly
    inferred, spec 04 §5). The working directory and a temp dir are writable."""
    net = sandbox.get("network", {}) or {}
    return SandboxConfig(
        allow_write=[cwd, tmp] + list(sandbox.get("allowWrite", [])),
        deny_read=list(sandbox.get("denyRead", [])),
        network_restricted=not net.get("allowAll", False),
        allow_git_config=bool(sandbox.get("allowGitConfig", False)),
        allow_pty=bool(sandbox.get("allowPty", False)),
        allow_all_unix_sockets=bool(sandbox.get("allowAllUnixSockets", False)),
        enable_weaker_network_isolation=bool(sandbox.get("enableWeakerNetworkIsolation", False)),
        enable_weaker_nested_sandbox=bool(sandbox.get("enableWeakerNestedSandbox", False)),
        allow_apple_events=bool(sandbox.get("allowAppleEvents", False)),
    )


@dataclass
class SandboxDecision:
    """How one shell call is treated by the sandbox layer (R45)."""

    sandboxed: bool
    auto_allow: bool  # may skip whole-tool ask rules and prompts
    force_ask: bool  # dangerouslyDisableSandbox honoured -> always ask
    reason: str = ""


def sandbox_decision(command: str, input: dict[str, Any], sandbox: dict[str, Any]) -> SandboxDecision:
    """R45: ``autoAllowBashIfSandboxed`` (default true) lets sandboxed calls skip
    prompts; ``excludedCommands`` run unsandboxed (convenience only - permissions
    still apply); ``dangerouslyDisableSandbox`` is honoured only when
    ``allowUnsandboxedCommands`` is true and then always forces an ask."""
    if not sandbox.get("enabled", False):
        return SandboxDecision(False, False, False, "sandbox disabled")
    if input.get("dangerouslyDisableSandbox"):
        if sandbox.get("allowUnsandboxedCommands", False):
            return SandboxDecision(False, False, True, "model asked to run outside the sandbox")
        # flag not honoured: the call simply stays sandboxed
    base = command.strip().split(" ", 1)[0] if command.strip() else ""
    for pat in sandbox.get("excludedCommands", []) or []:
        if fnmatch.fnmatchcase(command.strip(), pat) or base == pat or command.strip().startswith(pat.rstrip("*").rstrip(":")):
            return SandboxDecision(False, False, False, f"excluded command {pat}")
    return SandboxDecision(True, bool(sandbox.get("autoAllowBashIfSandboxed", True)), False, "sandboxed")


# ------------------------------------------------------------------ network policy (§4.9)


def _host_matches(host: str, pattern: str) -> bool:
    h = host.lower().rstrip(".")
    p = pattern.lower().rstrip(".")
    if p.startswith("*."):
        return h.endswith(p[1:])  # subdomains only
    return h == p


def _matches_any(host: str, patterns: list[str]) -> bool:
    return any(_host_matches(host, p) for p in patterns)


@dataclass
class NetworkPolicy:
    allowed_domains: list[str] = field(default_factory=list)
    denied_domains: list[str] = field(default_factory=list)
    strict_allowlist: bool = False
    ask: Callable[[str], bool] | None = None

    def allow_host(self, host: str, call_allowed: list[str] | None = None) -> bool:
        """Deny list -> allow list -> strict -> per-call allowed domains -> ask
        callback (no callback = deny). Deny always wins (§4.9)."""
        if _matches_any(host, self.denied_domains):
            return False
        if _matches_any(host, self.allowed_domains):
            return True
        if self.strict_allowlist:
            return False
        if call_allowed and _matches_any(host, call_allowed):
            return True
        return bool(self.ask(host)) if self.ask is not None else False


# ------------------------------------------------------------------ credential masking (R44)


@dataclass
class CredentialMask:
    """Sentinel value inside the sandbox -> real secret substituted by the proxy only
    for configured hosts (R44)."""

    sentinels: dict[str, str] = field(default_factory=dict)  # sentinel -> env var name
    hosts: dict[str, list[str]] = field(default_factory=dict)  # env var -> allowed hosts
    mode: dict[str, str] = field(default_factory=dict)  # env var -> "mask" | "deny"

    def sandbox_env(self, real_env: dict[str, str]) -> dict[str, str]:
        """Environment handed to the sandbox: masked variables carry their
        sentinel, ``deny`` variables are removed entirely."""
        by_var = {v: s for s, v in self.sentinels.items()}
        out: dict[str, str] = {}
        for k, v in real_env.items():
            if self.mode.get(k) == "deny":
                continue
            out[k] = by_var.get(k, v)
        return out

    def substitute(self, host: str, text: str, real_env: dict[str, str]) -> str:
        """Proxy side: swap sentinels for real values only when ``host`` is
        allowed for that credential; elsewhere the sentinel leaks nothing."""
        for sentinel, var in self.sentinels.items():
            if sentinel in text and self.mode.get(var, "mask") == "mask" and _matches_any(host, self.hosts.get(var, [])):
                text = text.replace(sentinel, real_env.get(var, ""))
        return text


# ------------------------------------------------------------------ simulation


def _is_under(path: str, root: str) -> bool:
    p, r = os.path.normpath(path), os.path.normpath(root)
    return p == r or p.startswith(r.rstrip(os.sep) + os.sep)


class SimulatedSandbox:
    """In-process stand-in: decides whether a write/read path or host would be allowed."""

    def __init__(self, cfg: SandboxConfig, network: NetworkPolicy | None = None) -> None:
        self.cfg = cfg
        self.network = network or NetworkPolicy()
        self.violations: list[tuple[str, str]] = []  # (command, detail)

    def _protected(self, path: str) -> bool:
        parts = [c for c in os.path.normpath(path).split(os.sep) if c]
        for name in _protected_names(self.cfg.allow_git_config):
            seq = name.split("/")
            for i in range(len(parts) - len(seq) + 1):
                if parts[i : i + len(seq)] == seq:
                    return True
        return any(_is_under(path, p) for p in self.cfg.deny_within_allow)

    def _violate(self, command: str, detail: str) -> bool:
        # attribution goes through the same tag round trip the real watcher uses (R46)
        self.violations.append((decode_command_tag(f"{detail} # {tag_command(command)}") or command, detail))
        return False

    def check_write(self, command: str, path: str) -> bool:
        if not any(_is_under(path, r) for r in self.cfg.allow_write):
            return self._violate(command, f"file-write-create {path}")
        if self._protected(path):
            return self._violate(command, f"file-write protected {path}")
        return True

    def check_read(self, command: str, path: str) -> bool:
        if any(_is_under(path, r) for r in self.cfg.deny_read):
            return self._violate(command, f"file-read-data {path}")
        return True

    def check_connect(self, command: str, host: str, call_allowed: list[str] | None = None) -> bool:
        if not self.cfg.network_restricted:
            return True
        if self.network.allow_host(host, call_allowed):
            return True
        return self._violate(command, f"network-outbound {host}")
