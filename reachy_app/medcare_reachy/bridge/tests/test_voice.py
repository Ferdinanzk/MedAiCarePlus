"""DoneListener: phrase matching, audio conversion, and the listening thread with fake speech models."""

import time

import numpy as np
import pytest

from medcare_reachy.bridge.voice import STT_FILES, VAD_WINDOW, DoneListener, done_phrase, to_mono_16k


@pytest.mark.parametrize("text,word", [
    ("我吃完了。", "吃完"), ("我吃完了", "吃完"), ("吃好了", "吃好"), ("藥吃了", "吃了"), ("已經吃過了", "吃過"),
    ("I'm finished.", "finished"), ("OK, I took it", "took it"),
    ("我還沒吃完", None), ("还没吃", None), ("I haven't finished", None), ("not yet", None),
    ("今天天氣很好", None), ("", None), (None, None),
])
def test_done_phrase(text, word):
    assert done_phrase(text) == word


def test_audio_is_mixed_to_mono_and_decimated_to_16k():
    stereo = np.ones((4800, 2), np.float32) * np.array([0.2, 0.4], np.float32)
    mono = to_mono_16k(stereo, 48000)
    assert mono.shape == (1600,) and np.allclose(mono, 0.3)
    assert to_mono_16k(np.zeros(160, np.float32), 16000).shape == (160,)
    assert len(to_mono_16k(np.zeros(441, np.float32), 44100)) == 160


class Mic:
    def __init__(self):
        self.chunks = []

    def get_audio(self):
        return (self.chunks.pop(0), 16000) if self.chunks else None


class FakeVad:
    """Emits one speech segment for every four windows it is fed."""

    def __init__(self):
        self.fed, self.segments, self.resets = 0, [], 0

    def accept_waveform(self, samples):
        assert len(samples) == VAD_WINDOW
        self.fed += 1
        if self.fed % 4 == 0:
            self.segments.append(type("Segment", (), {"samples": np.zeros(VAD_WINDOW * 4, np.float32)})())

    def empty(self):
        return not self.segments

    @property
    def front(self):
        return self.segments[0]

    def pop(self):
        self.segments.pop(0)

    def reset(self):
        self.resets += 1


def listener(tmp_path, text="我吃完了"):
    for name in STT_FILES:
        (tmp_path / name).write_bytes(b"x")
    mic, vad = Mic(), FakeVad()
    heard_texts = []

    def transcribe(segment):
        heard_texts.append(len(segment))
        return text

    voice = DoneListener(mic, "zh-TW", tmp_path, load=lambda models_dir, language: (vad, transcribe))
    return voice, mic, vad, heard_texts


def wait_for(predicate, timeout=2.0):
    end = time.time() + timeout
    while time.time() < end:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def test_hears_finished_only_while_active(tmp_path):
    voice, mic, vad, texts = listener(tmp_path)
    voice.start()
    try:
        mic.chunks = [np.zeros(VAD_WINDOW * 4, np.float32)]
        assert wait_for(lambda: not mic.chunks)
        time.sleep(0.05)
        assert texts == [] and voice.heard_since(0) is None   # inactive: audio is dropped unheard
        voice.set_active(True)
        start = time.monotonic()
        mic.chunks = [np.zeros(VAD_WINDOW * 2, np.float32), np.zeros(VAD_WINDOW * 2, np.float32)]
        assert wait_for(lambda: voice.heard_since(start) == "吃完")
        assert voice.heard_since(time.monotonic() + 10) is None
        voice.set_active(False)
        voice.set_active(True)
        assert voice.heard_since(start) is None   # a new listening period forgets the old claim
    finally:
        voice.stop()


def test_muted_while_the_robot_speaks(tmp_path):
    voice, mic, vad, texts = listener(tmp_path)
    voice.start()
    try:
        voice.set_active(True)
        voice.hold()
        mic.chunks = [np.zeros(VAD_WINDOW * 4, np.float32)]
        assert wait_for(lambda: not mic.chunks)
        time.sleep(0.05)
        assert texts == [] and voice.heard_since(0) is None
        voice.release()
        assert voice.heard_since(0) is None
    finally:
        voice.stop()


def test_negated_speech_is_not_a_claim(tmp_path):
    voice, mic, vad, texts = listener(tmp_path, text="我還沒吃完")
    voice.start()
    try:
        voice.set_active(True)
        mic.chunks = [np.zeros(VAD_WINDOW * 4, np.float32)]
        assert wait_for(lambda: texts)
        time.sleep(0.05)
        assert voice.heard_since(0) is None
    finally:
        voice.stop()


def test_chat_mode_hands_over_what_was_said_after_a_pause(tmp_path):
    clock = [100.0]
    for name in STT_FILES:
        (tmp_path / name).write_bytes(b"x")
    mic, vad = Mic(), FakeVad()
    words = iter(["我早上", "去散步了"])
    voice = DoneListener(mic, "zh-TW", tmp_path, clock=lambda: clock[0],
                         load=lambda models_dir, language: (vad, lambda segment: next(words)))
    voice.start()
    try:
        voice.set_active(True, mode="chat")
        mic.chunks = [np.zeros(VAD_WINDOW * 4, np.float32), np.zeros(VAD_WINDOW * 4, np.float32)]
        assert wait_for(lambda: not mic.chunks and vad.fed == 8)
        time.sleep(0.05)
        assert voice.take_utterance() is None            # the patient may still be talking
        clock[0] += 2
        assert voice.take_utterance() == "我早上 去散步了"
        assert voice.take_utterance() is None
        assert voice.heard_since(0) is None               # chat words are never treated as "finished"
    finally:
        voice.stop()


def test_switching_mode_forgets_what_was_heard(tmp_path):
    voice, mic, vad, texts = listener(tmp_path)
    voice.start()
    try:
        voice.set_active(True, mode="chat")
        mic.chunks = [np.zeros(VAD_WINDOW * 4, np.float32)]
        assert wait_for(lambda: texts)
        time.sleep(0.05)
        voice.set_active(True, mode="done")
        assert voice.take_utterance() is None
    finally:
        voice.stop()


def test_missing_models_turn_listening_off(tmp_path):
    voice = DoneListener(Mic(), "zh-TW", tmp_path)
    assert voice.available is False
    voice.start()
    voice.set_active(True)
    assert voice._thread is None and voice._listening() is False
