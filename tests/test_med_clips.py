"""Spoken medication prompts for the robot (services/med_clips.py): the sentence per position and language, the
clip's key, the WAV normalization of the speech API's streamed file, and clip(): disk first, one render per key,
nothing without a medicine, a key or when the API fails."""

import asyncio
import io
import struct
import sys
import types
import wave

import pytest

_asyncpg_stub = types.ModuleType("asyncpg")
_asyncpg_stub.Pool = object
sys.modules.setdefault("asyncpg", _asyncpg_stub)

from app import config   # noqa: E402
from app.services import ai_settings, med_clips   # noqa: E402


def test_the_sentence_names_the_medicine_and_its_place_in_the_slot():
    assert med_clips.prompt_text("Adapalene", 1, "zh-TW") == "請現在吃第一種藥，Adapalene。"
    assert med_clips.prompt_text("Metformin", 2, "zh-TW") == "接下來，請吃第二種藥，Metformin。"
    assert med_clips.prompt_text("Adapalene", 1, "en") == "Please take your first medicine, Adapalene."
    assert med_clips.prompt_text("Amlodipine 5 mg", 3, "en-US") == "Next, please take your third medicine, Amlodipine 5 mg."
    assert med_clips.prompt_text(" 降血壓藥 ", 9, "zh-TW") == "接下來，請吃第八種藥，降血壓藥。"   # capped at 8
    assert med_clips.prompt_text("x" * 100, 1, "zh-TW").count("x") == med_clips.MAX_NAME


def test_the_key_follows_the_words_and_the_voice():
    a = med_clips.clip_key("請現在吃第一種藥，Adapalene。", "marin")
    assert a == med_clips.clip_key("請現在吃第一種藥，Adapalene。", "marin") and len(a) == 20
    assert a != med_clips.clip_key("請現在吃第一種藥，Adapalen。", "marin") != med_clips.clip_key("請現在吃第一種藥，Adapalene。", "coral")
    assert med_clips.voice_for(types.SimpleNamespace(voice="marin")) == "marin"
    assert med_clips.voice_for(types.SimpleNamespace(voice="robot")) == med_clips.DEFAULT_VOICE


def _streamed_wav(pcm: bytes, rate=24000, channels=1) -> bytes:
    """What the speech API sends: a WAV whose RIFF and data sizes are placeholders."""
    fmt = struct.pack("<HHIIHH", 1, channels, rate, rate * channels * 2, channels * 2, 16)
    return (b"RIFF" + struct.pack("<I", 0xFFFFFFFF) + b"WAVE" + b"fmt " + struct.pack("<I", 16) + fmt
            + b"data" + struct.pack("<I", 0xFFFFFFFF) + pcm)


def test_a_streamed_wav_gets_a_truthful_header_and_odd_bytes_are_dropped():
    pcm = struct.pack("<" + "h" * 2401, *([1000] * 2401)) + b"\x01"   # an odd trailing byte
    out = med_clips.normalize_wav(_streamed_wav(pcm))
    with wave.open(io.BytesIO(out)) as w:
        assert (w.getframerate(), w.getnchannels(), w.getsampwidth(), w.getnframes()) == (24000, 1, 2, 2401)
    with pytest.raises(ValueError):
        med_clips.normalize_wav(b"OggS" + b"\0" * 40)
    with pytest.raises(ValueError):
        med_clips.normalize_wav(_streamed_wav(b"\0" * 8).replace(struct.pack("<HH", 1, 1), struct.pack("<HH", 3, 1), 1))


@pytest.fixture
def api(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "MED_CLIP_DIR", tmp_path)
    w = types.SimpleNamespace(renders=[], name="Adapalene", target=types.SimpleNamespace(voice="marin", api_key="sk-x"),
                              fail=False)

    async def medicine_name(u_id, med_id):
        return w.name

    def render(text, voice, api_key):
        w.renders.append((text, voice, api_key))
        if w.fail:
            raise RuntimeError("502")
        return med_clips.normalize_wav(_streamed_wav(b"\0\0" * 240))

    monkeypatch.setattr(med_clips, "medicine_name", medicine_name)
    monkeypatch.setattr(med_clips, "render", render)
    monkeypatch.setattr(ai_settings, "live_target", lambda snapshot=None: w.target)
    med_clips._renders.clear()
    return w


def test_clip_renders_once_per_key_and_keeps_it_on_disk(api, tmp_path):
    first = asyncio.run(med_clips.clip(7, 10, 1, "zh-TW"))
    again = asyncio.run(med_clips.clip(7, 10, 1, "zh-TW"))
    second = asyncio.run(med_clips.clip(7, 10, 2, "zh-TW"))
    assert first is not None and again == first and second[1] != first[1]
    assert api.renders == [("請現在吃第一種藥，Adapalene。", "marin", "sk-x"), ("接下來，請吃第二種藥，Adapalene。", "marin", "sk-x")]
    assert sorted(p.name for p in (tmp_path / "7").iterdir()) == sorted([f"med_10_{first[1]}.wav", f"med_10_{second[1]}.wav"])
    with wave.open(io.BytesIO(first[0])) as w:
        assert w.getnframes() == 240
    api.name = "Adapalene 0.1%"                       # renamed: a new clip
    renamed = asyncio.run(med_clips.clip(7, 10, 1, "zh-TW"))
    assert renamed[1] != first[1] and len(api.renders) == 3


def test_no_clip_without_a_medicine_a_key_or_when_the_api_fails(api, caplog):
    api.name = None
    assert asyncio.run(med_clips.clip(7, 99, 1, "zh-TW")) is None
    api.name = "Adapalene"
    api.target = None
    assert asyncio.run(med_clips.clip(7, 10, 1, "zh-TW")) is None
    api.target = types.SimpleNamespace(voice="marin", api_key="")
    assert asyncio.run(med_clips.clip(7, 10, 1, "zh-TW")) is None
    api.target = types.SimpleNamespace(voice="marin", api_key="sk-x")
    api.fail = True
    assert asyncio.run(med_clips.clip(7, 10, 1, "zh-TW")) is None and api.renders[-1][2] == "sk-x"
    assert "could not be rendered" in caplog.text and "sk-x" not in caplog.text
