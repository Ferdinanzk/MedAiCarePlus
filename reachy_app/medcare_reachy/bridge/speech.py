"""Reachy's voice for text that isn't a prerecorded clip (check-in replies), with Matcha-TTS on the robot.

The server sends replies already converted for this voice (`speech_text`, Simplified characters for the Mandarin
zh-baker voice). Audio is synthesised into a temporary WAV, played, and deleted.
"""

import logging
import queue
import os
import re
import tempfile
import threading
import wave
from pathlib import Path

import numpy as np

log = logging.getLogger(__name__)

TTS_DIR = Path.home() / ".medcare_reachy" / "models" / "tts"
MATCHA = "matcha-icefall-zh-baker"
TTS_FILES = (f"{MATCHA}/model-steps-3.onnx", f"{MATCHA}/lexicon.txt", f"{MATCHA}/tokens.txt", "vocos-22khz-univ.onnx")
SPEED = 0.85   # a little slower than default for older listeners

_engine = None
_engine_lock = threading.Lock()


def _matcha(models_dir: Path):
    import sherpa_onnx

    matcha = models_dir / MATCHA
    fsts = ",".join(str(matcha / f) for f in ("phone.fst", "date.fst", "number.fst") if (matcha / f).is_file())
    config = sherpa_onnx.OfflineTtsConfig(
        model=sherpa_onnx.OfflineTtsModelConfig(
            matcha=sherpa_onnx.OfflineTtsMatchaModelConfig(
                acoustic_model=str(matcha / "model-steps-3.onnx"), vocoder=str(models_dir / "vocos-22khz-univ.onnx"),
                lexicon=str(matcha / "lexicon.txt"), tokens=str(matcha / "tokens.txt")),
            num_threads=2),
        rule_fsts=fsts, max_num_sentences=2)
    return sherpa_onnx.OfflineTts(config)


MAX_CHUNK = 16   # characters per spoken chunk: short enough that the first one is ready in ~2 s


def chunks(text: str | None) -> list[str]:
    """Split a reply into sentences, and long sentences again at commas, keeping the punctuation."""
    out: list[str] = []
    for sentence in re.split(r"(?<=[。！？!?；;.])", (text or "").strip()):
        sentence = sentence.strip()
        if not sentence:
            continue
        if len(sentence) <= MAX_CHUNK:
            out.append(sentence)
            continue
        current = ""
        for part in re.split(r"(?<=[，,、])", sentence):
            if current and len(current) + len(part) > MAX_CHUNK:
                out.append(current.strip())
                current = part
            else:
                current += part
        if current.strip():
            out.append(current.strip())
    return out


def write_wav(path: Path, samples: np.ndarray, rate: int) -> None:
    pcm = (np.clip(np.asarray(samples, dtype=np.float32), -1.0, 1.0) * 32767).astype("<i2")
    with wave.open(str(path), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(rate)
        out.writeframes(pcm.tobytes())


class Speaker:
    def __init__(self, robot, models_dir: Path = TTS_DIR, *, load=_matcha):
        self.robot, self.models_dir, self._load = robot, Path(models_dir), load
        missing = [name for name in TTS_FILES if not (self.models_dir / name).is_file()]
        self.available = not missing
        if missing:
            log.warning("check-in voice is off: missing %s in %s", ", ".join(missing), self.models_dir)

    def _tts(self):
        global _engine
        with _engine_lock:   # one engine per process (~12 s to load on the Pi); reused across bridge restarts
            if _engine is None:
                _engine = self._load(self.models_dir)
            return _engine

    def preload(self) -> None:
        """Load the voice in the background so the first reply isn't delayed."""
        if self.available:
            threading.Thread(target=self._safe_load, name="medcare-tts-load", daemon=True).start()

    def _safe_load(self) -> None:
        try:
            self._tts()
        except Exception:
            log.exception("check-in voice failed to load")
            self.available = False

    def say(self, text: str) -> bool:
        """Speak `text` (blocking until playback ends). False when there's no voice or nothing to say.

        Synthesis on the Pi runs at roughly real time, so a whole reply would keep the patient waiting as long
        as it lasts. Instead each chunk is played while the next one is being synthesised.
        """
        pieces = chunks(text)
        if not pieces or not self.available:
            return False
        tts = self._tts()
        ready: queue.Queue = queue.Queue(maxsize=2)

        def produce() -> None:
            try:
                for piece in pieces:
                    ready.put(tts.generate(piece, sid=0, speed=SPEED))
            except Exception:
                log.exception("speech synthesis failed")
            finally:
                ready.put(None)

        threading.Thread(target=produce, name="medcare-tts", daemon=True).start()
        played = False
        while (audio := ready.get()) is not None:
            if len(audio.samples) == 0:
                continue
            handle, name = tempfile.mkstemp(prefix="medcare-say-", suffix=".wav")
            os.close(handle)
            path = Path(name)
            try:
                write_wav(path, audio.samples, audio.sample_rate)
                played = bool(self.robot.play_clip(path)) or played
            finally:
                path.unlink(missing_ok=True)
        return played
