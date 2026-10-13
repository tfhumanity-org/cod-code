"""WP3 [PSEUDO] sandbox profile tests (spec 04 R41-R46, §4.8-§4.9; 04#T10).
Profiles are generated as text only - nothing is executed."""

from __future__ import annotations

import os

import pytest

from harnesslab.pseudo.sandbox_profiles import (
    CredentialMask,
    NetworkPolicy,
    SandboxConfig,
    SandboxUnavailable,
    SimulatedSandbox,
    darwin_profile,
    decode_command_tag,
    linux_bwrap_args,
    sandbox_decision,
    sandbox_wrap,
    scan_protected,
    tag_command,
)


def test_command_tag_round_trip():
    for cmd in ["ls", "echo 'a b' && rm -rf x_END", "üñí € $(x)"]:
        tag = tag_command(cmd)
        assert tag.startswith("CMD64_") and tag.endswith("_END")
        assert decode_command_tag(f"Sandbox: deny file-write ... {tag} trailing") == cmd
    assert decode_command_tag("no tag here") is None
    assert decode_command_tag("CMD64_!!!_END") is None


def test_scan_protected_finds_root_and_nested(tmp_path):
    (tmp_path / "sub" / ".git" / "hooks").mkdir(parents=True)
    (tmp_path / "sub" / ".bashrc").write_text("")
    found = scan_protected([str(tmp_path)])
    assert str(tmp_path / ".gitconfig") in found  # listed even if absent: creation is denied too
    assert str(tmp_path / "sub" / ".git" / "hooks") in found
    assert str(tmp_path / "sub" / ".bashrc") in found
    assert str(tmp_path / ".git" / "config") in found
    assert str(tmp_path / ".git" / "config") not in scan_protected([str(tmp_path)], allow_git_config=True)


def test_darwin_profile_contents(tmp_path):
    root = str(tmp_path)
    cfg = SandboxConfig(allow_write=[root], deny_read=["/Users/me/.ssh"], log_tag="tag-123")
    protected = scan_protected([root])
    prof = darwin_profile("make", cfg, protected)
    lines = prof.splitlines()
    assert lines[0] == "(version 1)"
    assert '(deny default (with message "tag-123"))' in lines
    assert f'(allow file-write* (subpath "{root}"))' in lines
    assert '(deny file-read* (subpath "/Users/me/.ssh"))' in lines
    assert f'(deny file-write* (literal "{root}/.bashrc"))' in lines
    assert f'(deny file-write* (subpath "{root}/.git/hooks"))' in lines
    assert f'(deny file-write-unlink (literal "{root}/.git"))' in lines  # parent of a protected path
    assert f'(deny file-write-unlink (literal "{root}"))' in lines
    assert '(allow network-outbound (remote ip "localhost:3128"))' in lines
    assert '(allow network-outbound (remote ip "localhost:1080"))' in lines
    assert "(allow network*)" not in lines
    open_net = darwin_profile("make", SandboxConfig(allow_write=[root], network_restricted=False), [])
    assert "(allow network*)" in open_net.splitlines()
    assert "(allow pseudo-tty)" in darwin_profile("x", SandboxConfig(allow_pty=True), [])


def test_darwin_wrap_argv_and_proxy_env(tmp_path):
    argv, env = sandbox_wrap("make test", SandboxConfig(allow_write=[str(tmp_path)]), platform="darwin")
    assert argv[:2] == ["/usr/bin/sandbox-exec", "-p"] and argv[3:5] == ["bash", "-c"]
    assert decode_command_tag(argv[5]) == "make test" and argv[5].startswith("make test # CMD64_")
    assert env["HTTP_PROXY"] == "http://localhost:3128" and env["ALL_PROXY"].startswith("socks5")


def test_linux_bwrap_args(tmp_path):
    root = str(tmp_path)
    (tmp_path / ".bashrc").write_text("")
    args, env = linux_bwrap_args("make", SandboxConfig(allow_write=[root], deny_read=["/secret"]), scan_protected([root]))
    assert args[:3] == ["bwrap", "--new-session", "--die-with-parent"]
    assert "--unshare-net" in args and "--proc" in args
    i = args.index("--bind", args.index("--ro-bind"))
    assert args[i : i + 3] == ["--bind", root, root]
    j = args.index(f"{root}/.bashrc")
    assert args[j - 1] == "--ro-bind"  # protected overlay
    assert ["--tmpfs", "/secret"] == args[args.index("--tmpfs") : args.index("--tmpfs") + 2]
    sep = args.index("--")
    assert args[sep + 1 : sep + 3] == ["apply-seccomp", "--block-af-unix"]
    assert "socat TCP-LISTEN:3128" in args[-1] and decode_command_tag(args[-1]) == "make"
    assert env == {"HTTP_PROXY": "http://localhost:3128", "HTTPS_PROXY": "http://localhost:3128", "ALL_PROXY": "socks5://localhost:1080"}
    args2, env2 = linux_bwrap_args("ls", SandboxConfig(network_restricted=False, allow_all_unix_sockets=True, enable_weaker_nested_sandbox=True), [])
    assert "--unshare-net" not in args2 and "apply-seccomp" not in args2 and env2 == {}
    assert args2[args2.index("--bind") : args2.index("--bind") + 3] == ["--bind", "/proc", "/proc"]


def test_unsupported_platform():
    with pytest.raises(SandboxUnavailable):
        sandbox_wrap("ls", SandboxConfig(), platform="win32")


def test_network_policy_order():
    asked: list[str] = []
    pol = NetworkPolicy(allowed_domains=["*.github.com", "pypi.org"], denied_domains=["evil.github.com"], ask=lambda h: asked.append(h) or h == "ok.dev")
    assert not pol.allow_host("evil.github.com")  # deny beats allow
    assert pol.allow_host("api.github.com")
    assert not pol.allow_host("github.com")  # "*.x" means subdomains only -> falls through to ask
    asked.clear()
    assert pol.allow_host("PyPI.org.")
    assert pol.allow_host("per-call.dev", call_allowed=["per-call.dev"]) and asked == []
    assert pol.allow_host("ok.dev") and not pol.allow_host("nope.dev")
    assert asked == ["ok.dev", "nope.dev"]
    strict = NetworkPolicy(allowed_domains=["pypi.org"], strict_allowlist=True, ask=lambda h: True)
    assert not strict.allow_host("other.org", call_allowed=["other.org"])
    assert not NetworkPolicy().allow_host("x.org")  # no callback: deny


def test_credential_masking():
    mask = CredentialMask(
        sentinels={"__SENTINEL_GH__": "GH_TOKEN"},
        hosts={"GH_TOKEN": ["api.github.com"]},
        mode={"GH_TOKEN": "mask", "AWS_SECRET": "deny"},
    )
    real = {"GH_TOKEN": "ghp_real", "AWS_SECRET": "s3cr3t", "PATH": "/bin"}
    env = mask.sandbox_env(real)
    assert env == {"GH_TOKEN": "__SENTINEL_GH__", "PATH": "/bin"}
    assert mask.substitute("api.github.com", "Bearer __SENTINEL_GH__", real) == "Bearer ghp_real"
    assert mask.substitute("evil.com", "Bearer __SENTINEL_GH__", real) == "Bearer __SENTINEL_GH__"


def test_simulated_sandbox_decisions_and_attribution(tmp_path):
    root = str(tmp_path)
    sb = SimulatedSandbox(SandboxConfig(allow_write=[root], deny_read=["/secrets"]), NetworkPolicy(allowed_domains=["pypi.org"]))
    assert sb.check_write("make", os.path.join(root, "out", "a.o"))
    assert not sb.check_write("echo x > ~/.bashrc", "/home/me/.bashrc")
    assert not sb.check_write("cp hook .git/hooks/", os.path.join(root, ".git", "hooks", "pre-commit"))
    assert not sb.check_write("git config", os.path.join(root, ".git", "config"))
    assert SimulatedSandbox(SandboxConfig(allow_write=[root], allow_git_config=True)).check_write("git config", os.path.join(root, ".git", "config"))
    assert not sb.check_read("cat /secrets/key", "/secrets/key") and sb.check_read("cat a", "/etc/hosts")
    assert sb.check_connect("pip install x", "pypi.org") and not sb.check_connect("curl evil", "evil.com")
    assert [c for c, _ in sb.violations] == ["echo x > ~/.bashrc", "cp hook .git/hooks/", "git config", "cat /secrets/key", "curl evil"]
    assert SimulatedSandbox(SandboxConfig(network_restricted=False)).check_connect("curl", "anything.com")


def test_sandbox_decision_rules():
    off = sandbox_decision("make", {}, {})
    assert not off.sandboxed and not off.auto_allow
    on = sandbox_decision("make", {}, {"enabled": True})
    assert on.sandboxed and on.auto_allow
    assert not sandbox_decision("make", {}, {"enabled": True, "autoAllowBashIfSandboxed": False}).auto_allow
    ex = sandbox_decision("docker build .", {}, {"enabled": True, "excludedCommands": ["docker"]})
    assert not ex.sandboxed and not ex.auto_allow
    ignored = sandbox_decision("make", {"dangerouslyDisableSandbox": True}, {"enabled": True})
    assert ignored.sandboxed and not ignored.force_ask  # flag not honoured without allowUnsandboxedCommands
    honoured = sandbox_decision("make", {"dangerouslyDisableSandbox": True}, {"enabled": True, "allowUnsandboxedCommands": True})
    assert not honoured.sandboxed and honoured.force_ask
