"""Speech-to-text on the robot: "I finished" while a dose is watched, and the patient's words in a check-in.

The slot session turns listening on only while a dose is watched (robot_microphone consent) or during a check-in
conversation (all check-in consents), and mutes it while the robot speaks. Audio stays in memory: silero VAD cuts
speech segments and SenseVoice-Small turns each into text. In "done" mode only whether a "finished" phrase was heard
leaves this module; in "chat" mode the transcript is handed to the session, which sends that text (never audio) to
the server. Nothing is stored or logged here.
"""

import logging
import threading
import time
from pathlib import Path

import numpy as np

from medcare_reachy.bridge.media import resample

log = logging.getLogger(__name__)

STT_DIR = Path.home() / ".medcare_reachy" / "models" / "stt"
STT_FILES = ("model.int8.onnx", "tokens.txt", "silero_vad.onnx")
SAMPLE_RATE = 16000
VAD_WINDOW = 512                 # silero VAD's window at 16 kHz
MUTE_TAIL_SECONDS = 0.8          # the speaker's echo dies down after a clip ends
UTTERANCE_GAP = 0.7              # chat: hand over what was said once the patient has paused this long
MAX_QUEUED_SEGMENTS = 8
MODES = ("done", "chat")
IDLE_SLEEP = 0.02
LANGUAGE_CODES = {"zh-TW": "zh", "en": "en"}

# Substrings of what SenseVoice writes (Simplified or Traditional Chinese, or English) when someone says they
# have taken it. Any negation voids the match: "還沒吃完" is not "吃完".
DONE_WORDS = ("吃完", "吃好", "吃了", "吃过", "吃過", "吞了", "吞下", "服用了", "好了", "完成",
              "finished", "done", "took it", "taken")
NEGATIONS = ("没", "沒", "不", "未", "not", "n't", "haven", "didn")


def done_phrase(text: str | None) -> str | None:
    """The "finished" word heard in a transcript, or None."""
    text = (text or "").strip().lower()
    if not text or any(word in text for word in NEGATIONS):
        return None
    return next((word for word in DONE_WORDS if word in text), None)


def to_mono_16k(samples, rate: int) -> np.ndarray:
    samples = np.asarray(samples, dtype=np.float32)
    if samples.ndim == 2:
        samples = samples.mean(axis=1) if samples.shape[1] <= 8 else samples.mean(axis=0)
    if rate == SAMPLE_RATE:
        return samples
    if rate % SAMPLE_RATE == 0:
        factor = rate // SAMPLE_RATE   # 48 kHz: average each 3 samples (low-pass, then decimate)
        usable = len(samples) // factor * factor
        return samples[:usable].reshape(-1, factor).mean(axis=1)
    return resample(samples, rate, SAMPLE_RATE)


_recognizers: dict = {}
_recognizers_lock = threading.Lock()


def _recognizer(models_dir: Path, language: str):
    """SenseVoice takes ~10 s to load on the Pi; keep one per process so a bridge restart reuses it."""
    import sherpa_onnx

    key = (str(models_dir), language)
    with _recognizers_lock:
        if key not in _recognizers:
            _recognizers[key] = sherpa_onnx.OfflineRecognizer.from_sense_voice(
                model=str(models_dir / "model.int8.onnx"), tokens=str(models_dir / "tokens.txt"),
                language=LANGUAGE_CODES.get(language, "auto"), use_itn=True, num_threads=2)
        return _recognizers[key]


def _sherpa_models(models_dir: Path, language: str):
    import sherpa_onnx

    recognizer = _recognizer(models_dir, language)
    config = sherpa_onnx.VadModelConfig()
    config.silero_vad.model = str(models_dir / "silero_vad.onnx")
    config.silero_vad.min_silence_duration = 0.6
    config.silero_vad.min_speech_duration = 0.25
    config.silero_vad.max_speech_duration = 8.0
    config.sample_rate = SAMPLE_RATE
    vad = sherpa_onnx.VoiceActivityDetector(config, buffer_size_in_seconds=30)

    def transcribe(segment: np.ndarray) -> str:
        stream = recognizer.create_stream()
        stream.accept_waveform(SAMPLE_RATE, segment)
        recognizer.decode_stream(stream)
        return stream.result.text

    return vad, transcribe


class DoneListener:
    """Background thread pulling the robot's microphone.

    "done" mode: `heard_since()` reports a "finished" phrase. "chat" mode: `take_utterance()` returns what the
    patient said, once they pause.
    """

    def __init__(self, robot, language: str = "zh-TW", models_dir: Path = STT_DIR, *, clock=time.monotonic,
                 load=_sherpa_models):
        self.robot, self.language, self.models_dir = robot, language, Path(models_dir)
        self.clock, self._load = clock, load
        missing = [name for name in STT_FILES if not (self.models_dir / name).is_file()]
        self.available = not missing
        if missing:
            log.warning("listening for 'I finished' is off: missing %s in %s", ", ".join(missing), self.models_dir)
        self._lock = threading.Lock()
        self._active = False
        self._holds = 0
        self._muted_until = 0.0
        self._heard: tuple[float, str] | None = None
        self._mode = "done"
        self._segments: list[tuple[float, str]] = []
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ── control (called from the slot session) ──
    def start(self) -> None:
        if self.available and self._thread is None:
            self._thread = threading.Thread(target=self._run, name="medcare-voice", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(5)
            self._thread = None

    def set_active(self, active: bool, mode: str = "done") -> None:
        if mode not in MODES:
            raise ValueError(f"unknown listening mode {mode}")
        with self._lock:
            if (active and not self._active) or mode != self._mode:
                self._heard = None
                self._segments = []
            self._mode = mode
            self._active = bool(active) and self.available

    def hold(self) -> None:
        with self._lock:
            self._holds += 1

    def release(self) -> None:
        with self._lock:
            self._holds = max(0, self._holds - 1)
            self._muted_until = self.clock() + MUTE_TAIL_SECONDS

    def heard_since(self, since: float) -> str | None:
        with self._lock:
            heard = self._heard
        return heard[1] if heard and heard[0] >= since else None

    def take_utterance(self) -> str | None:
        """Chat mode: everything said since the last call, once the patient has paused UTTERANCE_GAP seconds."""
        with self._lock:
            if not self._segments or self.clock() - self._segments[-1][0] < UTTERANCE_GAP:
                return None
            text = " ".join(segment for _, segment in self._segments).strip()
            self._segments = []
        return text or None

    # ── worker ──
    def _listening(self) -> bool:
        with self._lock:
            return self._active and self._holds == 0 and self.clock() >= self._muted_until

    def _run(self) -> None:
        try:
            vad, transcribe = self._load(self.models_dir, self.language)
        except Exception:
            log.exception("speech models failed to load; listening for 'I finished' is off")
            self.available = False
            return
        pending = np.zeros(0, dtype=np.float32)
        dirty = False
        while not self._stop.is_set():
            chunk = self.robot.get_audio()
            if not self._listening():
                if dirty:   # forget half-heard speech from before a pause or from our own prompt
                    vad.reset()
                    pending, dirty = np.zeros(0, dtype=np.float32), False
                if chunk is None:
                    time.sleep(IDLE_SLEEP)
                continue
            if chunk is None:
                time.sleep(IDLE_SLEEP)
                continue
            pending = np.concatenate([pending, to_mono_16k(*chunk)])
            dirty = True
            while len(pending) >= VAD_WINDOW:
                vad.accept_waveform(pending[:VAD_WINDOW])
                pending = pending[VAD_WINDOW:]
            while not vad.empty():
                segment = np.asarray(vad.front.samples, dtype=np.float32)
                vad.pop()
                text = transcribe(segment)
                if not self._listening():
                    continue
                with self._lock:
                    if self._mode == "chat":
                        if text and text.strip():
                            self._segments = (self._segments + [(self.clock(), text.strip())])[-MAX_QUEUED_SEGMENTS:]
                    elif (word := done_phrase(text)) is not None:
                        self._heard = (self.clock(), word)
