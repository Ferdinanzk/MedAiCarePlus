"""Post-chat work: summary, risk skip, the summary's risk backstop, fact storage under the lock, follow-up marking,
retries and the never-silent last attempt."""

import asyncio
import sys
import types
import uuid
from datetime import date, datetime, timezone

import pytest

_asyncpg_stub = types.ModuleType("asyncpg")
_asyncpg_stub.Pool = object
sys.modules.setdefault("asyncpg", _asyncpg_stub)

from app import config
from app.services import after_chat, consent_service, conversation, memory, outbox

CID = str(uuid.uuid4())
MEMORY_ON = {s: {"granted": True, "terms_version": config.TERMS_VERSION} for s in memory.MEMORY_SCOPES}
NOW = datetime(2026, 10, 3, 2, 0, tzinfo=timezone.utc)


class Conn:
    def __init__(self, store):
        self.s = store

    def transaction(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def fetchrow(self, query, *args):
        if "SET after_chat_attempts = after_chat_attempts + 1" in query:
            chat = self.s.chat
            if chat["after_chat_state"] in ("pending", "failed") and chat["after_chat_attempts"] < args[2]:
                assert "after_chat_state = 'pending'" in query   # until the attempt writes its own state
                chat.update(after_chat_attempts=chat["after_chat_attempts"] + 1, after_chat_state="pending")
                return dict(chat)
            return None
        if "SELECT ended_at, risk_flag FROM conversation" in query:
            assert "FOR SHARE" in query
            return {"ended_at": NOW, "risk_flag": self.s.chat["risk_flag"]} if not self.s.deleted_chat else None
        if "FROM patient_memory WHERE memory_id" in query:
            return {"subject": "amy_visit"}
        raise AssertionError(query)

    async def fetch(self, query, *args):
        if "FROM conversation_turn" in query:
            return self.s.turns
        if "DISTINCT ON (kind, subject)" in query:
            return [dict(f) for f in self.s.facts]
        if "FROM patient_memory_deleted" in query:
            return self.s.tombstones
        raise AssertionError(query)

    async def fetchval(self, query, *args):
        if "max(followed_up_at)" in query:
            return None
        if "count(*) FROM conversation" in query:
            return self.s.followup_tries
        if 'SELECT name FROM "user"' in query:
            return "Pearl"
        if "SET risk_flag = TRUE" in query and "AND NOT risk_flag RETURNING" in query:   # alert_family
            if self.s.chat["risk_flag"]:
                return None
            self.s.chat["risk_flag"] = True
            return args[0]
        if "after_chat_attempts >= $3 RETURNING after_chat_state" in query:   # a last attempt that never finished
            chat = self.s.chat
            if chat["after_chat_state"] != "pending" or chat["after_chat_attempts"] < args[2]:
                return None
            chat["after_chat_state"] = ("skipped" if chat["risk_flag"] else "done" if chat["mood"] is not None
                                        else "failed")
            chat["mood"] = chat["mood"] or "unknown"
            return chat["after_chat_state"]
        raise AssertionError(query)

    async def execute(self, query, *args):
        self.s.sql.append(query)
        if 'FROM "user" WHERE u_id = $1 FOR UPDATE' in query:
            return "SELECT 1"
        if "SET after_chat_state" in query:
            self.s.chat["after_chat_state"] = args[1]
            self.s.chat["after_chat_attempts"] -= args[2]      # refund on rate limit
        elif "SET summary" in query:
            if self.s.fail_save:
                raise RuntimeError("database went away")
            self.s.chat.update(summary=args[1], mood=args[2])
        elif "SET mood = 'unknown'" in query:
            self.s.chat["mood"] = self.s.chat["mood"] or "unknown"
        elif "INSERT INTO notification" in query:
            if self.s.fail_save:
                raise RuntimeError("database went away")
            self.s.notifications.append(args)
        elif "INSERT INTO patient_memory" in query:
            self.s.inserted.append(args)
            return "INSERT 0 1"
        elif "SET followed_up_at = NOW()" in query:
            self.s.followed_up.append(args)
        else:
            raise AssertionError(query)
        return "UPDATE 1"


class Pool:
    def __init__(self, store):
        self.store = store

    def acquire(self):
        return Conn(self.store)


@pytest.fixture
def world(monkeypatch):
    store = types.SimpleNamespace(
        chat={"language": "zh-TW", "risk_flag": False, "followup_memory_id": None, "after_chat_state": "pending",
              "after_chat_attempts": 0, "refundable": True, "summary": None, "mood": None, "started_at": NOW},
        fail_save=False,
        turns=[{"role": "reachy", "text": conversation.OPENING["zh-TW"]},
               {"role": "patient", "text": "我孫女 Amy 下週日要來看我"},
               {"role": "reachy", "text": "真好！"},
               {"role": "patient", "text": "我很喜歡在陽台種花"}],
        facts=[], tombstones=[], inserted=[], followed_up=[], sql=[], followup_tries=1, deleted_chat=False,
        model_calls=[], call_options=[], answers=None, answer=None, reason="ok", consent=dict(MEMORY_ON),
        summary=("談到孫女。", "happy", None), summary_reason="ok", notifications=[], alerts=[])

    async def get_state(u_id):
        return store.consent

    async def fetch_state(conn, u_id):
        return store.consent

    async def complete_with_reason(messages, max_tokens=200, temperature=0.7, **options):
        # The model chain: each answer goes through accept, as the primary and then the fallback model would.
        store.model_calls.append(messages)
        store.call_options.append({"max_tokens": max_tokens, "temperature": temperature, **options})
        accept = options.get("accept") or (lambda answer: answer or None)
        for answer in (store.answers or [store.answer]):
            result = accept(answer)
            if result is not None:
                return result, "ok"
        return None, store.reason

    async def summarize(history, language, info=None):
        store.model_calls.append(history)
        if info is not None:
            info["reason"] = store.summary_reason
        return store.summary

    async def enqueue_to_contacts(conn, u_id, **kwargs):
        store.alerts.append({"u_id": u_id, **kwargs})
        return 1

    monkeypatch.setattr(after_chat, "get_pool", lambda: Pool(store))
    monkeypatch.setattr(consent_service, "get_state", get_state)
    monkeypatch.setattr(consent_service, "fetch_state", fetch_state)
    monkeypatch.setattr(conversation, "complete_with_reason", complete_with_reason)
    monkeypatch.setattr(conversation, "summarize", summarize)
    monkeypatch.setattr(outbox, "enqueue_to_contacts", enqueue_to_contacts)
    monkeypatch.setattr(memory, "local_today", lambda: date(2026, 10, 3))
    store.answer = ("MOOD: happy\nSUMMARY: 長者談到孫女和種花。\nRISK: none\n"
                    '{"facts": [{"kind": "event", "subject": "amy_visit", "text": "孫女 Amy 週日來訪", "event_date": "2026-10-11"},'
                    ' {"kind": "like", "subject": "garden", "text": "喜歡在陽台種花", "event_date": null},'
                    ' {"kind": "person", "subject": "meimei", "text": "女兒美美", "event_date": null}]}')
    return store


def run(store):
    return asyncio.run(after_chat.process(CID, 7))


def test_risk_chat_makes_no_model_call(world):
    world.chat["risk_flag"] = True
    assert run(world) == "skipped"
    assert world.model_calls == [] and world.chat["summary"] is None and world.chat["mood"] == "unknown"


def test_stores_summary_and_only_grounded_facts_under_the_lock(world):
    assert run(world) == "done"
    assert world.chat["summary"] == "長者談到孫女和種花。" and world.chat["mood"] == "happy"
    assert [args[3] for args in world.inserted] == ["amy_visit", "garden"]      # meimei is not in the patient's words
    lock = next(i for i, q in enumerate(world.sql) if 'FOR UPDATE' in q)
    first_insert = next(i for i, q in enumerate(world.sql) if "INSERT INTO patient_memory" in q)
    assert lock < first_insert and world.chat["after_chat_state"] == "done"


def test_bad_json_still_saves_summary(world):
    world.answer = 'MOOD: calm\nSUMMARY: 長者談到天氣。\nRISK: none\n{"facts": [{"kind": "like"'
    assert run(world) == "done"
    assert world.chat["summary"] == "長者談到天氣。" and world.inserted == []


def test_withdrawn_consent_stores_nothing(world, monkeypatch):
    async def fetch_state(conn, u_id):
        return {}            # withdrawn between the model call and the write

    monkeypatch.setattr(consent_service, "fetch_state", fetch_state)
    assert run(world) == "done" and world.inserted == []


def test_without_memory_consent_only_the_old_summary_runs(world):
    world.consent = {k: v for k, v in MEMORY_ON.items() if k != "conversation_memory"}
    assert run(world) == "done"
    assert world.chat["summary"] == "談到孫女。" and world.inserted == []


def test_tombstoned_subject_is_not_relearned(world):
    world.tombstones = [{"kind": "like", "subject": "garden"}]
    run(world)
    assert [args[3] for args in world.inserted] == ["amy_visit"]


def test_deleted_chat_stores_nothing(world):
    world.deleted_chat = True
    assert run(world) == "done" and world.inserted == []


def test_rate_limit_leaves_the_chat_pending_and_does_not_count_the_attempt(world):
    world.answer, world.reason = None, "rate_limited"
    assert run(world) == "pending"
    assert world.chat["after_chat_state"] == "pending" and world.chat["after_chat_attempts"] == 0


def test_short_chat_gets_a_summary_only(world):
    world.turns = [{"role": "reachy", "text": "嗨"}, {"role": "patient", "text": "好"}]
    run(world)
    assert world.chat["summary"] == "談到孫女。" and world.inserted == []


def test_followup_marked_only_after_a_real_reply(world):
    world.chat["followup_memory_id"] = uuid.uuid4()
    world.turns = [{"role": "reachy", "text": conversation.OPENING["zh-TW"]},
                   {"role": "patient", "text": "嗯"}, {"role": "reachy", "text": conversation.FALLBACK["zh-TW"]}]
    run(world)
    assert world.followed_up == []
    world.chat.update(after_chat_state="pending")
    world.turns[-1] = {"role": "reachy", "text": "Amy 來玩得開心嗎？"}
    run(world)
    assert world.followed_up and world.followed_up[0][1] == "amy_visit"


def test_followup_given_up_after_two_chats(world):
    world.chat["followup_memory_id"] = uuid.uuid4()
    world.followup_tries = 2
    world.turns = [{"role": "reachy", "text": conversation.OPENING["zh-TW"]}, {"role": "patient", "text": "嗯"},
                   {"role": "reachy", "text": conversation.CLOSING["zh-TW"]}]
    run(world)
    assert world.followed_up


def test_a_claimed_chat_is_not_processed_twice(world):
    world.chat["after_chat_state"] = "done"
    assert run(world) is None and world.model_calls == []


def test_sweep_closes_abandoned_chats_and_reprocesses_pending(monkeypatch):
    from app.jobs import after_chat_job
    sql, processed = [], []

    class C:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        def transaction(self):
            return self

        async def execute(self, query, *args):
            sql.append((query, args))

        async def fetch(self, query, *args):
            sql.append((query, args))
            if "after_chat_state = CASE" in query:   # end_stale: nothing that old
                return []
            return [{"conversation_id": uuid.UUID(CID), "u_id": 7}]

    class P:
        def acquire(self):
            return C()

    async def process(cid, u_id):
        processed.append((cid, u_id))
        return "done"

    monkeypatch.setattr(after_chat_job, "get_pool", lambda: P())
    monkeypatch.setattr(after_chat, "process", process)
    asyncio.run(after_chat_job.run_after_chat_sweep())
    assert "end_reason = 'abandoned'" in sql[0][0] and sql[0][1] == (after_chat_job.ABANDON_MINUTES,)
    assert "after_chat_state = CASE" in sql[1][0]   # end_stale, before the chats it ends could be picked
    assert "make_interval(days => $3)" in sql[2][0] and sql[2][1][2] == after_chat.RETRY_DAYS
    assert processed == [(CID, 7)]


def test_retention_purges_transcripts_events_and_tombstones():
    from app.jobs import conversation_retention_job as job
    sql = []

    class C:
        async def execute(self, query, *args):
            sql.append((query, args))

    asyncio.run(job.run_retention(C()))
    assert "conversation_turn" in sql[0][0]
    assert "kind = 'event'" in sql[1][0] and sql[1][1] == (config.MEDCARE_TIMEZONE, 30)
    assert "patient_memory_deleted" in sql[2][0] and sql[2][1] == (7,)


def test_withdrawn_checkin_consent_means_no_model_call(world):
    # e.g. an abandoned chat closed by the sweep after the patient switched check-ins off
    world.consent = {k: v for k, v in MEMORY_ON.items() if k != "conversation_analysis"}
    assert run(world) == "skipped"
    assert world.model_calls == [] and world.chat["summary"] is None


def test_an_empty_answer_is_retried_by_the_sweep(world):
    # openrouter/free sometimes routes to a model that returns nothing usable; the sweep should try again.
    world.answer, world.reason = None, "empty"
    assert run(world) == "failed" and world.chat["after_chat_state"] == "failed"
    assert world.notifications == [] and world.chat["mood"] is None


# ── the summary's risk backstop (layer 3) on both paths ──

KNOWN = {"memory_id": "g", "kind": "like", "subject": "garden", "text": "喜歡在陽台種花", "event_date": None,
         "source": "chat", "followed_up_at": None, "created_at": NOW}
RISKY = ("MOOD: sad\nSUMMARY: 長者說孫女來時也想傷害自己。\nRISK: self_harm\n"
         '{"facts": [{"kind": "like", "subject": "garden", "text": "喜歡在陽台種花", "event_date": null}]}')


def test_the_combined_call_asks_for_the_risk_line_with_the_chain_and_no_retry_loop(world):
    run(world)
    (options,) = world.call_options   # one call: the model chain (primary, then fallback) is the retry
    assert options["max_tokens"] == 1000 and options["temperature"] == 0 and callable(options["accept"])
    assert options["deadline"] == memory.AFTER_CHAT_DEADLINE_SECONDS
    assert options["call_timeout"] == memory.AFTER_CHAT_CALL_TIMEOUT
    for prompt in memory.AFTER_CHAT_PROMPT.values():
        assert "RISK: none|self_harm|overdose" in prompt and "self_harm" in prompt and "overdose" in prompt


@pytest.mark.parametrize("facts_known", [False, True])
def test_a_risk_in_the_combined_answer_alerts_once_and_stores_no_facts(world, facts_known):
    world.facts = [KNOWN] if facts_known else []
    world.answer = RISKY
    assert run(world) == "done"
    assert world.chat["risk_flag"] is True and world.chat["summary"] == "長者說孫女來時也想傷害自己。"
    assert world.inserted == []                                   # risk content never becomes memory
    (alert,) = world.alerts
    assert alert["kind"] == "safety_alert" and alert["contact_flag"] is None
    assert alert["dedupe_prefix"] == f"safety_alert:{CID}:summary"
    text = alert["messages"][0]["text"]
    assert "Pearl" in text and "對話摘要" in text and "傷害自己" in text
    # Written with the patient's memory notes in the prompt, the summary is not quoted (memory notice §3).
    assert ("長者說孫女來時" in text) is not facts_known
    calls = len(world.model_calls)
    world.chat["after_chat_state"] = "pending"   # run again (a retry): the chat is flagged now
    assert run(world) == "skipped" and len(world.alerts) == 1 and len(world.model_calls) == calls


def test_a_risk_any_answer_gave_counts_and_keeps_facts_out(world):
    # The primary model's answer is not enough (no summary, no facts) but says self_harm; the fallback says none.
    world.answers = ["MOOD: sad\nRISK: self_harm", world.answer]
    assert run(world) == "done"
    assert len(world.alerts) == 1 and world.inserted == [] and world.chat["summary"] == "長者談到孫女和種花。"


def test_an_answer_without_a_risk_line_goes_to_the_fallback_model(world):
    no_risk_line = world.answer.replace("RISK: none\n", "")
    world.answers = [no_risk_line, world.answer]
    assert run(world) == "done"
    assert [args[3] for args in world.inserted] == ["amy_visit", "garden"]   # from the fallback's full answer


@pytest.mark.parametrize("memory_on", [True, False])
def test_a_missing_risk_line_is_retried_and_the_last_attempt_keeps_the_summary_and_tells_family(world, memory_on):
    # No model wrote a RISK line: the backstop never judged the chat, so it is not 'done'.
    if memory_on:
        world.answer, world.reason = world.answer.replace("RISK: none\n", ""), "empty"
    else:
        world.consent = {k: v for k, v in MEMORY_ON.items() if k != "conversation_memory"}
        world.summary, world.summary_reason = ("談到孫女。", "happy", None), "empty"
    for attempt in range(1, after_chat.MAX_ATTEMPTS):
        assert run(world) == "failed" and world.chat["summary"] is None and world.chat["mood"] is None
        assert world.notifications == [] and world.chat["after_chat_attempts"] == attempt
    assert run(world) == "failed"   # the last attempt
    assert world.chat["summary"] == ("長者談到孫女和種花。" if memory_on else "談到孫女。")
    assert world.chat["mood"] == "happy" and world.inserted == [] and world.alerts == []
    assert [note[1] for note in world.notifications] == ["safety_check_incomplete"]
    assert run(world) is None and len(world.notifications) == 1   # never claimed again


def test_a_risk_on_the_summary_path_alerts_with_the_summary_quoted(world):
    world.consent = {k: v for k, v in MEMORY_ON.items() if k != "conversation_memory"}
    world.summary = ("長者表達了想自傷的念頭。", "sad", "self_harm")
    assert run(world) == "done"
    (alert,) = world.alerts
    assert "長者表達了想自傷的念頭" in alert["messages"][0]["text"] and world.chat["mood"] == "sad"


def test_store_facts_refuses_a_flagged_chat(world):
    world.chat["risk_flag"] = True   # flagged after the model call, before the write

    async def store():
        return await memory.store_facts(Conn(world), 7, CID, [{"kind": "like", "subject": "garden",
                                                               "text": "喜歡在陽台種花", "event_date": None}])

    assert asyncio.run(store()) == 0 and world.inserted == []


# ── retries: never silent ──

@pytest.mark.parametrize("reason", ["unavailable", "empty", "no_key", "error"])
def test_the_last_failed_attempt_tells_family_once(world, reason):
    world.consent = {k: v for k, v in MEMORY_ON.items() if k != "conversation_memory"}
    world.summary, world.summary_reason = (None, "unknown", None), reason
    for attempt in range(1, after_chat.MAX_ATTEMPTS + 1):
        assert run(world) == "failed" and world.chat["after_chat_attempts"] == attempt
        assert len(world.notifications) == (1 if attempt == after_chat.MAX_ATTEMPTS else 0)
    assert run(world) is None and len(world.notifications) == 1   # never claimed again
    (note,) = world.notifications
    assert note[:2] == (7, "safety_check_incomplete") and "safety check" in note[2]
    assert world.chat["mood"] == "unknown" and world.chat["summary"] is None and world.alerts == []


@pytest.mark.parametrize("risk_flag, mood, state, noted", [
    (False, None, "failed", 1),        # nothing saved: the safety check did not run
    (False, "happy", "done", 0),       # saved before the state write failed
    (True, None, "skipped", 0),        # family were alerted by an earlier layer
])
def test_a_last_attempt_that_never_finished_is_ended_never_silently(world, risk_flag, mood, state, noted):
    # Claimed (attempts == MAX) but still 'pending': a restart or an error before the state write.
    world.chat.update(risk_flag=risk_flag, mood=mood, after_chat_attempts=after_chat.MAX_ATTEMPTS)
    assert run(world) == state and world.model_calls == []
    assert world.chat["after_chat_state"] == state and world.chat["mood"] == (mood or "unknown")
    assert [note[1] for note in world.notifications] == ["safety_check_incomplete"] * noted
    assert run(world) is None and len(world.notifications) == noted   # ended once


def test_a_last_attempt_still_running_is_left_alone(world):
    world.chat["after_chat_attempts"] = after_chat.MAX_ATTEMPTS
    after_chat._running.add(CID)
    try:
        assert run(world) is None
    finally:
        after_chat._running.discard(CID)
    assert world.chat["after_chat_state"] == "pending" and world.notifications == []


def test_an_error_on_the_last_attempt_is_not_silent(world, monkeypatch):
    world.chat["after_chat_attempts"] = after_chat.MAX_ATTEMPTS - 1

    async def broken(u_id):
        raise RuntimeError("database went away")

    monkeypatch.setattr(consent_service, "get_state", broken)
    with pytest.raises(RuntimeError):
        run(world)   # claimed, then failed before any state was written
    assert world.chat["after_chat_state"] == "pending" and world.notifications == []
    assert run(world) == "failed"   # the next sweep ends it
    assert [note[1] for note in world.notifications] == ["safety_check_incomplete"]


def test_a_flagged_chat_gets_no_safety_check_incomplete(world):
    world.chat.update(risk_flag=True, after_chat_attempts=after_chat.MAX_ATTEMPTS - 1)
    assert run(world) == "skipped" and world.notifications == [] and world.model_calls == []


def test_a_rate_limit_counts_as_an_attempt_after_the_first_ten_minutes(world, monkeypatch):
    claims = []
    real_fetchrow = Conn.fetchrow

    async def fetchrow(self, query, *args):
        if "after_chat_attempts + 1" in query:
            claims.append((query, args))
        return await real_fetchrow(self, query, *args)

    monkeypatch.setattr(Conn, "fetchrow", fetchrow)
    world.answer, world.reason = None, "rate_limited"
    world.chat.update(refundable=False, after_chat_attempts=after_chat.MAX_ATTEMPTS - 1)
    assert run(world) == "failed" and world.chat["after_chat_attempts"] == after_chat.MAX_ATTEMPTS
    assert [note[1] for note in world.notifications] == ["safety_check_incomplete"]
    # A refund only in the first 10 minutes: under a lasting rate limit family hear within about half an hour.
    ((query, args),) = claims
    assert "ended_at > NOW() - make_interval(mins => $4) AS refundable" in query and args[3] == 10


def test_post_chat_work_waits_for_a_late_risk_check_still_running(world):
    async def scenario():
        async def late_check():
            await asyncio.sleep(0.2)
            world.chat["risk_flag"] = True   # what a late check that finds risk leaves behind

        after_chat.track_risk_check(CID, asyncio.create_task(late_check()))
        state = await after_chat.process(CID, 7)
        return state, after_chat._risk_checks.get(CID)

    assert asyncio.run(scenario()) == ("skipped", None)
    assert world.model_calls == [] and world.chat["mood"] == "unknown"


def test_the_wait_for_a_late_risk_check_is_bounded(world, monkeypatch):
    monkeypatch.setattr(after_chat, "risk_check_wait", lambda: 0.1)

    async def scenario():
        hung = asyncio.create_task(asyncio.sleep(5))
        after_chat.track_risk_check(CID, hung)
        try:
            return await after_chat.process(CID, 7)
        finally:
            hung.cancel()

    assert asyncio.run(scenario()) == "done"


# ── review of the merge (3 Oct): never silent, and no memory notes in family alerts ──

def test_a_last_attempt_after_a_failed_one_that_never_finishes_is_not_silent(world, monkeypatch):
    # Attempt 2 failed ('failed'); attempt 3 is claimed and dies (a restart, an error). Left 'failed' with no
    # attempts, the sweep would never pick it up again: the claim makes it 'pending' until the attempt finishes.
    world.chat.update(after_chat_state="failed", after_chat_attempts=after_chat.MAX_ATTEMPTS - 1)

    async def broken(u_id):
        raise RuntimeError("database went away")

    with monkeypatch.context() as patch:
        patch.setattr(consent_service, "get_state", broken)
        with pytest.raises(RuntimeError):
            run(world)
    assert world.chat["after_chat_state"] == "pending" and world.notifications == []
    assert run(world) == "failed" and world.model_calls == []
    assert [note[1] for note in world.notifications] == ["safety_check_incomplete"]


def test_saving_that_fails_on_the_last_attempt_is_not_silent(world):
    world.consent = {k: v for k, v in MEMORY_ON.items() if k != "conversation_memory"}
    world.chat["after_chat_attempts"] = after_chat.MAX_ATTEMPTS - 1
    world.fail_save = True
    assert run(world) == "pending"   # left for the next sweep, not 'failed' with no attempts left
    assert world.notifications == [] and world.chat["mood"] is None
    world.fail_save = False
    assert run(world) == "failed" and len(world.model_calls) == 1   # ended without another model call
    assert [note[1] for note in world.notifications] == ["safety_check_incomplete"]


@pytest.mark.parametrize("final", [False, True])
def test_a_risk_whose_summary_cannot_be_saved_still_alerts_family(world, final):
    world.consent = {k: v for k, v in MEMORY_ON.items() if k != "conversation_memory"}
    world.summary = ("長者表達了想自傷的念頭。", "sad", "self_harm")
    world.chat["after_chat_attempts"] = after_chat.MAX_ATTEMPTS - 1 if final else 0
    world.fail_save = True
    assert run(world) == ("pending" if final else "failed")
    (alert,) = world.alerts
    assert alert["dedupe_prefix"] == f"safety_alert:{CID}:summary" and "長者表達了想自傷的念頭" in alert["messages"][0]["text"]
    world.fail_save = False
    calls = len(world.model_calls)
    assert run(world) == "skipped" and len(world.model_calls) == calls   # flagged now: no model call
    assert world.notifications == [] and len(world.alerts) == 1


SELF_HARM_SUMMARY = ("長者說活著好累，也聊到在陽台種花。", "sad", "self_harm")


@pytest.mark.parametrize("facts, followup, consent, quoted", [
    ([], None, "on", True),                  # memory on, but no notes yet: nothing to carry
    ([KNOWN], None, "on", False),            # notes in the reply's block: Reachy's lines may repeat them
    ([], "followup", "on", False),           # the chat was opened with a follow-up note
    ([KNOWN], None, "off", True),            # memory off before the chat: the notes were never used
    ([KNOWN], None, "withdrawn during", False),   # memory on when the chat started
])
def test_a_short_memory_chat_alert_never_quotes_a_summary_reachy_had_notes_for(world, facts, followup, consent,
                                                                               quoted):
    # Fewer than 2 patient turns: the plain SUMMARY_PROMPT path, which used to quote whenever memory was on.
    world.turns = [{"role": "reachy", "text": "王奶奶，" + conversation.OPENING["zh-TW"]},
                   {"role": "patient", "text": "活著好累"}, {"role": "reachy", "text": "您還在陽台種花嗎？"}]
    world.facts = facts
    world.chat["followup_memory_id"] = uuid.uuid4() if followup else None
    if consent != "on":
        world.consent = {k: v for k, v in MEMORY_ON.items() if k != "conversation_memory"}
        changed = NOW.replace(hour=1) if consent == "off" else NOW.replace(hour=3)   # the chat started at 02:00
        world.consent["conversation_memory"] = {"granted": False, "terms_version": config.TERMS_VERSION,
                                                "created_at": changed}
    world.summary = SELF_HARM_SUMMARY
    assert run(world) == "done" and world.chat["summary"] == SELF_HARM_SUMMARY[0]
    (alert,) = world.alerts
    text = alert["messages"][0]["text"]
    assert "對話摘要" in text and "傷害自己" in text and ("陽台種花" in text) is quoted


def test_a_risk_line_followed_by_the_format_reminder_still_counts(world):
    # Review probe P2: the template echoed after the answer must not hide the RISK line the answer was accepted for.
    world.answer = ("MOOD: sad\nSUMMARY: 長者說不想活了。\nRISK: self_harm\n"
                    '{"facts": [{"kind": "like", "subject": "garden", "text": "喜歡在陽台種花", "event_date": null}]}\n'
                    "(format reminder: RISK: none|self_harm|overdose)")
    assert run(world) == "done"
    assert len(world.alerts) == 1 and world.inserted == [] and world.chat["risk_flag"] is True


class StaleConn:
    def __init__(self, rows):
        self.rows, self.sql, self.notes = rows, [], []

    def transaction(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def fetch(self, query, *args):
        self.sql.append((query, args))
        return self.rows

    async def execute(self, query, *args):
        assert "INSERT INTO notification" in query
        self.notes.append(args)


def test_chats_unfinished_after_the_retry_window_are_ended_never_silently():
    rows = [{"conversation_id": uuid.uuid4(), "u_id": 7, "after_chat_state": state}
            for state in ("failed", "done", "skipped")]
    conn = StaleConn(rows)
    after_chat._running.add("a-chat-being-processed")
    try:
        assert asyncio.run(after_chat.end_stale(conn)) == 3
    finally:
        after_chat._running.discard("a-chat-being-processed")
    ((query, args),) = conn.sql
    # pending at any count, failed with attempts left (a last failed attempt was told already), past the window
    assert "after_chat_state = 'pending' OR (after_chat_state = 'failed' AND after_chat_attempts < $1)" in query
    assert "ended_at <= NOW() - make_interval(days => $2)" in query and "ANY($3::text[])" in query
    assert "WHEN risk_flag THEN 'skipped' WHEN mood IS NOT NULL THEN 'done' ELSE 'failed'" in query
    assert "after_chat_attempts = GREATEST(after_chat_attempts, $1)" in query   # never picked again
    assert args == (after_chat.MAX_ATTEMPTS, after_chat.RETRY_DAYS, ["a-chat-being-processed"])
    assert [note[:2] for note in conn.notes] == [(7, "safety_check_incomplete")]   # only the one with nothing saved
