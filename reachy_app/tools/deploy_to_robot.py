"""Copy this working copy's medcare_reachy package onto the robot and switch it to server-side vision.

Interim deployment until the Hugging Face space is updated: reinstalling the app from the dashboard puts the
space's version back. The installed package is backed up first (~/.medcare_reachy/backup-<time>.tgz).

    python tools/deploy_to_robot.py [--host reachy-mini.local] [--user pollen]

The SSH password comes from REACHY_SSH_PASSWORD, or is asked for.
"""

import argparse
import getpass
import os
import posixpath
import sys
import time
from pathlib import Path

import paramiko

ROOT = Path(__file__).resolve().parent.parent
PACKAGE = ROOT / "medcare_reachy"
REMOTE_PKG = "/venvs/apps_venv/lib/python3.12/site-packages/medcare_reachy"
PYTHON = "/venvs/apps_venv/bin/python"
SKIP_DIRS = {"__pycache__", "vision_models"}
VAD_URL = "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/silero_vad.onnx"

# The Matcha zh-baker voice reads Simplified Chinese; this is the manifest's zh-TW say_when_done text.
RENDER = r'''
import wave
from pathlib import Path
import numpy as np
import sherpa_onnx

M = Path.home() / ".medcare_reachy" / "models"
T = M / "tts" / "matcha-icefall-zh-baker"
OUT = Path.home() / ".medcare_reachy" / "clips" / "zh-TW" / "say_when_done.wav"
TEXT = "吃完药以后，请跟我说：我吃完了。"
WANT = "吃完药以后请跟我说我吃完了"

fsts = ",".join(str(T / f) for f in ("phone.fst", "date.fst", "number.fst") if (T / f).is_file())
tts = sherpa_onnx.OfflineTts(sherpa_onnx.OfflineTtsConfig(
    model=sherpa_onnx.OfflineTtsModelConfig(matcha=sherpa_onnx.OfflineTtsMatchaModelConfig(
        acoustic_model=str(T / "model-steps-3.onnx"), vocoder=str(M / "tts" / "vocos-22khz-univ.onnx"),
        lexicon=str(T / "lexicon.txt"), tokens=str(T / "tokens.txt")), num_threads=2),
    rule_fsts=fsts, max_num_sentences=1))
asr = sherpa_onnx.OfflineRecognizer.from_sense_voice(
    model=str(M / "stt" / "model.int8.onnx"), tokens=str(M / "stt" / "tokens.txt"),
    language="zh", use_itn=True, num_threads=2)
for speed in (0.85, 0.8, 0.9):
    audio = tts.generate(TEXT, sid=0, speed=speed)
    samples = np.asarray(audio.samples, dtype=np.float32)
    stream = asr.create_stream()
    stream.accept_waveform(audio.sample_rate, samples)
    asr.decode_stream(stream)
    got = "".join(ch for ch in stream.result.text if ch.isalnum())
    print(f"render speed={speed} heard={stream.result.text!r} ok={got == WANT}")
    if got == WANT:
        break
OUT.parent.mkdir(parents=True, exist_ok=True)
with wave.open(str(OUT), "wb") as w:
    w.setnchannels(1)
    w.setsampwidth(2)
    w.setframerate(audio.sample_rate)
    w.writeframes((np.clip(samples, -1, 1) * 32767).astype("<i2").tobytes())
print("wrote", OUT)
'''


def run(client, command: str, timeout: float = 120) -> str:
    _, out, err = client.exec_command(command, timeout=timeout)
    text = out.read().decode(errors="replace")
    status = out.channel.recv_exit_status()
    error = err.read().decode(errors="replace")
    if status != 0:
        raise RuntimeError(f"`{command}` failed ({status}): {error or text}")
    return text


def upload_package(client) -> int:
    sftp = client.open_sftp()
    count = 0
    try:
        for local in sorted(PACKAGE.rglob("*")):
            relative = local.relative_to(PACKAGE)
            if any(part in SKIP_DIRS for part in relative.parts) or local.is_dir():
                continue
            remote = posixpath.join(REMOTE_PKG, *relative.parts)
            run(client, f"mkdir -p '{posixpath.dirname(remote)}'")
            sftp.put(str(local), remote)
            count += 1
    finally:
        sftp.close()
    return count


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--host", default="reachy-mini.local")
    parser.add_argument("--user", default="pollen")
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")   # the clip check prints Chinese
    password =os.environ.get("REACHY_SSH_PASSWORD") or getpass.getpass(f"SSH password for {args.user}@{args.host}: ")

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(args.host, username=args.user, password=password, timeout=15)
    try:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        run(client, f"mkdir -p ~/.medcare_reachy && tar -czf ~/.medcare_reachy/backup-{stamp}.tgz "
                    f"-C {posixpath.dirname(REMOTE_PKG)} --exclude=vision_models --exclude=__pycache__ medcare_reachy")
        print(f"backed up the installed app to ~/.medcare_reachy/backup-{stamp}.tgz")

        print(f"uploaded {upload_package(client)} files")
        run(client, f"find {REMOTE_PKG} -name __pycache__ -type d -prune -exec rm -rf {{}} +")
        run(client, f"C={REMOTE_PKG}/bridge/clips; [ -e $C/zh-TW ] || ln -s ~/.medcare_reachy/clips/zh-TW $C/zh-TW")

        run(client, f"mkdir -p ~/.medcare_reachy/models/stt && cd ~/.medcare_reachy/models/stt && "
                    f"([ -s silero_vad.onnx ] || curl -sSL -o silero_vad.onnx {VAD_URL}) && ls -la silero_vad.onnx")
        print("voice activity model ready")

        sftp = client.open_sftp()
        with sftp.file("/tmp/render_say_when_done.py", "w") as handle:
            handle.write(RENDER)
        sftp.close()
        print(run(client, f"nice -n 15 {PYTHON} /tmp/render_say_when_done.py 2>&1 | grep -E '^(render|wrote)'",
                  timeout=600).strip())

        update = '{"capture_fps": 15, "vision_on_server": true}'
        answer = run(client, f"curl -s -m 30 -X POST localhost:8042/api/config "
                             f"-H 'Content-Type: application/json' -d '{update}' || true")
        if not answer.strip():
            print("the medcare_reachy app is not running: start it in Reachy Mini Control, then rerun this script")
            return 1
        print("settings:", answer.strip()[:300])
        time.sleep(10)
        print("status:", run(client, "curl -s -m 10 localhost:8042/api/status").strip())
    finally:
        client.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
