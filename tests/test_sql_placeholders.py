"""
Two things in db.py are safe only because of what the codebase happens not to
contain. Both are documented in their own comments; neither had a guard, so
both were trip-wires nobody would hear go off.

1. Connection.execute() translates every '?' to '%s' with a regex that is NOT
   quote-aware. A '?' inside a SQL string literal — a LIKE pattern, a CHECK
   default, an error message built in SQL — would be mistranslated into a
   parameter placeholder and desync the parameter list. The failure is a
   confusing psycopg error a long way from the cause.

2. run_script() splits a .sql file on ';' after stripping '--' comments. A
   semicolon inside a string literal, or a $$-quoted function body, would be
   split mid-statement. This runs at install and on every in-app update, so a
   break here is a database that half-exists.

Making the translator quote-aware and the splitter a real parser is more risk
than the problem warrants for the SQL this app actually writes. Pinning the
assumptions is not.
"""
import pathlib
import re

ROOT = pathlib.Path(__file__).parent.parent
SQL_FILES = sorted(ROOT.glob("*.sql"))
# routes/ too, since the blueprint split moved most SQL-bearing code there.
PY_FILES = sorted(ROOT.glob("*.py")) + sorted((ROOT / "routes").glob("*.py"))


def _sql_string_literals(text):
    """Single-quoted SQL literals, with '' escapes handled."""
    return re.findall(r"'((?:[^']|'')*)'", text)


def test_no_sql_string_literal_contains_a_question_mark():
    """GUARD on db.py's _PLACEHOLDER_RE, which substitutes every '?'
    unconditionally."""
    offenders = []
    for path in SQL_FILES:
        text = "\n".join(line.split("--")[0] for line in path.read_text(encoding="utf-8").splitlines())
        for lit in _sql_string_literals(text):
            if "?" in lit:
                offenders.append(f"{path.name}: '{lit[:50]}'")
    assert not offenders, (
        "a '?' inside a SQL string literal will be rewritten as a parameter "
        "placeholder by db.Connection.execute():\n  " + "\n  ".join(offenders))


def test_the_schema_has_no_dollar_quoted_bodies():
    """GUARD on run_script()'s split. A $$...$$ function or DO block contains
    semicolons that are not statement terminators."""
    offenders = [p.name for p in SQL_FILES if "$$" in p.read_text(encoding="utf-8")]
    assert not offenders, (
        f"dollar-quoted block(s) in {offenders} — db.run_script() splits on ';' "
        f"and would cut the body in half")


def test_the_schema_has_no_semicolon_inside_a_string_literal():
    """GUARD on the same split, for the more likely case: a default value or a
    CHECK message containing a semicolon."""
    offenders = []
    for path in SQL_FILES:
        text = "\n".join(line.split("--")[0] for line in path.read_text(encoding="utf-8").splitlines())
        for lit in _sql_string_literals(text):
            if ";" in lit:
                offenders.append(f"{path.name}: '{lit[:50]}'")
    assert not offenders, (
        "a ';' inside a SQL string literal would be treated as a statement "
        "terminator by db.run_script():\n  " + "\n  ".join(offenders))


def test_the_guards_are_looking_at_real_files():
    """CONTROL. Every assertion above passes vacuously against an empty file
    list — which is exactly how a path typo would present."""
    assert SQL_FILES, "no .sql files found; the glob above is wrong"
    assert any("CREATE TABLE" in p.read_text(encoding="utf-8") for p in SQL_FILES)
    assert PY_FILES, "no .py files found"


def test_the_literal_parser_actually_finds_literals():
    """CONTROL for _sql_string_literals(). If it silently returned nothing,
    the two literal-scanning guards above would pass against anything."""
    assert _sql_string_literals("SELECT 'abc', 'd''e' FROM t") == ["abc", "d''e"]
    assert _sql_string_literals("a ';' b") == [";"]
    assert _sql_string_literals("no literals here") == []
