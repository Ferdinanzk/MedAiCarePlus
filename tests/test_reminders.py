"""Real reminders (services/reminders.py): the due moment from the server's clock, the sentence with the database's
medicine name, the timer that fires it, and where it goes (the live relay, a check-in task, LINE, undelivered)."""

import asyncio
import io
import math
import types
import wave
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from app import config
from app.services import reminders

TZ = ZoneInfo("Asia/Taipei")
NOW = datetime(2026, 10, 14, 14, 25, 0, tzinfo=TZ)


# ── parsing the moment ──

@pytest.mark.parametrize("text, hm", [("20:00", (20, 0)), ("8pm", (20, 0)), ("8 PM", (20, 0)), ("8:30 PM", (20, 30)),
                                      ("晚上8點", (20, 0)), ("下午3點半", (15, 30)), ("早上9點", (9, 0)), ("12 am", (0, 0)),
                                      ("12:00", (12, 0)), ("9", (9, 0)), ("21:05", (21, 5)), ("凌晨12點", (0, 0))])
def test_clock_times_in_both_languages(text, hm):
    assert reminders.parse_clock(text) == hm


@pytest.mark.parametrize("text", ["", "soon", "25:00", "8:60", "noonish"])
def test_bad_clock_times_are_refused(text):
    with pytest.raises(reminders.ReminderError, match="invalid_time"):
        reminders.parse_clock(text)


def test_relative_times_count_from_now():
    due, note = reminders.due_time(NOW, in_minutes=1)
    assert due == NOW + timedelta(minutes=1) and note is None
    due, _ = reminders.due_time(NOW, in_minutes=120)
    assert due == NOW + timedelta(hours=2)


def test_absolute_times_use_the_real_calendar_and_zone():
    due, note = reminders.due_time(NOW, at_time="20:00")
    assert due == datetime(2026, 10, 14, 20, 0, tzinfo=TZ) and note is None
    due, note = reminders.due_time(NOW, at_time="9:00")                 # already passed today: tomorrow's
    assert due == datetime(2026, 10, 15, 9, 0, tzinfo=TZ) and note == "tomorrow"
    due, note = reminders.due_time(NOW, at_time="09:00", day="tomorrow")
    assert due == datetime(2026, 10, 15, 9, 0, tzinfo=TZ) and note is None
    due, _ = reminders.due_time(NOW, at_time="08:00", day="2026-10-20")
    assert due == datetime(2026, 10, 20, 8, 0, tzinfo=TZ)


@pytest.mark.parametrize("kw, error", [({}, "no_time"), ({"in_minutes": 0}, "invalid_time"),
                                       ({"in_minutes": "soon"}, "invalid_time"), ({"in_minutes": 0.1}, "in_the_past"),
                                       ({"at_time": "08:00", "day": "yesterday"}, "invalid_date"),
                                       ({"at_time": "14:25", "day": "today"}, "in_the_past"),
                                       ({"in_minutes": 60 * 24 * 8}, "too_far"),
                                       ({"at_time": "08:00", "day": "2027-01-01"}, "too_far")])
def test_impossible_times_are_refused(kw, error):
    with pytest.raises(reminders.ReminderError, match=error):
        reminders.due_time(NOW, **kw)


def test_the_spoken_time_names_the_day_when_it_is_not_today(monkeypatch):
    monkeypatch.setattr(reminders, "now_local", lambda: NOW)
    assert reminders.spoken(NOW + timedelta(minutes=1), "zh-TW") == "下午2點26分"
    assert reminders.spoken(datetime(2026, 10, 15, 9, 0, tzinfo=TZ), "zh-TW") == "明天早上9點整"
    assert reminders.spoken(datetime(2026, 10, 15, 9, 0, tzinfo=TZ), "en") == "tomorrow 9:00 in the morning"
    assert reminders.spoken(datetime(2026, 10, 20, 20, 0, tzinfo=TZ), "zh-TW") == "10月20日晚上8點整"


# ── the sentence ──

def test_sentences_name_the_medicine_from_the_database_not_the_request():
    assert reminders.sentence("named_position", "zh-TW", name="Adapalene", position=1) == "時間到了，請吃第一種藥，Adapalene。"
    assert reminders.sentence("named_position", "en", name="Adapalene", position=1) == "It's time to take your first medicine, Adapalene."
    assert reminders.sentence("named", "en", name="Metformin") == "It's time to take your medicine, Metformin."
    assert reminders.sentence("medicine", "zh-TW") == "時間到了，請記得吃藥。"
    assert reminders.sentence("plain", "zh-TW", what="打電話給女兒。") == "提醒您：打電話給女兒。"
    assert reminders.sentence("plain", "en", what="call your daughter") == "A reminder: call your daughter."


# ── create: the medicine, the row, the timer ──

class Conn:
    def __init__(self, medicines, today=(), slot=()):
        self.medicines, self.today, self.slot, self.executed = medicines, list(today), list(slot), []

    async def fetch(self, query, *args):
        if "FROM medication WHERE u_id" in query:
            return [{"med_id": m, "med_name": n} for m, n in self.medicines]
        if "i.intk_id, i.intake_time_stamp FROM intake" in query:
            return self.today
        if "ANY($2::int[]) ORDER BY m.med_name" in query:
            return [row for row in self.slot if row["intk_id"] in args[1]]
        if "UPDATE reminder SET status = 'cancelled'" in query:
            return [{"reminder_id": "r-1"}]
        raise AssertionError(query)

    async def fetchrow(self, query, *args):
        raise AssertionError(query)

    async def fetchval(self, query, *args):
        raise AssertionError(query)

    async def execute(self, query, *args):
        self.executed.append((query, args))

    def transaction(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class Pool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        return self.conn


@pytest.fixture
def world(monkeypatch):
    w = types.SimpleNamespace(conn=Conn([(3, "Adapalene"), (4, "Metformin 500mg")],
                                        today=[{"intk_id": 11, "intake_time_stamp": NOW.replace(hour=20, minute=0)},
                                               {"intk_id": 12, "intake_time_stamp": NOW.replace(hour=20, minute=0)}],
                                        slot=[{"intk_id": 11, "med_id": 3, "med_name": "Adapalene"},
                                              {"intk_id": 12, "med_id": 4, "med_name": "Metformin 500mg"}]),
                              armed=[], rendered=[])
    monkeypatch.setattr(reminders, "get_pool", lambda: Pool(w.conn))
    monkeypatch.setattr(reminders, "now_local", lambda: NOW)
    monkeypatch.setattr(reminders, "arm", lambda row: w.armed.append(row))
    monkeypatch.setattr(reminders, "_render_ahead", lambda text, language: w.rendered.append(text))
    return w


def test_remind_me_to_take_my_medicine_in_one_minute(world):
    answer = asyncio.run(reminders.create(7, what="提醒我吃藥", in_minutes=1, language="zh-TW", conversation_id="c-1"))
    assert answer["status"] == "scheduled" and answer["in_seconds"] == 60 and answer["due_spoken"] == "下午2點26分"
    assert answer["text"] == "時間到了，請記得吃藥。" and answer["medication_name"] is None
    assert answer["due_at"] == "2026-10-14T14:26:00+08:00"
    query, args = world.conn.executed[0]
    assert query.startswith("INSERT INTO reminder") and args[1] == 7 and args[2] == answer["text"] and args[8] == NOW + timedelta(minutes=1)
    assert world.armed[0]["reminder_id"] == answer["reminder_id"] and world.rendered == [answer["text"]]


def test_my_first_medicine_is_the_first_of_the_slot_due_then_in_the_robots_order(world):
    answer = asyncio.run(reminders.create(7, position=1, at_time="20:00", language="en"))
    assert answer["status"] == "scheduled" and answer["medication_name"] == "Adapalene" and answer["position"] == 1
    assert answer["text"] == "It's time to take your first medicine, Adapalene."
    second = asyncio.run(reminders.create(7, position=2, at_time="20:00", language="zh-TW"))
    assert second["text"] == "時間到了，請吃第二種藥，Metformin 500mg。"
    third = asyncio.run(reminders.create(7, position=3, at_time="20:00", language="zh-TW"))
    assert third["text"] == "時間到了，請記得吃藥。" and third["note"] == "no_such_dose_in_that_slot"


def test_a_named_medicine_is_matched_against_the_patients_medicines(world):
    answer = asyncio.run(reminders.create(7, medication_name="metformin", in_minutes=10, language="en"))
    assert answer["medication_name"] == "Metformin 500mg" and answer["text"] == "It's time to take your medicine, Metformin 500mg."
    unknown = asyncio.run(reminders.create(7, medication_name="Amlodipine", in_minutes=10, language="en"))
    assert unknown == {"status": "medicine_not_found", "medicines": ["Adapalene", "Metformin 500mg"]}
    assert len(world.conn.executed) == 1   # nothing scheduled for the unknown name


def test_a_plain_reminder_needs_words_and_a_time(world):
    assert asyncio.run(reminders.create(7, what="", in_minutes=5)) == {"status": "error", "error": "nothing_to_remind"}
    assert asyncio.run(reminders.create(7, what="call my son")) == {"status": "error", "error": "no_time"}
    answer = asyncio.run(reminders.create(7, what="call my son", at_time="9:00", language="en"))
    assert answer["status"] == "scheduled" and answer["note"] == "tomorrow" and answer["text"] == "A reminder: call my son."


def test_cancel_stops_the_timer_and_marks_the_row(world):
    timer = asyncio.get_event_loop().create_future() if False else None
    reminders._timers.clear()

    async def scenario():
        stuck = asyncio.ensure_future(asyncio.sleep(3600))
        reminders._timers["r-1"] = stuck
        answer = await reminders.cancel(7)
        await asyncio.sleep(0)
        return answer, stuck.cancelled()

    answer, cancelled = asyncio.run(scenario())
    assert answer == {"status": "cancelled", "count": 1} and cancelled


# ── firing: the relay, the robot, LINE ──

class FireConn(Conn):
    def __init__(self, row):
        super().__init__([])
        self.row, self.marks, self.line = row, [], None

    async def fetchrow(self, query, *args):
        if "SET status = 'firing'" in query:
            return self.row if self.row["status"] == "pending" else None
        raise AssertionError(query)

    async def fetchval(self, query, *args):
        if 'line_id FROM "user"' in query:
            return self.line
        raise AssertionError(query)

    async def execute(self, query, *args):
        if "UPDATE reminder SET status = $2" in query:
            self.marks.append((args[1], args[2]))
        self.executed.append((query, args))


class Relay:
    def __init__(self, can=True, ok=True):
        self.can, self.ok, self.spoken = can, ok, []

    def can_speak(self):
        return self.can

    async def speak(self, text, pcm, *, reason="reminder", after_opening=False):
        self.spoken.append((text, pcm, reason))
        return self.ok


@pytest.fixture
def firing(monkeypatch):
    row = {"reminder_id": "r-1", "u_id": 7, "text": "時間到了，請吃第一種藥，Adapalene。", "language": "zh-TW",
           "due_at": NOW, "status": "pending"}
    w = types.SimpleNamespace(conn=FireConn(row), relay=None, checkin=None, lines=[])

    async def audio(text, language):
        return b"\x00\x01" * 160

    async def queue_checkin(reminder_id, u_id, text):
        return w.checkin

    async def line(reminder_id, u_id, text):
        w.lines.append(text)
        return bool(w.conn.line)

    monkeypatch.setattr(reminders, "get_pool", lambda: Pool(w.conn))
    monkeypatch.setattr(reminders, "audio", audio)
    monkeypatch.setattr(reminders, "find_relay", lambda u_id: w.relay)
    monkeypatch.setattr(reminders, "_queue_checkin", queue_checkin)
    monkeypatch.setattr(reminders, "_line", line)
    return w


def test_a_reminder_during_a_live_chat_is_said_by_its_relay(firing):
    firing.relay = Relay()
    asyncio.run(reminders.fire("r-1"))
    assert firing.relay.spoken == [("時間到了，請吃第一種藥，Adapalene。", b"\x00\x01" * 160, "reminder")]
    assert firing.conn.marks == [("fired", "live")]


def test_without_a_chat_the_robot_is_sent_as_a_checkin_task(firing):
    firing.checkin = "task-9"
    asyncio.run(reminders.fire("r-1"))
    assert firing.conn.marks == [] and firing.lines == []   # queued_robot: marked by _queue_checkin itself


def test_without_a_robot_the_patient_gets_a_line_text_else_it_is_undelivered(firing):
    firing.conn.line = "U123"
    asyncio.run(reminders.fire("r-1"))
    assert firing.lines == ["時間到了，請吃第一種藥，Adapalene。"] and firing.conn.marks == [("fired", "line")]
    firing.conn.marks.clear()
    firing.conn.line = None
    asyncio.run(reminders.fire("r-1"))
    assert firing.conn.marks == [("undelivered", None)]


def test_a_relay_that_cannot_speak_falls_through_to_the_robot(firing):
    firing.relay, firing.checkin = Relay(can=False), "task-9"
    asyncio.run(reminders.fire("r-1"))
    assert firing.relay.spoken == [] and firing.conn.marks == []


def test_a_reminder_fires_only_once(firing):
    firing.conn.row["status"] = "fired"
    firing.relay = Relay()
    asyncio.run(reminders.fire("r-1"))
    assert firing.relay.spoken == []


# ── the timer and the sweep ──

def test_the_timer_fires_at_the_due_moment_and_the_sweep_rearms_and_expires(monkeypatch):
    fired = []

    async def fire(reminder_id):
        fired.append(reminder_id)

    monkeypatch.setattr(reminders, "fire", fire)
    reminders._timers.clear()

    async def scenario():
        reminders.arm({"reminder_id": "soon", "due_at": datetime.now(timezone.utc) + timedelta(seconds=0.05)})
        reminders.arm({"reminder_id": "soon", "due_at": datetime.now(timezone.utc)})   # armed once
        assert len(reminders._timers) == 1
        await asyncio.sleep(0.2)
        return list(fired)

    assert asyncio.run(scenario()) == ["soon"]

    class SweepConn(Conn):
        def __init__(self):
            super().__init__([])
            self.marks = []

        async def fetch(self, query, *args):
            now = datetime.now(timezone.utc)
            return [{"reminder_id": "late", "u_id": 7, "text": "x", "due_at": now - timedelta(hours=1), "status": "pending", "fired_at": None},
                    {"reminder_id": "due", "u_id": 7, "text": "x", "due_at": now + timedelta(minutes=5), "status": "pending", "fired_at": None},
                    {"reminder_id": "stuck", "u_id": 7, "text": "x", "due_at": now, "status": "firing", "fired_at": now - timedelta(minutes=9)}]

        async def execute(self, query, *args):
            self.marks.append((args[1], args[2]))

    conn = SweepConn()
    armed = []
    monkeypatch.setattr(reminders, "get_pool", lambda: Pool(conn))
    monkeypatch.setattr(reminders, "arm", lambda row: armed.append(row["reminder_id"]))
    asyncio.run(reminders.sweep())
    assert armed == ["due"] and conn.marks == [("expired", None), ("undelivered", None)]


# ── the voice ──

def test_the_voice_is_resampled_to_the_robots_16_khz():
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(24000)
        w.writeframes(b"\x10\x00" * 24000)   # one second
    pcm = reminders.to_pcm16k(out.getvalue())
    assert len(pcm) == 16000 * 2
    stereo = io.BytesIO()
    with wave.open(stereo, "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"\x10\x00\x10\x00" * 1600)
    assert len(reminders.to_pcm16k(stereo.getvalue())) == 1600 * 2


def test_audio_needs_a_live_connection_and_caches_what_it_rendered(monkeypatch, tmp_path):
    from app.services import ai_settings, med_clips
    monkeypatch.setattr(config, "MED_CLIP_DIR", tmp_path)
    monkeypatch.setattr(ai_settings, "live_target", lambda snapshot=None: None)
    reminders._audio.clear()
    assert asyncio.run(reminders.audio("x", "zh-TW")) is None
    monkeypatch.setattr(ai_settings, "live_target", lambda snapshot=None: ai_settings.LiveTarget("sk", "gpt-live-1", "marin"))
    renders = []

    def render(text, voice, api_key):
        renders.append((text, voice, api_key))
        out = io.BytesIO()
        with wave.open(out, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(24000)
            w.writeframes(b"\x00\x00" * 2400)
        return out.getvalue()

    monkeypatch.setattr(med_clips, "render", render)
    first = asyncio.run(reminders.audio("時間到了", "zh-TW"))
    assert len(first) == 1600 * 2 and renders == [("時間到了", "marin", "sk")]
    assert asyncio.run(reminders.audio("時間到了", "zh-TW")) == first and len(renders) == 1   # cached
    reminders._audio.clear()
    assert asyncio.run(reminders.audio("時間到了", "zh-TW")) == first and len(renders) == 1   # from disk
