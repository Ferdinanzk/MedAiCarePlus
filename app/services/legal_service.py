"""Versioned legal notices: load, fill in operator values, hash exactly what is shown.

The server is the source of truth for the notice text so consent can reference
the precise document a person accepted (by kind, version, language, sha256).
"""

import hashlib
import json
import re
from functools import lru_cache
from typing import NamedTuple, cast

from app import config

KIND_SCOPES: dict[str, tuple[str, ...]] = {
    "core": ("core",),
    "robot": ("robot_camera", "robot_microphone", "cloud_voice", "conversation_analysis", "safety_alerts"),
}
SCOPE_KIND: dict[str, str] = {scope: kind for kind, scopes in KIND_SCOPES.items() for scope in scopes}
LANGUAGES = ("en", "zh-TW")
PLACEHOLDER = re.compile(r"\{[A-Z][A-Z0-9_]*\}")


class LegalDocument(NamedTuple):
    kind: str
    version: str
    language: str
    document: dict
    sha256: str
    complete: bool


def canonical_hash(doc: dict) -> str:
    payload = json.dumps(doc, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def render(raw, fills: dict[str, str]):
    """Replace {KEY} in every string; empty fill-ins are left as placeholders."""
    if isinstance(raw, str):
        return PLACEHOLDER.sub(lambda m: fills.get(m.group(0)[1:-1]) or m.group(0), raw)
    if isinstance(raw, list):
        return [render(item, fills) for item in raw]
    if isinstance(raw, dict):
        return {key: render(value, fills) for key, value in raw.items()}
    return raw


def has_placeholders(doc) -> bool:
    if isinstance(doc, str):
        return bool(PLACEHOLDER.search(doc))
    if isinstance(doc, list):
        return any(has_placeholders(item) for item in doc)
    if isinstance(doc, dict):
        return any(has_placeholders(value) for value in doc.values())
    return False


def fills() -> dict[str, str]:
    return {
        "OPERATOR_NAME": config.OPERATOR_NAME,
        "OPERATOR_CONTACT": config.OPERATOR_CONTACT,
        "TUNNEL_PROVIDER": config.TUNNEL_PROVIDER,
        "LLM_SERVICE": config.LLM_SERVICE,
        "LLM_PROVIDER": config.LLM_PROVIDER,
        "LLM_PROVIDER_REGION": config.LLM_PROVIDER_REGION,
        "LLM_RETENTION": config.LLM_RETENTION,
    }


def normalise_language(lang: str | None) -> str:
    return "zh-TW" if (lang or "").strip().lower().startswith("zh") else "en"


@lru_cache(maxsize=8)
def load_documents(version: str | None = None) -> dict[tuple[str, str], LegalDocument]:
    version = version or config.TERMS_VERSION
    values = fills()
    documents = {}
    for kind in KIND_SCOPES:
        for language in LANGUAGES:
            path = config.LEGAL_DIR / kind / version / f"{language}.json"
            raw = json.loads(path.read_text(encoding="utf-8"))
            if (raw.get("kind"), raw.get("version"), raw.get("language")) != (kind, version, language):
                raise ValueError(f"{path} header does not match its location")
            document = cast(dict, render(raw, values))
            documents[(kind, language)] = LegalDocument(
                kind, version, language, document, canonical_hash(document), not has_placeholders(document))
    return documents


def get_document(kind: str, language: str | None) -> LegalDocument:
    if kind not in KIND_SCOPES:
        raise KeyError(kind)
    return load_documents()[(kind, normalise_language(language))]


async def register_documents(conn) -> None:
    for doc in load_documents().values():
        await conn.execute(
            "INSERT INTO legal_document (kind, terms_version, language, sha256) VALUES ($1,$2,$3,$4) "
            "ON CONFLICT DO NOTHING",
            doc.kind, doc.version, doc.language, doc.sha256)
