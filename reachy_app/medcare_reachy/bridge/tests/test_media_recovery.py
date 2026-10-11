"""ReachyRobot.reopen_camera() and media_status(): the camera watchdog's recovery (spec 2026-10-11 A2) through the
SDK's public release_media() and acquire_media() (reachy_mini 1.11.0, reachy_mini.py:203-240)."""

import time

import httpx
import numpy as np
import pytest

from medcare_reachy.bridge import media as media_module
from medcare_reachy.bridge.media import ReachyRobot

RELEASED = {"available": False, "released": True, "no_media": False}
AVAILABLE = {"available": True, "released": False, "no_media": False}


class Media:
    """One SDK media manager: hands out frames and microphone samples until it is closed."""

    def __init__(self, name):
        self.name, self.closed, self.recording, self.pushed = name, False, False, 0

    def get_frame(self):
        return None if self.closed else np.zeros((720, 1280, 3), np.uint8)

    def get_audio_sample(self):
        return None if self.closed else np.zeros((160, 2), np.float32)

    def get_input_audio_samplerate(self):
        return 16000

    def get_output_audio_samplerate(self):
        return 16000

    def start_playing(self):
        pass

    def push_audio_sample(self, samples):
        self.pushed += 1

    def start_recording(self):
        self.recording = True

    def close(self):
        self.closed = True


class MediaMini:
    """ReachyMini's media calls as SDK 1.11.0 makes them: release_media() closes the manager and marks it released
    (once); acquire_media() does nothing unless released, else asks the daemon and builds a fresh manager, or logs and
    stays released when the daemon refuses (`gives_back=False`)."""

    def __init__(self, gives_back=True, error=None):
        self.calls, self.gives_back, self.error = [], gives_back, error
        self.media_released = False
        self.managers = [Media("first")]

    @property
    def media(self):
        return self.managers[-1]

    def release_media(self):
        self.calls.append("release_media")
        if self.error is not None:
            raise self.error
        if self.media_released:
            return
        self.media.close()
        self.media_released = True

    def acquire_media(self):
        self.calls.append("acquire_media")
        if not self.media_released or not self.gives_back:
            return
        self.media.close()
        self.managers.append(Media(f"fresh {len(self.managers)}"))
        self.media_released = False


def robot_with(mini, status=RELEASED):
    robot = ReachyRobot.attach(mini)
    robot.media_status = lambda: status
    return robot


def test_a_camera_the_daemon_released_is_acquired_back_with_a_fresh_media_manager():
    mini = MediaMini()
    robot = robot_with(mini, RELEASED)
    assert robot.reopen_camera() == "acquired"
    assert mini.calls == ["release_media", "acquire_media"]
    assert mini.managers[0].closed and mini.media.name == "fresh 1"
    assert mini.media.recording                       # the microphone's pipeline runs again
    assert robot.get_camera_frame() is not None       # read from the fresh manager


def test_a_stale_reader_of_a_daemon_that_has_its_media_is_reopened():
    mini = MediaMini()
    robot = robot_with(mini, AVAILABLE)
    assert robot.reopen_camera() == "reopened"
    assert mini.calls == ["release_media", "acquire_media"] and mini.media.name == "fresh 1"


def test_a_refused_acquire_fails_and_the_next_attempt_only_asks_again():
    mini = MediaMini(gives_back=False)
    robot = robot_with(mini, RELEASED)
    assert robot.reopen_camera() == "failed: the daemon did not give the media back"
    assert mini.media_released
    # Meanwhile nothing reads the closed manager (the SDK would log a warning on every call).
    assert robot.get_camera_frame() is None and robot.get_audio() is None
    mini.gives_back = True
    assert robot.reopen_camera() == "acquired"
    assert mini.calls == ["release_media", "acquire_media", "acquire_media"]
    assert robot.get_camera_frame() is not None and robot.get_audio() is not None


def test_a_daemon_without_media_is_not_cycled():
    mini = MediaMini()
    robot = robot_with(mini, {"available": False, "released": False, "no_media": True})
    assert robot.reopen_camera() == "failed: the daemon runs without media"
    assert mini.calls == []


def test_an_sdk_error_is_a_failed_attempt_never_an_exception(caplog):
    mini = MediaMini(error=RuntimeError("daemon unreachable"))
    robot = robot_with(mini, None)
    with caplog.at_level("ERROR", logger="medcare_reachy.bridge.media"):
        assert robot.reopen_camera() == "failed: RuntimeError('daemon unreachable')"
    assert any("camera recovery failed" in r.getMessage() for r in caplog.records)


def test_no_connection_cannot_reopen():
    robot = ReachyRobot("192.168.1.50")
    assert robot.reopen_camera() == "failed: no robot connection"


def test_a_recovery_waits_for_the_clip_still_playing():
    mini = MediaMini()
    robot = robot_with(mini, AVAILABLE)
    robot._quiet_at = time.monotonic() + 0.3      # a clip still playing for 0.3 s
    began = time.perf_counter()                   # monotonic() ticks every 15.6 ms on Windows
    assert robot.reopen_camera() == "reopened"
    assert time.perf_counter() - began >= 0.25


def test_a_clip_after_a_recovery_plays_on_the_fresh_manager(tmp_path):
    import wave

    path = tmp_path / "thanks.wav"
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(np.zeros(1600, "<i2").tobytes())
    mini = MediaMini()
    robot = robot_with(mini, RELEASED)
    robot.reopen_camera()
    assert robot.play_clip(path, wait=False) is True
    assert mini.media.pushed > 0 and mini.managers[0].pushed == 0
    mini.media_released = True                                    # a recovery between its release and its acquire
    assert robot.play_clip(path, wait=False) is False


class Response:
    def __init__(self, status=200, body=None):
        self.status_code, self.body = status, body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("error", request=None, response=None)

    def json(self):
        if isinstance(self.body, Exception):
            raise self.body
        return self.body


@pytest.mark.parametrize("answer, expected", [
    (Response(200, RELEASED), RELEASED),
    (Response(503, {"detail": "Backend not running"}), None),
    (Response(200, ValueError("not JSON")), None),
    (Response(200, ["not", "a", "dict"]), None),
    (httpx.ConnectError("refused"), None),
])
def test_media_status_reads_the_local_daemon_within_2_s(monkeypatch, answer, expected):
    seen = []

    def get(url, timeout):
        seen.append((url, timeout))
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(media_module.httpx, "get", get)
    assert ReachyRobot.attach(MediaMini()).media_status() == expected
    assert seen == [("http://127.0.0.1:8000/api/media/status", 2.0)]
