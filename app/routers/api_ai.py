"""The AI connections card (spec docs/superpowers/specs/2026-10-10-ai-connections-design.md): services with their
keys, models with the request profile the Test button finds, and the main and backup model of each job.

Every route needs a consented user; any account may change the setup (spec decision 3). Keys never leave the
server: answers carry only their last 4 characters. Every change answers with the whole card. The live voice model
(spec 2026-10-12 E2) has two routes of its own, which exist only while .env has LIVE_VOICE_TEST."""

import asyncio
import dataclasses
import logging
import re
import time
from collections import Counter
from typing import Literal

import requests
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app import config
from app.database import get_pool
from app.dependencies import get_consented_user
from app.services import ai_crypto, ai_profile, ai_settings, ai_store, conversation, live_client

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/ai", tags=["ai-connections"])
REMOTE_MODELS_LIMIT = 2000
REMOTE_MODELS_TIMEOUT = 10
# No "@" before the path: a user:password@ in the address would show on the card in clear and go out with every call.
_BASE_URL = re.compile(r"^https?://[^\s/@]+(/\S*)?$", re.IGNORECASE)


class NewConnection(BaseModel):
    name: str = Field(max_length=60)
    kind: str
    base_url: str | None = Field(default=None, max_length=300)
    api_key: str | None = Field(default=None, max_length=500)
    service_name: str = Field(max_length=80)


class ConnectionChange(BaseModel):
    name: str | None = Field(default=None, max_length=60)
    base_url: str | None = Field(default=None, max_length=300)
    api_key: str | None = Field(default=None, max_length=500)
    service_name: str | None = Field(default=None, max_length=80)


class NewModel(BaseModel):
    connection_id: int
    model_id: str = Field(max_length=100)
    company: str = Field(default="", max_length=80)
    region: str = Field(default="", max_length=80)
    retention: str = Field(default="", max_length=200)


class ModelChange(BaseModel):
    company: str | None = Field(default=None, max_length=80)
    region: str | None = Field(default=None, max_length=80)
    retention: str | None = Field(default=None, max_length=200)


class JobChoice(BaseModel):
    primary_ref: int
    fallback_ref: int | None = None
    thinking: Literal["off", "low", "medium", "high"] = "off"   # each model gets it or its nearest lower level


class Jobs(BaseModel):
    reply: JobChoice
    safety: JobChoice
    after_chat: JobChoice


class LiveChoice(BaseModel):
    connection_id: int
    model_id: str = Field(max_length=100)
    voice: Literal[ai_settings.LIVE_VOICES]   # the card's list; any other voice is a 422


def _card() -> dict:
    return {**ai_settings.public(ai_settings.current()), "weak_secret": ai_crypto.weak_secret()}


def _name(value: str) -> str:
    value = value.strip()
    if not value:
        raise HTTPException(400, "invalid_name")
    return value


def _base_url(kind: str, base_url: str | None) -> str:
    """The fixed address of a known service, or a cleaned "Other" one: no trailing / and no /chat/completions
    (a pasted full address would otherwise be called as /chat/completions/chat/completions)."""
    if kind not in ai_profile.KINDS:
        raise HTTPException(400, "invalid_kind")
    if kind != "openai_compatible":
        return ai_profile.BASE_URLS[kind]
    url = re.sub(r"/chat/completions/?$", "", (base_url or "").strip(), flags=re.IGNORECASE).rstrip("/")
    if not _BASE_URL.match(url):
        raise HTTPException(400, "invalid_base_url")
    return url


def _connection(connection_id: int) -> ai_settings.Connection:
    connection = ai_settings.current().connections.get(connection_id)
    if connection is None or connection_id == ai_settings.ENV_CONNECTION_ID:
        raise HTTPException(404, "not_found")
    return connection


def _model(model_ref: int) -> ai_settings.Model:
    model = ai_settings.current().models.get(model_ref)
    if model is None or model_ref <= 0:
        raise HTTPException(404, "not_found")
    return model


def _guard(snapshot: ai_settings.Snapshot) -> None:
    """With the robot feature in prod, refuse a change that adds a problem the next start would refuse (spec §10).
    Only an added one: with two incomplete models in use, fixing either must not be refused for the other."""
    if config.REACHY_FEATURE_ENABLED and config.APP_ENV == "prod":
        added = Counter(ai_settings.problems(snapshot)) - Counter(ai_settings.problems(ai_settings.current()))
        if added:
            raise HTTPException(400, "notice_incomplete")


def _live_on() -> None:
    """The live voice routes exist only while .env has LIVE_VOICE_TEST (spec 2026-10-12 §5). Used as a route
    dependency, so it answers before the body is checked: with the switch off, any body gets 404."""
    if not config.LIVE_VOICE_TEST:
        raise HTTPException(404, "not_found")


def _openai_connection(connection_id: int) -> ai_settings.Connection:
    """GPT-Live answers only on OpenAI's own Live WebSocket, so only an OpenAI connection's key can open it."""
    connection = _connection(connection_id)
    if connection.kind != "openai":
        raise HTTPException(400, "invalid_kind")
    return connection


@router.get("/settings")
async def settings(user: dict = Depends(get_consented_user)):
    return _card()


@router.post("/connections")
async def add_connection(body: NewConnection, user: dict = Depends(get_consented_user)):
    name, service_name = _name(body.name), _name(body.service_name)
    base_url = _base_url(body.kind, body.base_url)
    if any(c.name == name for c in ai_settings.current().connections.values()):
        raise HTTPException(409, "already_exists")
    async with get_pool().acquire() as conn:
        async with ai_store.change(conn):
            await ai_store.add_connection(conn, name=name, kind=body.kind, base_url=base_url,
                                          api_key=(body.api_key or "").strip(), service_name=service_name)
    return _card()


@router.patch("/connections/{connection_id}")
async def update_connection(connection_id: int, body: ConnectionChange, user: dict = Depends(get_consented_user)):
    connection = _connection(connection_id)
    name = _name(body.name) if body.name is not None else None
    if name and any(c.name == name and c.connection_id != connection_id
                    for c in ai_settings.current().connections.values()):
        raise HTTPException(409, "already_exists")
    base_url = None
    if body.base_url is not None:
        if connection.kind != "openai_compatible":
            raise HTTPException(400, "invalid_base_url")
        base_url = _base_url(connection.kind, body.base_url)
        if (base_url != connection.base_url and (connection.key_last4 is not None or connection.api_key)
                and not (body.api_key or "").strip()):
            raise HTTPException(400, "no_key")   # the stored key must not follow a new address
    async with get_pool().acquire() as conn:
        async with ai_store.change(conn):
            await ai_store.update_connection(
                conn, connection_id, name=name, base_url=base_url, api_key=(body.api_key or "").strip() or None,
                service_name=_name(body.service_name) if body.service_name is not None else None)
    return _card()


@router.delete("/connections/{connection_id}")
async def delete_connection(connection_id: int, user: dict = Depends(get_consented_user)):
    _connection(connection_id)
    try:
        async with get_pool().acquire() as conn:
            async with ai_store.change(conn):
                await ai_store.delete_connection(conn, connection_id)
    except ai_store.InUse:
        raise HTTPException(409, "in_use") from None
    return _card()


@router.get("/connections/{connection_id}/remote-models")
async def remote_models(connection_id: int, user: dict = Depends(get_consented_user)):
    connection = _connection(connection_id)

    def fetch() -> list[str]:
        response = requests.get(f"{connection.base_url}/models", timeout=REMOTE_MODELS_TIMEOUT,
                                headers=ai_profile.headers(connection.kind, connection.api_key)
                                if connection.api_key else {})
        if response.status_code != 200:
            raise RuntimeError(f"HTTP {response.status_code}")
        body = response.json()
        if not isinstance(body, dict) or not isinstance(body.get("data") or [], list):
            raise RuntimeError("unexpected body")
        data = body.get("data") or []
        return sorted({item["id"] for item in data if isinstance(item, dict) and isinstance(item.get("id"), str)})

    try:
        models = await asyncio.to_thread(fetch)
    except (requests.RequestException, ValueError, RuntimeError) as exc:
        log.warning("model list for AI connection %s failed: %s", connection_id,
                    exc if isinstance(exc, RuntimeError) else type(exc).__name__)
        raise HTTPException(502, "remote_models_unavailable") from None
    return {"models": models[:REMOTE_MODELS_LIMIT]}


@router.post("/models")
async def add_model(body: NewModel, user: dict = Depends(get_consented_user)):
    model_id = _name(body.model_id)
    _connection(body.connection_id)
    if any(m.connection_id == body.connection_id and m.model_id == model_id
           for m in ai_settings.current().models.values()):
        raise HTTPException(409, "already_exists")
    async with get_pool().acquire() as conn:
        async with ai_store.change(conn):
            await ai_store.add_model(conn, connection_id=body.connection_id, model_id=model_id,
                                     company=body.company.strip(), region=body.region.strip(),
                                     retention=body.retention.strip())
    return _card()


@router.patch("/models/{model_ref}")
async def update_model(model_ref: int, body: ModelChange, user: dict = Depends(get_consented_user)):
    changed = dataclasses.replace(_model(model_ref), **{key: value.strip() for key, value in body.model_dump().items()
                                                        if value is not None})
    _guard(ai_settings.with_model(ai_settings.current(), changed))
    async with get_pool().acquire() as conn:
        async with ai_store.change(conn):
            await ai_store.update_model(conn, model_ref, company=changed.company, region=changed.region,
                                        retention=changed.retention)
    return _card()


@router.delete("/models/{model_ref}")
async def delete_model(model_ref: int, user: dict = Depends(get_consented_user)):
    _model(model_ref)
    try:
        async with get_pool().acquire() as conn:
            async with ai_store.change(conn):
                await ai_store.delete_model(conn, model_ref)
    except ai_store.InUse:
        raise HTTPException(409, "in_use") from None
    return _card()


@router.post("/models/{model_ref}/test")
async def test_model(model_ref: int, user: dict = Depends(get_consented_user)):
    """Find the request settings this model accepts (ai_profile.detect), then the thinking levels it accepts
    (ai_profile.detect_thinking), and save them, or why it failed. A failed base test (the service's words, or
    ai_profile.NOT_CHAT_MODEL for a voice-only model) saves only its reason and starts no thinking probe."""
    model = _model(model_ref)
    connection = ai_settings.current().connections[model.connection_id]
    if not connection.key_readable:
        raise HTTPException(400, "key_unreadable")
    if not connection.api_key:
        raise HTTPException(400, "no_key")
    target = ai_settings.Target(connection, model)
    provider = conversation._provider() if connection.kind == "openrouter" else None   # as real calls are routed

    def send(profile: dict, level: str = "off"):
        body = ai_profile.probe_body(profile, model.model_id, level, {"provider": provider} if provider else None)
        started = time.monotonic()
        try:
            response = requests.post(target.url, json=body, timeout=ai_profile.TEST_TIMEOUT,
                                     headers=ai_profile.headers(connection.kind, connection.api_key))
        except requests.RequestException as exc:
            return 0, {"error": {"message": f"No answer: {type(exc).__name__}"}}, int((time.monotonic() - started) * 1000)
        ms = int((time.monotonic() - started) * 1000)
        try:
            parsed = response.json()
        except ValueError:
            parsed = response.text   # cut by detect, after the key is removed (a cut key would escape the match)
        return response.status_code, parsed, ms

    def openrouter_entry() -> dict | None:
        """The model's entry in OpenRouter's public model list (asked without the key), or None when it isn't listed."""
        response = requests.get(f"{connection.base_url}/models", timeout=REMOTE_MODELS_TIMEOUT)
        if response.status_code != 200:
            raise RuntimeError(f"HTTP {response.status_code}")
        body = response.json()
        data = body.get("data") if isinstance(body, dict) else None
        if not isinstance(data, list):
            raise RuntimeError("unexpected body")
        return next((item for item in data if isinstance(item, dict) and item.get("id") == model.model_id), None)

    # The exact key too: a service may echo a key of any shape in its error, which is saved and shown on the card.
    result = await asyncio.to_thread(ai_profile.detect, send, connection.kind, connection.api_key)
    if result["ok"]:
        # Apart from the base test (spec review focus 4): nothing here may cost the model the profile detect found.
        try:
            thinking = await asyncio.to_thread(ai_profile.detect_thinking, send, connection.kind, result["profile"],
                                               openrouter_entry)
        except Exception as exc:
            log.warning("thinking levels of AI model %s not found: %s", model_ref,
                        exc if isinstance(exc, RuntimeError) else type(exc).__name__)
        else:
            result = {**result, "profile": {**result["profile"], "thinking": thinking}}
    async with get_pool().acquire() as conn:
        async with ai_store.change(conn):
            await ai_store.save_test(conn, model_ref, result)
    return {**result, "settings": _card()}


@router.put("/jobs")
async def set_jobs(body: Jobs, user: dict = Depends(get_consented_user)):
    snapshot = ai_settings.current()
    chosen = {job: (getattr(body, job).primary_ref, getattr(body, job).fallback_ref) for job in ai_settings.JOBS}
    thinking = {job: getattr(body, job).thinking for job in ai_settings.JOBS}
    for ref in (ref for refs in chosen.values() for ref in refs if ref is not None):
        model = snapshot.models.get(ref)
        if model is None:
            raise HTTPException(404, "not_found")
        if model.profile is None:
            raise HTTPException(400, "untested_model")
    _guard(dataclasses.replace(snapshot, jobs=chosen, thinking=thinking))
    async with get_pool().acquire() as conn:
        async with ai_store.change(conn):
            await ai_store.set_jobs(conn, chosen, thinking)
    return _card()


@router.put("/live", dependencies=[Depends(_live_on)])
async def set_live(body: LiveChoice, user: dict = Depends(get_consented_user)):
    """The live voice model: an OpenAI connection, the model id and the voice. Saving clears the last Check, so the
    model is usable for check-ins only after the next Check passes."""
    _openai_connection(body.connection_id)
    model_id = _name(body.model_id)
    async with get_pool().acquire() as conn:
        async with ai_store.change(conn):
            await ai_store.set_live(conn, body.connection_id, model_id, body.voice)
    return _card()


@router.post("/live/check", dependencies=[Depends(_live_on)])
async def check_live(user: dict = Depends(get_consented_user)):
    """Open a GPT-Live session with the saved model and close it (live_client.check), then save its time or its
    error code. When the model was saved again while the Check ran, the result belongs to the old model and is not
    saved: the new one stays unchecked."""
    live = ai_settings.current().live
    if live is None or live.connection_id is None:
        raise HTTPException(404, "not_found")
    connection = _openai_connection(live.connection_id)
    if not connection.key_readable:
        raise HTTPException(400, "key_unreadable")
    if not connection.api_key:
        raise HTTPException(400, "no_key")
    ok, ms, error = await live_client.check(connection.api_key, live.model_id, live.voice)
    now = ai_settings.current().live
    if now is not None and (now.connection_id, now.model_id, now.voice) == (live.connection_id, live.model_id,
                                                                             live.voice):
        async with get_pool().acquire() as conn:
            async with ai_store.change(conn):
                await ai_store.save_live_check(conn, ok, ms, error)
    return {"ok": ok, "ms": ms, "error": error, "settings": _card()}
