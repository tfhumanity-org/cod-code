"""WP2 Edit matching and the Edit tool: 03 R27-R33, §4.3 (03#T6), R17-R20 via the tool."""

from __future__ import annotations

import os

import pytest

from harnesslab.permissions.types import PermissionMode
from harnesslab.tools.base import ToolErrorCode
from harnesslab.tools.builtin.edit import EditTool
from harnesslab.tools.edit_match import (
    apply_edit_text,
    apply_multi_edit,
    desanitize,
    find_actual,
    fold_quotes,
    restyle_new,
)
from harnesslab.tools.ledger import ReadLedger
from harnesslab.tools.pipeline import run_tool_use

from tests.wp2_fakes import allow_all, block, call_ctx_for

# ----------------------------------------------------------- pure matching


def test_exact_match():
    r = apply_edit_text("a = 1\nb = 2\n", "b = 2", "b = 3")
    assert r.ok and r.content == "a = 1\nb = 3\n" and r.replacements == 1
    assert not r.used_quote_folding


def test_curly_quote_folding_returns_file_substring_and_recurls():
    file = "print(\u201chello\u201d)\nit\u2019s fine\n"
    assert fold_quotes(file) == 'print("hello")\nit\'s fine\n'
    assert find_actual(file, 'print("hello")') == "print(\u201chello\u201d)"
    r = apply_edit_text(file, 'print("hello")', 'print("bye", "x")')
    assert r.ok and r.used_quote_folding
    assert "print(\u201cbye\u201d, \u201cx\u201d)" in r.content
    r2 = apply_edit_text(file, "it's fine", "don't 'panic'")
    assert r2.ok
    assert "don\u2019t \u2018panic\u2019" in r2.content  # apostrophe between letters; opening after space


def test_recurl_only_when_file_style_differs():
    assert restyle_new('say "x"', 'say "x"', 'say "x"') == 'say "x"'


def test_escape_matching_both_directions():
    # model wrote a real character, file spells it as an escape
    file = 'msg = "caf\\u00e9"\n'
    assert find_actual(file, 'msg = "café"') == 'msg = "caf\\u00e9"'
    r = apply_edit_text(file, 'msg = "café"', 'msg = "crème"')
    assert r.ok and r.content == 'msg = "cr\\u00e8me"\n' and r.used_escape_matching
    # upper-case hex style is kept
    r_up = apply_edit_text('s = "\\u00E9t\\u00E9"\n', 's = "été"', 's = "ç"')
    assert r_up.content == 's = "\\u00E7"\n'
    # model wrote an escape, file has the real character
    r2 = apply_edit_text('x = "é"\n', 'x = "\\u00e9"', 'x = "\\u00e8"')
    assert r2.ok and r2.content == 'x = "è"\n'


def test_escape_matching_respects_backslash_parity():
    file = 'raw = "\\\\u00e9"\n'  # an escaped backslash followed by literal "u00e9"
    assert find_actual(file, '"\\é"') is None


def test_desanitize_retry_applies_to_new_too():
    file = "<function_results>\nok\n</function_results>\n"
    assert desanitize("<fnr>") == "<function_results>"
    r = apply_edit_text(file, "<fnr>\nok", "<fnr>\nfine")
    assert r.ok and r.content == "<function_results>\nfine\n</function_results>\n"


def test_trailing_whitespace_stripped_except_markdown():
    r = apply_edit_text("a\n", "a", "b   \nc\t\n")
    assert r.content == "b\nc\n\n"
    md = apply_edit_text("a\n", "a", "line  ", is_markdown=True)
    assert md.content == "line  \n"


def test_deleting_a_line_removes_its_newline():
    r = apply_edit_text("keep\ndrop me\nkeep2\n", "drop me", "")
    assert r.content == "keep\nkeep2\n"


def test_not_found_quotes_string():
    r = apply_edit_text("abc", "zzz", "y")
    assert not r.ok and r.error_code == ToolErrorCode.NOT_FOUND_IN_FILE and "zzz" in r.message
    r2 = apply_edit_text("abc", "é", "y")
    assert "escapes" in r2.message


def test_multiple_matches_code_9_and_replace_all():
    r = apply_edit_text("x x x", "x", "y")
    assert not r.ok and r.error_code == 9 and "Found 3 matches" in r.message
    r2 = apply_edit_text("x x x", "x", "y", replace_all=True)
    assert r2.ok and r2.content == "y y y" and r2.replacements == 3


def test_identical_strings_rejected():
    r = apply_edit_text("abc", "a", "a")
    assert r.error_code == ToolErrorCode.NO_CHANGE


def test_multi_edit_rules():
    ok = apply_multi_edit("a b c", [("a", "A", False), ("c", "C", False)])
    assert ok.ok and ok.content == "A b C" and ok.replacements == 2
    clash = apply_multi_edit("a b", [("a", "xy", False), ("xy", "z", False)])
    assert not clash.ok and "earlier edit" in clash.message
    noop = apply_multi_edit("ab", [("a", "b", False), ("bb", "ab", False)])  # ends where it began
    assert not noop.ok and noop.error_code == ToolErrorCode.NO_CHANGE
    failing = apply_multi_edit("a", [("q", "r", False)])
    assert failing.message.startswith("Edit 1:")


# --------------------------------------------------------------- the tool


@pytest.fixture
def ectx(ctx):
    ctx.can_use_tool = allow_all
    ctx.ledger = ReadLedger()
    return ctx


def read_into_ledger(ctx, path):
    from harnesslab.tools.builtin.read import read_text_file

    ctx.ledger.record_read(str(path), read_text_file(str(path)).text, offset=None, limit=None, partial=False)


def run_edit(ctx, **inp):
    o = run_tool_use(block("Edit", "t1", **inp), EditTool(), call_ctx_for(ctx))
    return o, o.result.tool_results()[0].text()


def test_edit_requires_read_first(ectx, project):
    p = project / "a.py"
    p.write_text("x = 1\n")
    o, t = run_edit(ectx, file_path=str(p), old_string="x = 1", new_string="x = 2")
    assert o.error_code == ToolErrorCode.NOT_READ_FIRST and "Read" in t
    read_into_ledger(ectx, p)
    o, t = run_edit(ectx, file_path=str(p), old_string="x = 1", new_string="x = 2")
    assert p.read_text() == "x = 2\n"
    assert "1 replacement" in t and "no need to Read it again" in t
    assert str(p) in ectx.touched_paths


def test_edit_coerces_other_field_names(ectx, project):
    p = project / "a.py"
    p.write_text("x = 1\n")
    read_into_ledger(ectx, p)
    o, t = run_edit(ectx, path=str(p), old_str="x = 1", new_str="x = 9")
    assert p.read_text() == "x = 9\n" and "'old_str' was treated as 'old_string'" in t


def test_edit_preserves_bom_and_crlf(ectx, project):
    p = project / "win.txt"
    p.write_bytes(b"\xef\xbb\xbfone\r\ntwo\r\n")
    read_into_ledger(ectx, p)
    run_edit(ectx, file_path=str(p), old_string="one\ntwo", new_string="uno\ndos")
    assert p.read_bytes() == b"\xef\xbb\xbfuno\r\ndos\r\n"


def test_edit_preserves_utf16le(ectx, project):
    p = project / "u16.txt"
    p.write_bytes(b"\xff\xfe" + "héllo\n".encode("utf-16-le"))
    read_into_ledger(ectx, p)
    run_edit(ectx, file_path=str(p), old_string="héllo", new_string="hola")
    assert p.read_bytes() == b"\xff\xfe" + "hola\n".encode("utf-16-le")


def test_edit_refuses_notebooks_and_multiple_matches(ectx, project):
    nb = project / "n.ipynb"
    nb.write_text("{}")
    o, _ = run_edit(ectx, file_path=str(nb), old_string="{", new_string="[")
    assert o.error_code == ToolErrorCode.NOTEBOOK_REQUIRES_NOTEBOOK_EDIT
    p = project / "m.py"
    p.write_text("a\na\n")
    read_into_ledger(ectx, p)
    o, t = run_edit(ectx, file_path=str(p), old_string="a", new_string="b")
    assert o.error_code == ToolErrorCode.MULTIPLE_MATCHES and "Found 2 matches" in t


def test_edit_creates_file_with_empty_old_string(ectx, project):
    p = project / "sub" / "new.py"
    o, t = run_edit(ectx, file_path=str(p), old_string="", new_string="print(1)\n")
    assert p.read_text() == "print(1)\n" and "created" in t


def test_edit_stale_recovered_warns(ectx, project):
    p = project / "a.py"
    p.write_text("x = 1\ny = 2\n")
    read_into_ledger(ectx, p)
    p.write_text("x = 1\ny = 3\n")
    st = os.stat(p)
    os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))
    o, t = run_edit(ectx, file_path=str(p), old_string="x = 1", new_string="x = 0")
    assert o.error_code == ToolErrorCode.STALE_FILE  # not auto-allowed
    ectx.permissions.mode = PermissionMode.ACCEPT_EDITS
    o, t = run_edit(ectx, file_path=str(p), old_string="x = 1", new_string="x = 0")
    assert p.read_text() == "x = 0\ny = 3\n"
    assert "changes you have not seen" in t


def test_edit_calls_file_history_first(ectx, project):
    calls = []

    class FH:
        def before_edit(self, path):
            calls.append((path, open(path).read()))

    ectx.file_history = FH()
    p = project / "a.py"
    p.write_text("old\n")
    read_into_ledger(ectx, p)
    run_edit(ectx, file_path=str(p), old_string="old", new_string="new")
    assert calls == [(str(p), "old\n")]


def test_edit_classifier_projection():
    proj = EditTool().classifier_projection({"file_path": "/a.py", "old_string": "x\ny", "new_string": "z"})
    assert proj == "/a.py\n- x\n- y\n+ z"
