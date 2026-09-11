# -*- coding: utf-8 -*-
"""Every `%(name)s` in a msgid must have a matching keyword in the call.

Jinja's gettext ALWAYS runs `rv % variables`, even when the call passed no
keyword arguments at all. A msgid that names a placeholder the call does not
supply therefore raises `KeyError` **while rendering the page** — not at
import, not in a linter, but as a 500 the first time someone opens that page.

This shipped as six 500s per app, in English as well as Arabic, and it came
from a reasonable-looking idea: leave `%(stock)s` in the string for JavaScript
to substitute later with `.replace()`. Placeholders JavaScript fills must be
written `{stock}`, which carries no `%` and passes through untouched;
`%(name)s` means "Jinja fills this, here, now".

Static rather than a page-render test because a render test only covers the
pages it happens to visit, in the states it happens to reach — this reads
every call in every template.
"""
import re
from pathlib import Path

TEMPLATES = Path(__file__).resolve().parents[1] / "templates"
LITERAL = re.compile(r"\s*(?:'((?:[^'\\]|\\.)*)'|\"((?:[^\"\\]|\\.)*)\")", re.S)


def _gettext_calls(src):
    """(offset, argument-text) for each `_(` ... `)`, brackets and quotes aware."""
    for m in re.finditer(r"\b_\(", src):
        i, depth, quote = m.end(), 1, None
        while i < len(src) and depth:
            c = src[i]
            if quote:
                if c == "\\":
                    i += 2
                    continue
                if c == quote:
                    quote = None
            elif c in "'\"":
                quote = c
            elif c == "(":
                depth += 1
            elif c == ")":
                depth -= 1
            i += 1
        yield m.start(), src[m.end():i - 1]


def _split(args):
    """The msgid (adjacent literals are one string) and the rest of the call."""
    m = LITERAL.match(args)
    if not m:
        return None, ""
    msgid = m.group(1) or m.group(2) or ""
    rest = args[m.end():]
    while True:
        c = LITERAL.match(rest)
        if not c:
            break
        msgid += c.group(1) or c.group(2) or ""
        rest = rest[c.end():]
    return msgid, rest


def _offenders():
    out = []
    for f in sorted(TEMPLATES.rglob("*.html")):
        src = f.read_text(encoding="utf-8")
        for pos, args in _gettext_calls(src):
            msgid, rest = _split(args)
            if msgid is None:
                continue
            needed = set(re.findall(r"%\((\w+)\)s", msgid))
            supplied = set(re.findall(r"(\w+)\s*=", rest))
            missing = needed - supplied
            if missing:
                line = src[:pos].count("\n") + 1
                out.append(f"{f.name}:{line}: {sorted(missing)} in {msgid[:60]!r}")
    return out


def test_every_placeholder_has_an_argument():
    offenders = _offenders()
    assert not offenders, (
        "These gettext calls name a placeholder they do not supply — each is a "
        "KeyError when the page renders:\n  " + "\n  ".join(offenders)
        + "\nIf JavaScript fills it, write it {name}, not %(name)s."
    )


def test_control_the_check_reads_real_calls():
    """Without a count this passes just as happily against zero calls."""
    total = sum(1 for f in TEMPLATES.rglob("*.html")
                for _ in _gettext_calls(f.read_text(encoding="utf-8")))
    assert total > 500, f"only {total} gettext calls found — the scanner is not reading"


def test_control_it_can_tell_the_two_apart():
    supplied, _ = _split("'Total (%(currency_label)s)', currency_label=currency_label()")
    assert set(re.findall(r"%\((\w+)\)s", supplied)) == {"currency_label"}
    msgid, rest = _split("'Only %(stock)s in stock.'")
    assert set(re.findall(r"%\((\w+)\)s", msgid)) - set(re.findall(r"(\w+)\s*=", rest)) \
        == {"stock"}, "the check would not have caught the bug it was written for"
