"""API keys saved on the AI connections card: encrypted at rest with a key derived from SECRET_KEY.

A key never goes back to the browser (only its last 4 characters) and never into logs or error text (redact).
A key saved under another SECRET_KEY cannot be read (decrypt returns None); the card then asks for it again.
"""

import base64
import re

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from app import config

DEFAULT_SECRET = "change-me-in-production-32chars!!"
HKDF_INFO = b"medaicareplus ai-connection keys"
# What an API key or an Authorization header looks like inside a service's error text.
_KEY_SHAPES = re.compile(r"sk-[A-Za-z0-9_-]{6,}|Bearer\s+\S+", re.IGNORECASE)


def _fernet(secret: str | None) -> Fernet:
    # A key derived for this purpose only: the face-token signing secret is never used as an encryption key.
    secret = config.SECRET_KEY if secret is None else secret
    derived = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=HKDF_INFO).derive(secret.encode("utf-8"))
    return Fernet(base64.urlsafe_b64encode(derived))


def encrypt(api_key: str, secret: str | None = None) -> str:
    return _fernet(secret).encrypt(api_key.encode("utf-8")).decode("ascii")


def decrypt(token: str, secret: str | None = None) -> str | None:
    """The key, or None when this SECRET_KEY cannot read it."""
    try:
        return _fernet(secret).decrypt(token.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError):
        return None


def last4(api_key: str) -> str:
    return api_key[-4:]


def weak_secret() -> bool:
    """True while SECRET_KEY is empty or the committed default: the encryption then protects nothing."""
    return config.SECRET_KEY.strip() in ("", DEFAULT_SECRET)


def redact(text: str, secret: str = "") -> str:
    """text without anything shaped like a key, and without `secret` (the exact key, when the caller holds it: a
    service may echo a key of any shape). Redact before cutting text short, or a cut key escapes the match."""
    if secret:
        text = text.replace(secret, "[key removed]")
    return _KEY_SHAPES.sub("[key removed]", text)
