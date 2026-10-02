"""Hardware-free, network-free fakes for the slot state machine."""

import asyncio
import copy
from datetime import datetime, timedelta, timezone

from medcare_reachy.bridge.app_client import SessionLost
from medcare_reachy.bridge.session import SlotSession

BASE_WALL = datetime(2026, 10, 1, 0, 0, tzinfo=timezone.utc)


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def monotonic(self):
        return self.t

    def wall(self):
        return BASE_WALL + timedelta(seconds=self.t - 1000.0)


class FakeRobot:
    def __init__(self, reachable=True):
        self.reachable = reachable
        self.calls = []

    def is_reachable(self):
        self.calls.append("is_reachable")
        return self.reachable

    def wake(self):
        self.calls.append("wake")

    def sleep(self):
        self.calls.append("sleep")

    def hold_head(self):
        self.calls.append("hold_head")

    def look_around(self, step):
        self.calls.append(f"look_around:{step}")


class FakeClips:
    def __init__(self):
        self.played = []

    def play(self, clip_id):
        self.played.append(clip_id)
        return True

    def play_med_prompt(self, med_id):
        self.played.append(f"med_prompt:{med_id}")
        return True


class FakeVoice:
    """DoneListener stand-in: tests call say() to make the patient say they finished."""

    def __init__(self, clock, available=True):
        self.clock, self.available = clock, available
        self.active = False
        self.mode = "done"
        self.activity = []
        self.holds = 0
        self.max_holds = 0
        self.heard = None
        self.utterances = []

    def set_active(self, active, mode="done"):
        if active != self.active:
            self.activity.append(active)
        self.active, self.mode = active, mode

    def hold(self):
        self.holds += 1
        self.max_holds = max(self.max_holds, self.holds)

    def release(self):
        self.holds -= 1

    def say(self, word="吃完"):
        assert self.active and self.holds == 0, "the listener is off"
        self.heard = (self.clock.monotonic(), word)

    def speak(self, text):
        """Chat mode: the patient says something."""
        assert self.active and self.mode == "chat" and self.holds == 0, "not listening for a conversation"
        self.utterances.append(text)

    def take_utterance(self):
        return self.utterances.pop(0) if self.utterances else None

    def heard_since(self, since):
        return self.heard[1] if self.heard and self.heard[0] >= since else None


class FakeSpeaker:
    def __init__(self, available=True):
        self.available = available
        self.said = []

    def say(self, text):
        self.said.append(text)
        return True


class FakeStream:
    def __init__(self):
        self.attached = None
        self.latest = None
        self.errors = []
        self.history = []

    def attach(self, monitor):
        self.attached = monitor
        self.latest = None
        self.history.append(("attach", monitor["session_id"]))

    def detach(self):
        if self.attached:
            self.history.append(("detach", self.attached["session_id"]))
        self.attached = None
        self.latest = None

    def take_error(self):
        return self.errors.pop(0) if self.errors else None


class FakeApp:
    """Records every call; `fail` maps a method name to a list of exceptions raised on successive calls."""

    def __init__(self, task, clock):
        self.task = copy.deepcopy(task)
        self.clock = clock
        self.current_task = copy.deepcopy(task)
        self.calls = []
        self.fail = {}
        self.unreachable_since = None
        self.sessions = []

    def _maybe_fail(self, name):
        queue = self.fail.get(name)
        if queue:
            exc = queue.pop(0)
            if exc is not None:
                raise exc

    def dose(self, intk_id):
        return next(d for d in self.current_task["doses"] if d["intk_id"] == intk_id)

    async def tasks_current(self):
        self.calls.append(("tasks_current",))
        self._maybe_fail("tasks_current")
        return copy.deepcopy(self.current_task)

    async def task_status(self, task_id, status, detail=None):
        self.calls.append(("task_status", status, detail))
        self._maybe_fail("task_status")
        self.current_task["status"] = status
        return {"status": status}

    async def confirmation(self, task_id, intk_id, source, evidence):
        self.calls.append(("confirmation", intk_id, source, evidence))
        self._maybe_fail("confirmation")
        self.dose(intk_id)["intake_stats"] = "pending_confirmation"
        return {"confirmation_id": "c1"}

    async def extra_event(self, task_id, event_id, decision, confidence):
        self.calls.append(("extra_event", event_id, decision, confidence))
        self._maybe_fail("extra_event")
        return {"ok": True}

    async def monitor_start(self, mode, intk_id, task_id):
        self.calls.append(("monitor_start", mode, intk_id))
        self._maybe_fail("monitor_start")
        n = len(self.sessions) + 1
        session = {"session_id": f"s{n}", "generation": f"g{n}", "mode": mode, "intk_id": intk_id,
                   "identity_status": "searching"}
        self.sessions.append(session)
        return dict(session)

    async def conversation_start(self, task_id, language):
        self.calls.append(("conversation_start", task_id, language))
        self._maybe_fail("conversation_start")
        return {"conversation_id": "c1", "reply": "今天感覺怎麼樣？", "speech_text": "今天感觉怎么样？", "end": False}

    async def conversation_turn(self, conversation_id, text):
        self.calls.append(("conversation_turn", conversation_id, text))
        self._maybe_fail("conversation_turn")
        answer = self.replies.pop(0) if getattr(self, "replies", None) else {}
        return {"reply": "真好", "speech_text": "真好", "end": False, "risk": False, **answer}

    async def conversation_end(self, conversation_id, reason):
        self.calls.append(("conversation_end", conversation_id, reason))
        self._maybe_fail("conversation_end")
        return {"ended": True}

    async def monitor_end(self, session_id, generation):
        self.calls.append(("monitor_end", session_id))
        self._maybe_fail("monitor_end")
        return {"success": True}

    def named(self, name):
        return [call for call in self.calls if call[0] == name]


def dose(intk_id, med_id=None, stats="pending", supported=True):
    return {"intk_id": intk_id, "med_id": med_id or intk_id * 10, "med_name": f"Med{intk_id}",
            "pill_description": None, "dose_form": "solid_oral" if supported else "liquid",
            "units_per_dose": 1, "intake_stats": stats, "supported": supported}


def make_task(doses, *, status="leased", reason="upcoming", auto_record=True, expires_in=3600, microphone=False,
              checkin=False):
    return {"task_id": "task-1", "slot_time": "2026-10-01T00:00:00+00:00", "reason": reason, "attempt": 1,
            "status": status, "expires_at": (BASE_WALL + timedelta(seconds=expires_in)).isoformat(),
            "patient_name": "Amy", "auto_record": auto_record, "microphone": microphone, "checkin": checkin,
            "doses": doses}


class Harness:
    def __init__(self, task, robot=None, voice=False, speaker=False):
        self.clock = FakeClock()
        self.app = FakeApp(task, self.clock)
        self.robot = robot or FakeRobot()
        self.clips = FakeClips()
        self.stream = FakeStream()
        self.voice = FakeVoice(self.clock) if voice else None
        self.speaker = FakeSpeaker() if speaker else None
        self.slot = SlotSession(copy.deepcopy(task), app=self.app, robot=self.robot, clips=self.clips,
                                stream=self.stream, voice=self.voice, speaker=self.speaker, clock=self.clock)

    def tick(self, advance=0.0):
        self.clock.t += advance
        asyncio.run(self.slot.tick())

    def see(self, **state):
        """Pretend the attached monitor session answered with this public() state."""
        assert self.stream.attached, "no monitor session attached"
        self.stream.latest = {"session_id": self.stream.attached["session_id"], **state}

    def run_until(self, predicate, step=0.5, limit=2000):
        for _ in range(limit):
            if predicate():
                return
            self.tick(step)
        raise AssertionError(f"condition not reached; state={self.slot.state} done={self.slot.done}")

    def to_state(self, state, **kw):
        self.run_until(lambda: self.slot.state == state, **kw)

    def find_patient(self):
        self.to_state("SEARCHING")
        self.tick()
        self.see(identity_status="verified")
        self.tick()

    def verify_and_prompt(self):
        """In MED_PROMPT: the dose session verifies the patient, the prompt plays."""
        self.tick()   # starts the dose session
        self.see(identity_status="verified")
        self.tick()
        assert self.slot.state == "WATCHING"

    def statuses(self):
        return [call[1] for call in self.app.named("task_status")]


def lost(detail="Session is no longer active"):
    return SessionLost(status=409, detail=detail)
