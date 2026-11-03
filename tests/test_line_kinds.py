import ast
import pathlib

import pytest
import requests

from app.services import line_pause, line_service

APP = pathlib.Path(__file__).resolve().parent.parent / "app"
PAUSE_FILE = APP / "services" / "line_pause.py"


def _files():
    return [p for p in sorted(APP.rglob("*.py")) if "__pycache__" not in p.parts]


def _rel(path):
    return path.relative_to(APP.parent).as_posix()


def test_every_send_text_call_names_a_kind():
    missing = []
    for path in _files():
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8-sig"))):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr in ("send_text", "push_messages")):
                continue
            receiver = node.func.value
            if isinstance(receiver, ast.Name) and receiver.id == "websocket":
                continue
            if not any(k.arg == "kind" for k in node.keywords):
                missing.append(f"{_rel(path)}:{node.lineno}")
    assert not missing, missing


def test_only_reminder_sends_use_med_reminder():
    """Ruling R2: exactly two references, both in missed_dose_job.py (Task 3 widens this to reminders.py)."""
    found = []
    for path in _files():
        if path == PAUSE_FILE:
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8-sig"))):
            if (isinstance(node, ast.Name) and node.id == "MED_REMINDER") or \
               (isinstance(node, ast.Attribute) and node.attr == "MED_REMINDER") or \
               (isinstance(node, ast.Constant) and node.value == "med_reminder"):
                found.append(_rel(path))
    assert sorted(found) == ["app/jobs/missed_dose_job.py"] * 2, found


def test_send_text_refuses_med_reminder_while_paused(monkeypatch):
    posts = []

    class Resp:
        status_code = 200
        headers = {}
        text = ""

    def fake_post(*args, **kwargs):
        posts.append(args)
        return Resp()

    monkeypatch.setattr(requests, "post", fake_post)
    monkeypatch.setattr(line_service.LineService, "_available", True)
    monkeypatch.setitem(line_pause._state, "paused", True)
    svc = line_service.LineService()
    monkeypatch.setattr(line_service.LineService, "_available", True)

    result = svc.send_text("U1", "hi", kind=line_pause.MED_REMINDER)
    assert result == {"sent": False, "paused": True, "error": "reminders_paused"}
    assert posts == []

    pushed = svc.push_messages("U1", [{"type": "text", "text": "hi"}], "k", kind=line_pause.MED_REMINDER)
    assert pushed == {"status": "paused", "http_status": None, "request_id": None, "error": "reminders_paused"}
    assert posts == []

    assert svc.send_text("U1", "hi", kind=line_pause.OTHER)["sent"] is True
    assert len(posts) == 1
