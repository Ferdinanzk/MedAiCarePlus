"""Saved API keys: encrypted with a key derived from SECRET_KEY, unreadable under another one, never echoed."""

from app import config
from app.services import ai_crypto


def test_a_key_round_trips_under_the_same_secret():
    token = ai_crypto.encrypt("sk-test-abcd1234", secret="secret-one")
    assert token != "sk-test-abcd1234" and "abcd1234" not in token
    assert ai_crypto.decrypt(token, secret="secret-one") == "sk-test-abcd1234"


def test_a_key_saved_under_another_secret_cannot_be_read():
    token = ai_crypto.encrypt("sk-test-abcd1234", secret="secret-one")
    assert ai_crypto.decrypt(token, secret="secret-two") is None
    assert ai_crypto.decrypt("not-a-token", secret="secret-one") is None


def test_the_secret_comes_from_config_by_default(monkeypatch):
    monkeypatch.setattr(config, "SECRET_KEY", "from-config")
    assert ai_crypto.decrypt(ai_crypto.encrypt("sk-x-123456"), secret="from-config") == "sk-x-123456"


def test_last4_and_weak_secret(monkeypatch):
    assert ai_crypto.last4("sk-or-v1-0123456789wxyz") == "wxyz"
    monkeypatch.setattr(config, "SECRET_KEY", ai_crypto.DEFAULT_SECRET)
    assert ai_crypto.weak_secret()
    monkeypatch.setattr(config, "SECRET_KEY", "a-real-random-secret")
    assert not ai_crypto.weak_secret()


def test_redact_removes_key_shapes_from_error_text():
    cleaned = ai_crypto.redact("Incorrect API key provided: sk-proj-AbCdEf123456. Sent Bearer sk-or-v1-zzzzzzzz")
    assert "AbCdEf123456" not in cleaned and "zzzzzzzz" not in cleaned
    assert cleaned.startswith("Incorrect API key provided: [key removed]")
