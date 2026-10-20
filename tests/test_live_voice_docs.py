"""Docs for live voice check-ins, Phase 2a (docs/superpowers/specs/2026-10-12-live-voice-2a-design.md), and the
companion session of robot app 0.9.0 (docs/superpowers/specs/2026-10-12-companion-session-design.md).

The live voice sections of CLAUDE.md, reachy_app/README.md and HANDOFF.md, CLAUDE.md's "How it ends" bullet and the
README's companion section may name only what the code has. Every span in backticks there is checked against the code
(app/, sql/init.sql, the robot app, frontend_source/src and .env.example; files under a tests folder don't count):
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
COMPANION_PARTS = {   # doc -> (the first line of its companion text, line starts that end it outside code blocks)
    "CLAUDE.md": ("- **How it ends (robot app 0.9.0, the companion session):**", ("- ", "#")),
    "reachy_app/README.md": ("## Companion session (0.9.0)", ("## ",)),
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


def _lines_from(doc: str, first: str, stops: tuple) -> list:
    """doc's lines from the first one that starts with `first` up to the next line outside code blocks that starts
    with one of `stops`."""
    lines = (REPO / doc).read_text(encoding="utf-8").splitlines()
    start = next((index for index, line in enumerate(lines) if line.startswith(first)), None)
    if start is None:
        pytest.fail(f"{doc} has no line starting {first!r}")
    body, fenced = [lines[start]], False
    for line in lines[start + 1:]:
        if line.startswith("```"):
            fenced = not fenced
        elif not fenced and line.startswith(stops):
            break
        body.append(line)
    return body


def _section(doc: str) -> str:
    heading, stops = SECTIONS[doc]
    return "\n".join(_lines_from(doc, heading, stops)[1:])


def _companion_part(doc: str) -> str:
    return "\n".join(_lines_from(doc, *COMPANION_PARTS[doc]))


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


@pytest.mark.parametrize("doc", COMPANION_PARTS)
def test_every_code_name_in_the_companion_text_is_in_the_code(doc):
    problems = sorted({problem for span in SPAN.findall(_companion_part(doc)) for problem in _problems(span)})
    assert problems == [], f"{doc}: its companion session text names what the code lacks: {problems}"


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


def test_robot_app_0_10_0_in_its_code_and_its_readme():
    """0.10.0 merges 0.9.0 (the companion session) with the multimodal-emotion line (0.8.0-0.9.1 on the space)."""
    pyproject = (REPO / "reachy_app" / "pyproject.toml").read_text(encoding="utf-8")
    assert re.search(r'^version = "0\.10\.0"$', pyproject, re.M)
    init = (REPO / "reachy_app" / "medcare_reachy" / "bridge" / "__init__.py").read_text(encoding="utf-8")
    assert '__version__ = "0.10.0"' in init
    readme = (REPO / "reachy_app" / "README.md").read_text(encoding="utf-8")
    assert "- **0.10.0**" in readme and "- **0.9.0**" in readme and "- **0.8.0**" in readme


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


def test_cloud_speech_to_text_has_a_release_gate_and_the_older_bullets_name_it():
    """Quick fixes 2 sends the patient's audio to OpenAI, which the microphone switch and the robot notice say never
    happens (review of 19 Oct 2026): its bullet and the Phase 2b list carry a release gate, and the older bullets on
    the "I finished" phrase and the dose lines name the cloud step and meal_check."""
    quick_fixes_2 = "\n".join(_lines_from("CLAUDE.md", "- **Quick fixes 2", ("- ", "#")))
    assert "**Release gate:**" in quick_fixes_2 and "a new robot notice version and a consent scope" in quick_fixes_2
    lines = (REPO / "CLAUDE.md").read_text(encoding="utf-8").splitlines()
    phase_2b = next(line for line in lines if line.startswith("- **Not in 2a (Phase 2b"))
    assert "cloud speech-to-text" in phase_2b
    finished = next(line for line in lines if line.startswith('- The robot app can listen for the patient\'s "I'))
    assert "`POST /api/device/stt`" in finished
    dose_lines = next(line for line in lines if line.startswith("  - **Dose lines with the name.**"))
    assert "`meal_check`" in dose_lines


def test_quick_fixes_3_names_the_empty_stomach_rule_and_meal_not_yet():
    """Quick fixes 3 of 19 Oct 2026: no meal question for a medicine taken before a meal or on an empty stomach, and
    the line Reachy says when the patient has not eaten yet. The dose lines bullet names it too."""
    quick_fixes_3 = "\n".join(_lines_from("CLAUDE.md", "- **Quick fixes 3", ("- ", "#")))
    for name in ("`before_meals`", "空腹", "飯前", "餐前", "`meal_not_yet`", "CONTRACT3.md"):
        assert name in quick_fixes_3, f"CLAUDE.md's quick fixes 3 bullet doesn't name {name}"
    lines = (REPO / "CLAUDE.md").read_text(encoding="utf-8").splitlines()
    dose_lines = next(line for line in lines if line.startswith("  - **Dose lines with the name.**"))
    assert "`meal_not_yet`" in dose_lines


def test_quick_fix_4_names_the_meal_retry_and_the_quiet_missed_dose_job():
    """Quick fix 4 of 20 Oct 2026: Reachy comes back 20 minutes after "not eaten yet", and the missed-dose job waits
    for it. The quick fixes 3 bullet no longer says nothing waits for the meal."""
    quick_fix_4 = "\n".join(_lines_from("CLAUDE.md", "- **Quick fix 4", ("- ", "#")))
    for name in ("`MEAL_RETRY_MINUTES = 20`", "`MEAL_RETRY_LIMIT = 3`", "`not_before`", "`missed_retry`",
                 "`reachy_tasks.meal_retry_waiting_sql`", "`nothing_to_prompt`", "CONTRACT4.md"):
        assert name in quick_fix_4, f"CLAUDE.md's quick fix 4 bullet doesn't name {name}"
    quick_fixes_3 = "\n".join(_lines_from("CLAUDE.md", "- **Quick fixes 3", ("- ", "#")))
    assert "nothing waits for the meal" not in quick_fixes_3


def test_quick_fix_4_review_names_when_the_hold_ends_and_the_open_mixed_slot_question():
    """Review of quick fix 4: the hold ends once Reachy is with the patient or while the robot is offline, the missed
    mark reads it again, an expired dose cancels the meal retry, and "not yet" again moves it. A slot that mixes an
    after-meal dose with an empty-stomach one is an open question for the user (the robot gives meal_not_eaten to
    every dose still to prompt), not "only the ones not eaten for are retried"."""
    quick_fix_4 = "\n".join(_lines_from("CLAUDE.md", "- **Quick fix 4", ("- ", "#")))
    for name in ("`in_progress`", "`task_due`", "`OFFLINE_AFTER_SECONDS`", "`RETURNING`", "`dose_expired`",
                 "\"not yet\" again", "the user decides"):
        assert name in quick_fix_4, f"CLAUDE.md's quick fix 4 bullet doesn't name {name}"
    assert "only the ones not eaten for are retried" not in quick_fix_4


def test_quick_fix_4_re_review_holds_the_dose_while_reachy_prompts_it():
    """Re-review of quick fix 4: a meal retry with the patient (in_progress) holds its dose too, so the job sends no
    second visit and no missed mark while Reachy prompts it, except in the companion chat after the dose, which the
    job reads from the robot's latest heartbeat (status_detail's companion flag)."""
    quick_fix_4 = "\n".join(_lines_from("CLAUDE.md", "- **Quick fix 4", ("- ", "#")))
    for name in ("`reachy_device.status_detail`", "`companion`", "`in_progress`", "`task_due`"):
        assert name in quick_fix_4, f"CLAUDE.md's quick fix 4 bullet doesn't name {name}"
    assert "and also (review of quick fix 4) once Reachy is with the patient" not in quick_fix_4


def test_the_companion_flag_names_its_task():
    """Review of quick fixes 4 and 5: the companion chat releases only its own meal retry's hold, read from the task
    the heartbeat names (status_detail's companion_task), so a flag left from another task's chat releases nothing."""
    quick_fix_4 = "\n".join(_lines_from("CLAUDE.md", "- **Quick fix 4", ("- ", "#")))
    assert "`companion_task`" in quick_fix_4, "CLAUDE.md's quick fix 4 bullet doesn't name `companion_task`"


def test_quick_fix_5_names_the_meal_retry_switch():
    """Quick fix 5 of 20 Oct 2026: the Reachy card's switch for "come back 20 minutes after 'not eaten'", off by
    default. The quick fix 4 bullet says the meal retry is queued only while it is on."""
    quick_fix_5 = "\n".join(_lines_from("CLAUDE.md", "- **Quick fix 5", ("- ", "#")))
    for name in ('`"user".reachy_meal_retry`', "`meal_retry`", "`/api/reachy/status`", "`PATCH /api/reachy/settings`",
                 "`reachy_tasks.cancel_meal_retries`", "`meal_retry_off`", "還沒吃飯時，20 分鐘後再來提醒",
                 "Come back 20 minutes after 'not eaten yet'", "CONTRACT5.md"):
        assert name in quick_fix_5, f"CLAUDE.md's quick fix 5 bullet doesn't name {name}"
    quick_fix_4 = "\n".join(_lines_from("CLAUDE.md", "- **Quick fix 4", ("- ", "#")))
    assert "only while the patient's switch is on" in quick_fix_4


def test_quick_fix_6_names_the_demo_dose_button():
    """Quick fix 6 of 20 Oct 2026: the Reachy card's "Demo dose: Acetal 500mg" button and its route, after quick fix 5
    in the live voice section."""
    quick_fix_6 = "\n".join(_lines_from("CLAUDE.md", "- **Quick fix 6", ("- ", "#")))
    for name in ("`POST /api/reachy/demo-dose`", "示範藥物：Acetal 500mg", "Demo dose: Acetal 500mg", "`demo_reset`",
                 "`robot_not_paired`", "`robot_consent_required`", "`slot_busy`", "`dose_safety.is_ad_hoc`",
                 "`reachy_tasks.enqueue_reachy_task`", "CONTRACT6.md"):
        assert name in quick_fix_6, f"CLAUDE.md's quick fix 6 bullet doesn't name {name}"
    section = _section("CLAUDE.md")
    assert section.index("- **Quick fix 5") < section.index("- **Quick fix 6") < section.index("- **Not in 2a")
