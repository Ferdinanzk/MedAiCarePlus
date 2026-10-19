"""Any sentence the server sends the robot as text, in the live voice (services/speech_clips.py, quick fixes of 19 Oct
2026): the key (a random token per issue) and the gate, the background render (one try, 10 s), the help line from
its kept copy, the in-memory LRU with its TTL, the route that serves only keys issued to the device's patient, and the
`clip` on the text chat's lines and on a refusal's 409."""

import asyncio
import logging
import sys
import threading
import types

import pytest

_asyncpg_stub = types.ModuleType("asyncpg")
_asyncpg_stub.Pool = object
sys.modules.setdefault("asyncpg", _asyncpg_stub)

from app import config   # noqa: E402
from app.services import ai_settings, conversation, dose_safety, med_clips, speech_clips, voice_lines  # noqa: E402
from tests import test_conversation, test_device_api   # noqa: E402
from tests.test_med_clips import _streamed_wav   # noqa: E402

env = test_device_api.env       # the device API harness (dose routes)
robot = test_conversation.robot  # the text chat over one event loop, so background renders finish
TARGET = ai_settings.LiveTarget("sk-x", "gpt-live-1", "marin")
REPLY = "真好，您走了多久呢？"
COMPANION = voice_lines.INSTRUCTIONS["companion"]


@pytest.fixture
def speech(monkeypatch):
    """A live target (marin) and the speech API faked: `renders` holds (text, voice, api_key, instructions, timeout,
    retries) per call; `fail` is the error the next render raises; `gate`, when set, holds the render until set."""
    w = types.SimpleNamespace(renders=[], target=TARGET, fail=None, gate=None)

    def render(text, voice, api_key, instructions=med_clips.INSTRUCTIONS, timeout=med_clips.TIMEOUT,
               retries=med_clips.RETRIES):
        w.renders.append((text, voice, api_key, instructions, timeout, retries))
        if w.gate is not None:
            w.gate.wait(5)
        if w.fail is not None:
            raise w.fail
        return _streamed_wav(b"\x10\x27" * 2400, rate=24000)

    monkeypatch.setattr(med_clips, "render", render)
    monkeypatch.setattr(ai_settings, "live_target", lambda snapshot=None: w.target)
    speech_clips._clips.clear()
    speech_clips._tokens.clear()
    yield w
    speech_clips._clips.clear()
    speech_clips._tokens.clear()


def test_the_key_in_the_url_is_a_random_token_per_issue_so_a_logged_path_names_no_words(speech):
    """uvicorn logs the device port's paths: the words' own key (line_key, the same in every process) would tell
    anyone reading the logs which sentence a patient's robot fetched, the help line included."""
    async def scenario():
        first, second = speech_clips.issue(7, REPLY, "zh-TW"), speech_clips.issue(7, REPLY, "zh-TW")
        return first, second, await speech_clips.get(7, first["key"]), await speech_clips.get(7, second["key"])

    first, second, one, two = asyncio.run(scenario())
    content_key = voice_lines.line_key(REPLY, "marin", COMPANION)
    assert first["id"] == second["id"] == "speech" and first["key"] != second["key"]
    assert all(len(clip["key"]) == 20 and int(clip["key"], 16) >= 0 and clip["key"] != content_key
               for clip in (first, second))
    assert one == two and one[:4] == b"RIFF" and len(speech.renders) == 1      # one render under the words' key
    assert list(speech_clips._clips) == [content_key]


def test_no_clip_without_a_live_voice_target_for_no_words_or_a_long_text(speech):
    async def scenario():
        speech.target = None
        assert speech_clips.issue(7, REPLY, "zh-TW") is None
        speech.target = ai_settings.LiveTarget("", "gpt-live-1", "marin")   # no key
        assert speech_clips.issue(7, REPLY, "zh-TW") is None
        speech.target = TARGET
        assert speech_clips.issue(7, "", "zh-TW") is None
        assert speech_clips.issue(7, "長" * 241, "zh-TW") is None           # over 240 characters
        assert speech_clips.issue(7, "長" * 240, "zh-TW") is not None
        await asyncio.sleep(0.05)

    asyncio.run(scenario())
    assert len(speech.renders) == 1 and speech_clips.MAX_CHARS == 240


def test_the_render_starts_at_once_with_one_try_of_10_s_and_the_patient_gets_the_robots_wav(speech):
    async def scenario():
        clip = speech_clips.issue(7, REPLY, "zh-TW")
        again = speech_clips.issue(7, REPLY, "zh-TW")          # the same words again: one render
        assert again["key"] != clip["key"]
        data = await speech_clips.get(7, clip["key"])
        return clip, data, await speech_clips.get(8, clip["key"]), await speech_clips.get(7, "0" * 20)

    clip, data, other, unknown = asyncio.run(scenario())
    assert speech.renders == [(REPLY, "marin", "sk-x", COMPANION, 10, 0)]
    assert data[:4] == b"RIFF" and voice_lines.RATE == 16000      # voice_lines.to_robot_wav: PCM16 mono 16 kHz
    assert other is None and unknown is None                       # never issued to that patient; never issued


def test_the_route_waits_at_most_8_s_for_a_render_still_running(speech, monkeypatch):
    assert speech_clips.WAIT_SECONDS == 8
    monkeypatch.setattr(speech_clips, "WAIT_SECONDS", 0.05)
    speech.gate = threading.Event()

    async def scenario():
        clip = speech_clips.issue(7, REPLY, "zh-TW")
        late = await speech_clips.get(7, clip["key"])     # still rendering: nothing this time
        speech.gate.set()
        for _ in range(100):
            await asyncio.sleep(0.01)
            if speech_clips._clips[speech_clips._tokens[clip["key"]].key].render.done():
                break
        return late, await speech_clips.get(7, clip["key"])   # the render went on in the background

    late, kept = asyncio.run(scenario())
    assert speech_clips.WAIT_SECONDS == 0.05 and late is None and kept[:4] == b"RIFF" and len(speech.renders) == 1


def test_a_failed_render_is_logged_without_the_words_or_the_key_and_a_new_issue_tries_again(speech, caplog):
    speech.fail = RuntimeError("speech API answered 503")

    async def scenario():
        clip = speech_clips.issue(7, REPLY, "zh-TW")
        with caplog.at_level(logging.WARNING):
            assert await speech_clips.get(7, clip["key"]) is None
        speech.fail = None
        again = speech_clips.issue(7, REPLY, "zh-TW")     # issued again: rendered again
        return clip, await speech_clips.get(7, again["key"])

    clip, data = asyncio.run(scenario())
    assert data[:4] == b"RIFF" and len(speech.renders) == 2
    assert "speech clip (zh-TW, 10 characters) could not be rendered (speech API answered 503)" in caplog.text
    assert REPLY not in caplog.text and clip["key"] not in caplog.text and "sk-x" not in caplog.text
    assert voice_lines.line_key(REPLY, "marin", COMPANION) not in caplog.text


def test_the_cache_keeps_64_clips_for_10_minutes(speech, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(speech_clips, "_now", lambda: clock[0])

    async def scenario():
        first = speech_clips.issue(7, "第0句。", "zh-TW")
        keys = [speech_clips.issue(7, f"第{i}句。", "zh-TW")["key"] for i in range(1, 64)]
        await asyncio.sleep(0.05)
        assert await speech_clips.get(7, first["key"]) is not None      # 64 kept
        speech_clips.issue(7, "第64句。", "zh-TW")                         # the 65th drops the least recently used
        assert await speech_clips.get(7, keys[0]) is None and await speech_clips.get(7, first["key"]) is not None
        clock[0] += speech_clips.TTL_SECONDS + 1                         # 10 minutes on: every clip is gone
        assert await speech_clips.get(7, first["key"]) is None and not speech_clips._clips
        assert not speech_clips._tokens                                  # and every key issued for them
        await asyncio.sleep(0.05)
        for _ in range(speech_clips.TOKENS_MAX + 1):                     # the keys are capped too
            speech_clips.issue(7, "同一句。", "zh-TW")
        assert len(speech_clips._tokens) == speech_clips.TOKENS_MAX
        await asyncio.sleep(0.05)

    asyncio.run(scenario())
    assert (speech_clips.CACHE_SIZE, speech_clips.TTL_SECONDS, speech_clips.TOKENS_MAX) == (64, 600, 256)


def _kept_helpline(language: str):
    return voice_lines.fixed_path("helpline", language, voice_lines.key_for(
        "helpline", conversation.HELPLINE[language], language, "marin"))


def test_the_help_line_is_the_kept_helpline_line_never_a_new_render(speech, monkeypatch, tmp_path):
    """A patient in crisis never waits for a render: the help line is voice_lines' kept `helpline` copy (rendered
    ahead at startup, with the calm help-line instructions and the numbers digit by digit), read from its file."""
    monkeypatch.setattr(config, "MED_CLIP_DIR", tmp_path)
    kept = _kept_helpline("en")
    kept.parent.mkdir(parents=True)
    kept.write_bytes(b"RIFFkept")

    async def scenario():
        clip = speech_clips.issue(7, conversation.HELPLINE["en"], "en")
        return clip, await speech_clips.get(7, clip["key"]), await speech_clips.get(8, clip["key"])

    clip, data, other = asyncio.run(scenario())
    assert data == b"RIFFkept" and speech.renders == [] and other is None
    assert clip["key"] not in kept.name                                 # the URL still names nothing


def test_a_help_line_not_kept_yet_is_rendered_as_the_helpline_line_and_kept(speech, monkeypatch, tmp_path):
    monkeypatch.setattr(config, "MED_CLIP_DIR", tmp_path)

    async def scenario():
        clip = speech_clips.issue(7, conversation.HELPLINE["zh-TW"], "zh-TW")
        return await speech_clips.get(7, clip["key"])

    data = asyncio.run(scenario())
    [(text, voice, api_key, instructions, *_)] = speech.renders
    assert "一一九" in text and "119" not in text and instructions == voice_lines.INSTRUCTIONS["helpline"]
    assert data[:4] == b"RIFF" and _kept_helpline("zh-TW").read_bytes() == data   # kept for the next time


# ── the route ──

def test_the_robot_fetches_an_issued_clip_and_anything_else_is_a_404(env, monkeypatch):
    asked = []

    async def get(u_id, key):
        asked.append((u_id, key))
        return b"RIFFwav" if key == "k-reply" else None

    monkeypatch.setattr(speech_clips, "get", get)
    response = env.robot.get("/api/device/clips/speech/k-reply?language=zh-TW")
    assert response.status_code == 200 and response.content == b"RIFFwav"
    assert response.headers["content-type"].startswith("audio/wav") and response.headers["etag"] == '"k-reply"'
    assert response.headers["cache-control"] == "private" and asked == [(7, "k-reply")]   # the device's own patient
    response = env.robot.get("/api/device/clips/speech/k-other")
    assert response.status_code == 404 and response.json() == {"detail": "No clip for this text"}
    assert env.browser.get("/api/device/clips/speech/k-reply").status_code == 404        # device port only


# ── where a clip is issued: the text chat's lines, a refusal's 409 ──

def test_the_text_chats_opening_and_reply_carry_a_clip_the_robot_can_fetch(robot, speech):
    opened = robot.client.post("/api/device/conversations", json={"task_id": test_conversation.TASK_ID}).json()
    assert opened["clip"]["id"] == "speech" and opened["clip"]["key"] in speech_clips._tokens
    reply = robot.client.post(f"/api/device/conversations/{opened['conversation_id']}/turn",
                              json={"text": "我今天去散步"}).json()
    assert reply["reply"] == REPLY and speech_clips._tokens[reply["clip"]["key"]].key == voice_lines.line_key(
        REPLY, "marin", COMPANION)
    response = robot.client.get(f"/api/device/clips/speech/{reply['clip']['key']}?language=zh-TW")
    assert response.status_code == 200 and response.content[:4] == b"RIFF"
    assert response.headers["etag"] == f'"{reply["clip"]["key"]}"'      # the token, not the words' key
    assert {text for text, *_ in speech.renders} == {opened["reply"], REPLY}


def test_a_risk_turn_in_the_text_chat_plays_the_kept_help_line(robot, speech, monkeypatch, tmp_path):
    monkeypatch.setattr(config, "MED_CLIP_DIR", tmp_path)
    kept = _kept_helpline("zh-TW")
    kept.parent.mkdir(parents=True)
    kept.write_bytes(b"RIFFhelp")
    robot.state["risk"] = "self_harm"
    opened = robot.client.post("/api/device/conversations", json={"task_id": test_conversation.TASK_ID}).json()
    reply = robot.client.post(f"/api/device/conversations/{opened['conversation_id']}/turn",
                              json={"text": "我今天去散步"}).json()
    assert reply["risk"] is True and reply["reply"] == conversation.HELPLINE["zh-TW"]
    response = robot.client.get(f"/api/device/clips/speech/{reply['clip']['key']}?language=zh-TW")
    assert response.status_code == 200 and response.content == b"RIFFhelp"
    assert {text for text, *_ in speech.renders} <= {opened["reply"]}    # the help line itself is never rendered


def test_the_text_chat_sends_no_clip_without_a_live_voice_target(robot, speech):
    speech.target = None
    opened = robot.client.post("/api/device/conversations", json={"task_id": test_conversation.TASK_ID}).json()
    assert "clip" not in opened and "speech_text" in opened


def test_a_refusal_on_the_device_port_carries_a_clip_of_its_spoken_sentence(env, speech, monkeypatch):
    issued = []

    def issue(u_id, text, language):
        issued.append((u_id, text, language))
        return {"id": "speech", "key": "k-refusal"}

    monkeypatch.setattr(speech_clips, "issue", issue)
    refused = {}

    async def create(c, *, u_id, task_id, intk_ids, source, evidence, started_at=None):
        from datetime import datetime, timedelta, timezone
        now = datetime.now(timezone.utc)
        refused["error"] = dose_safety.DoseTooSoon(last_taken_at=now - timedelta(minutes=20), gap=timedelta(hours=1),
                                                   intk_id=intk_ids[0], med_name="allegra", scheduled_time=now,
                                                   at=now, u_id=u_id)
        refused["error"].after_intake = True
        raise refused["error"]

    monkeypatch.setattr(sys.modules["app.services.dose_confirmation"], "create", create)
    response = env.robot.post(f"/api/device/tasks/{test_device_api.TASK_ID}/confirmation",
                              json={"intk_id": 101, "source": "patient_claim"})
    body = response.json()
    assert response.status_code == 409 and body["clip"] == {"id": "speech", "key": "k-refusal"}
    # the spoken sentence in Traditional (what speech_text is in Simplified), for the device's patient
    assert issued == [(7, dose_safety.reply_text(refused["error"], "zh-TW", spoken=True), "zh-TW")]
    assert body["speech_text"] == conversation.speech_text(issued[0][1], "zh-TW")


def test_only_a_device_routes_refusal_of_a_known_patient_carries_a_clip(monkeypatch):
    """The browser's refusals (the public port) never get one; nor does a refusal that names no patient."""
    import json
    from datetime import datetime, timezone

    from starlette.requests import Request

    from app import main
    from app.services import schedule
    issued = []
    monkeypatch.setattr(speech_clips, "issue", lambda *args: issued.append(args) or {"id": "speech", "key": "k"})

    def body(path, refused):
        request = Request({"type": "http", "path": path, "headers": [], "method": "POST", "query_string": b""})
        return json.loads(asyncio.run(main.dose_refused(request, refused)).body)

    due = datetime(2026, 9, 30, 12, tzinfo=timezone.utc)
    assert "clip" not in body("/api/medications/101/take", schedule.DoseNotDueYet(due, 101, u_id=7)) and not issued
    assert "clip" not in body("/api/device/monitor/start", schedule.DoseNotDueYet(due, 101)) and not issued
    assert body("/api/device/monitor/start", schedule.DoseNotDueYet(due, 101, u_id=7))["clip"] == {
        "id": "speech", "key": "k"}
    assert len(issued) == 1 and issued[0][0] == 7
