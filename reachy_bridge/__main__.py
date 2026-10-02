"""python -m reachy_bridge"""

import asyncio
import logging
import signal

from reachy_bridge.app_client import AppClient
from reachy_bridge.clips import ClipPlayer
from reachy_bridge.config import Settings
from reachy_bridge.runner import MonitorStream, Runner

EMOTION_MODEL = "emotion_seed43.onnx"   # plus emotion_seed43_metadata.json; optional

log = logging.getLogger("reachy_bridge")


def make_robot(settings: Settings):
    from reachy_bridge.media import ReachyRobot, VideoFileRobot

    if settings.robot_backend == "video":
        return VideoFileRobot(settings.video_path)
    robot = ReachyRobot(settings.robot_host)
    if not robot.is_reachable():   # connects; a task will be aborted as robot_offline until it is
        log.warning("Reachy Mini at %s is not reachable yet", settings.robot_host)
    return robot


async def main() -> None:
    settings = Settings.from_env()
    from reachy_bridge.vision import VisionEngine   # imports onnxruntime

    robot = make_robot(settings)
    engine = VisionEngine(settings.models_dir)
    emotion = None
    if (settings.models_dir / EMOTION_MODEL).is_file():
        from reachy_bridge.emotion import EmotionEngine

        emotion = EmotionEngine(settings.models_dir / EMOTION_MODEL)
    else:
        log.warning("no %s in %s: emotion is not scored", EMOTION_MODEL, settings.models_dir)
    clips = ClipPlayer(robot, settings.clips_dir, settings.language)
    clips.audit()
    async with AppClient(settings.app_url, settings.device_token) as app:
        runner = Runner(app=app, robot=robot, clips=clips, stream=MonitorStream(app, robot, engine, emotion=emotion))
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, runner.request_shutdown)
            except (NotImplementedError, RuntimeError):
                pass   # Windows: Ctrl+C raises KeyboardInterrupt instead
        try:
            await runner.run()
        finally:
            engine.close()
            if hasattr(robot, "close"):
                robot.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
