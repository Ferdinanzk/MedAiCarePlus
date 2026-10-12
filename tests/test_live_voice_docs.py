"""Docs for live voice check-ins, Phase 2a (docs/superpowers/specs/2026-10-12-live-voice-2a-design.md).

The live voice sections of CLAUDE.md, reachy_app/README.md and HANDOFF.md may name only what the code has. Every span
in backticks there is checked against the code (app/, sql/init.sql, the robot app, frontend_source/src and
.env.example; files under a tests folder don't count):
- `NAME = value`, or `module.NAME = value`: every definition of NAME (in module.py) has exactly that value;
- a URL appears in the code as written; a route /api/<group>/... is declared in app/routers/api_<group>.py;
- a file path exists;
- a dotted name a.b: a.py, class a or SQL table a has b;
- any other code-like name (with an underscore, camelCase, or 4+ capital letters) is a whole word in the code;
- a number of 4 or more digits (a close code) appears in app/.
Plain words (start, flush, marin), dashed words (gpt-live-1, branch names) and commands run on the robot (`curl ...`)
are not checked.
"""
import functools
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent

SECTIONS = {   # doc -> (its live voice heading, line starts that end the section outside code blocks)
    "CLAUDE.md": ("### Live voice check-ins (Phase 2a, test)", ("## ", "### ")),
    "reachy_app/README.md": ("## Live voice check-ins (0.8.0, test)", ("## ",)),
    "HANDOFF.md": ("## 6. Phase 2a: live voice check-ins (test only)", ("# ", "## ", "---")),
}
CODE_GLOBS = ("app/**/*.py", "sql/init.sql", "reachy_app/medcare_reachy/**/*.py", "reachy_app/tools/*.py",
              "reachy_app/pyproject.toml", "frontend_source/src/**/*.ts", "frontend_source/src/**/*.tsx",
              ".env.example")
PATH_ROOTS = ("", "app", "reachy_app", "reachy_app/medcare_reachy", "frontend_source", "frontend_source/src")
NOT_NAMES = {"GET", "POST", "PUT", "PATCH", "DELETE", "JSON", "HTTP", "PCM16"}
SERVER_SENDS = ("ready", "flush", "say", "fallback", "end")
ROBOT_SENDS = ("start", "end", "playing", "idle")
CARD_WORDS = ("What should Reachy call you?", "Reachy 要怎麼稱呼您？", "Live voice (test)", "即時語音（測試）")

SPAN = re.compile(r"`([^`\n]+)`")
CONSTANT = re.compile(r"(?:([a-z_][a-z0-9_]*)\.)?([A-Z][A-Z0-9_]+) = (\S+)")
URL = re.compile(r"\b(?:wss?|https?)://[^\s`]+")
ROUTE = re.compile(r"/api/[\w{}/.-]+")
PATH = re.compile(r"[\w./-]*\w\.(?:py|tsx?|sql|md|toml|json)\b")
TOKEN = re.compile(r'(?:"user"|[A-Za-z_][A-Za-z0-9_]*)(?:\.[A-Za-z_][A-Za-z0-9_]*)*')


def _section(doc: str) -> str:
    heading, stops = SECTIONS[doc]
    lines = (REPO / doc).read_text(encoding="utf-8").splitlines()
    if heading not in lines:
        pytest.fail(f"{doc} has no line {heading!r}")
    body, fenced = [], False
    for line in lines[lines.index(heading) + 1:]:
        if line.startswith("```"):
            fenced = not fenced
        elif not fenced and line.startswith(stops):
            break
        body.append(line)
    return "\n".join(body)


@functools.lru_cache(maxsize=None)
def _code() -> dict:
    files = {path for pattern in CODE_GLOBS for path in REPO.glob(pattern)
             if "tests" not in path.relative_to(REPO).parts}
    return {path: path.read_text(encoding="utf-8", errors="replace") for path in sorted(files)}


def _word(name: str) -> re.Pattern:
    return re.compile(rf"(?<![A-Za-z0-9_]){re.escape(name)}(?![A-Za-z0-9_])")


def _has(name: str, paths=None) -> bool:
    pattern = _word(name)
    return any(pattern.search(text) for path, text in _code().items() if paths is None or path in paths)


def _sql_has(table: str, column: str) -> bool:
    sql = (REPO / "sql" / "init.sql").read_text(encoding="utf-8")
    name = re.escape(table)
    created = re.search(rf"^CREATE TABLE IF NOT EXISTS {name} \((.*?)^\);", sql, re.S | re.M)
    if created and _word(column).search(created.group(1)):
        return True
    return any(_word(column).search(line) for line in sql.splitlines() if re.match(rf"ALTER TABLE {name}(?!\w)", line))


def _route_declared(route: str) -> bool:
    group, _, rest = route[len("/api/"):].partition("/")
    router = REPO / "app" / "routers" / f"api_{group}.py"
    if not rest or not router.exists():
        return False
    pieces = re.split(r"\{[^}]*\}", "/" + rest.rstrip("/."))
    pattern = '"' + r"\{\w+\}".join(re.escape(piece) for piece in pieces) + '"'
    return bool(re.search(pattern, router.read_text(encoding="utf-8")))


def _path_exists(path: str) -> bool:
    if any((REPO / root / path).is_file() for root in PATH_ROOTS):
        return True
    return "/" not in path and any(code.name == path for code in _code())


def _value_matches(module, name: str, value: str) -> bool:
    definition = re.compile(rf"^[ \t]*{name}[ \t]*(?::[^=\n]*)?=(?!=)[ \t]*([^\s#]+)", re.M)
    found = [match for path, text in _code().items() if module is None or path.name == f"{module}.py"
             for match in definition.findall(text)]
    return bool(found) and all(match == value for match in found)


def _code_like(name: str) -> bool:
    if name in NOT_NAMES:
        return False
    capitals = name.isupper() and sum(char.isalpha() for char in name) >= 4
    return "_" in name or bool(re.search(r"[a-z][A-Z]", name)) or capitals


def _name_missing(token: str) -> bool:
    owner, _, rest = token.partition(".")
    if not rest:
        return _code_like(token) and not _has(token)
    member = rest.split(".")[0]
    if owner == '"user"':
        return not _sql_has('"user"', member)
    modules = [path for path in _code() if path.name == f"{owner}.py"]
    classes = [path for path, text in _code().items() if re.search(rf"^\s*class {re.escape(owner)}\b", text, re.M)]
    return not (_has(member, modules + classes) or _sql_has(owner, member))


def _problems(span: str) -> list:
    span = span.strip()
    if span.startswith(("curl ", "ssh ")):
        return []
    constant = CONSTANT.fullmatch(span)
    if constant:
        return [] if _value_matches(*constant.groups()) else [f"constant {span}"]
    if re.fullmatch(r"\d{4,}", span):
        in_app = any(_word(span).search(text) for path, text in _code().items()
                     if path.relative_to(REPO).parts[0] == "app")
        return [] if in_app else [f"number {span}"]
    problems = [f"URL {url}" for url in URL.findall(span) if not any(url in text for text in _code().values())]
    span = URL.sub(" ", span)
    problems += [f"route {route}" for route in ROUTE.findall(span) if not _route_declared(route)]
    span = ROUTE.sub(" ", span)
    problems += [f"file {path}" for path in PATH.findall(span) if not _path_exists(path)]
    span = PATH.sub(" ", span)
    return problems + [f"name {token}" for token in TOKEN.findall(span) if _name_missing(token)]


@pytest.mark.parametrize("doc", SECTIONS)
def test_each_doc_has_its_live_voice_section(doc):
    assert len(_section(doc).strip().splitlines()) >= 5


@pytest.mark.parametrize("doc", SECTIONS)
def test_every_code_name_in_the_section_is_in_the_code(doc):
    problems = sorted({problem for span in SPAN.findall(_section(doc)) for problem in _problems(span)})
    assert problems == [], f"{doc}: its live voice section names what the code lacks: {problems}"


def test_the_docs_name_each_live_message_and_both_ends_use_it():
    section = _section("CLAUDE.md")
    relay = (REPO / "app" / "services" / "live_session.py").read_text(encoding="utf-8")
    robot = (REPO / "reachy_app" / "medcare_reachy" / "bridge" / "live.py").read_text(encoding="utf-8")
    for kind in SERVER_SENDS + ROBOT_SENDS:
        assert f"`{kind}`" in section, f"CLAUDE.md's live voice section doesn't name the message {kind}"
        assert f'"{kind}"' in relay, f"app/services/live_session.py has no message {kind}"
        assert f'"{kind}"' in robot, f"reachy_app/medcare_reachy/bridge/live.py has no message {kind}"


def test_the_docs_quote_the_cards_words_as_the_web_app_says_them():
    section = _section("CLAUDE.md")
    web = "\n".join(text for path, text in _code().items() if path.suffix in (".ts", ".tsx"))
    for words in CARD_WORDS:
        assert words in section, f"CLAUDE.md's live voice section doesn't quote {words}"
        assert words in web, f"frontend_source/src has no {words}"


def test_robot_app_0_9_0_in_its_code():
    pyproject = (REPO / "reachy_app" / "pyproject.toml").read_text(encoding="utf-8")
    assert re.search(r'^version = "0\.9\.0"$', pyproject, re.M)
    init = (REPO / "reachy_app" / "medcare_reachy" / "bridge" / "__init__.py").read_text(encoding="utf-8")
    assert '__version__ = "0.9.0"' in init
    # The README's 0.9.0 entry comes with the companion session's docs (the docs task replaces this test).
    assert "- **0.8.0**" in (REPO / "reachy_app" / "README.md").read_text(encoding="utf-8")


def test_env_example_says_how_to_test_live_voice_and_that_patients_wait_for_2b():
    lines = (REPO / ".env.example").read_text(encoding="utf-8").splitlines()
    comment = []
    for line in reversed(lines[:lines.index("LIVE_VOICE_TEST=0")]):
        if not line.startswith("# ") or line.startswith("# REACHY_"):
            break
        comment.insert(0, line[2:])
    text = " ".join(comment)
    for words in ("for testing only", "Phase 2b", "docker compose up -d app", "Check", "Live voice (test)"):
        assert words in text, f".env.example's comment above LIVE_VOICE_TEST=0 doesn't say {words!r}"


@pytest.mark.parametrize("doc, stale", [
    ("CLAUDE.md", "(their own spec after a measurement spike; not built)"),
    ("CLAUDE.md", "The opening line gets a name only from the patient's own entry."),
    ("HANDOFF.md", "the spec is next"),
    ("reachy_app/README.md", "No microphone in this version."),
])
def test_stale_sentences_are_gone(doc, stale):
    assert stale not in (REPO / doc).read_text(encoding="utf-8")


def test_claude_md_lists_live_voice_test_and_its_port_rule_covers_websockets():
    text = (REPO / "CLAUDE.md").read_text(encoding="utf-8")
    assert re.search(r"^\| `LIVE_VOICE_TEST` \|.+\| `0` \|$", text, re.M)
    port_rule = next(line for line in text.splitlines() if "returns 404 for device routes on 8000" in line)
    assert "WebSocket" in port_rule


def test_the_docs_name_the_access_recheck_and_the_endings_it_adds():
    section = _section("CLAUDE.md")
    for name in ("ACCESS_RECHECK_SECONDS = 10.0", "ACCESS_CHECK_TIMEOUT = 5.0", "ACCESS_FAILURES_TO_END = 3",
                 "access_withdrawn", "access_unverified", "memory_withdrawn", "DB_TIMEOUT_SECONDS = 5.0",
                 "SILENCE_END_SECONDS = 45", "CHECKIN_SILENCE = None"):
        assert name in section, f"CLAUDE.md's live voice section doesn't name {name}"
