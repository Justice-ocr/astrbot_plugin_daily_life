import asyncio
import base64
import tempfile
import time
import types
import unittest
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

from support import DailyLifeRuntime, LifeSettings, Provider
from core.sight.animated import is_animated_gif, prepare_animated_image
from PIL import Image


def gif_bytes(colors, durations=100):
    frames = [Image.new("RGB", (80, 60), color) for color in colors]
    output = BytesIO()
    frames[0].save(
        output, format="GIF", save_all=True, append_images=frames[1:],
        duration=durations, loop=0,
    )
    return output.getvalue()


class AnimatedImageTest(unittest.TestCase):
    def test_single_frame_gif_is_static_and_keeps_original_resolution(self):
        data = gif_bytes(["red"])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "static.gif"
            path.write_bytes(data)
            for source in (
                str(path), path.as_uri(),
                "data:image/gif;base64," + base64.b64encode(data).decode("ascii"),
                "base64://" + base64.b64encode(data).decode("ascii"),
            ):
                with self.subTest(source_type=source.split(":", 1)[0]):
                    self.assertFalse(is_animated_gif(source))
                    prepared, hint = prepare_animated_image(source)
                    self.assertEqual(hint, "")
                    self.assertTrue(prepared.startswith("data:image/png;base64,"))
                    with Image.open(BytesIO(base64.b64decode(prepared.split(",", 1)[1]))) as image:
                        self.assertEqual(image.size, (80, 60))
                        self.assertEqual(image.getpixel((40, 30)), (255, 0, 0, 255))

    def test_animation_detection_uses_frames_instead_of_filename(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            animation = root / "animation.jpg"
            animation.write_bytes(gif_bytes(["red", "blue"]))
            self.assertTrue(is_animated_gif(str(animation)))
            for source in (str(root / "missing.gif"), "https://example.test/image.gif"):
                self.assertFalse(is_animated_gif(source))

    def test_mislabeled_gif_preserves_frame_order_and_timestamps(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "incoming.jpg"
            path.write_bytes(gif_bytes(["red", "green", "blue"], [100, 250, 500]))
            source, hint = prepare_animated_image(str(path))
            self.assertTrue(source.startswith("data:image/jpeg;base64,"))
            self.assertIn("原动图共 3 帧", hint)
            self.assertIn("0.85 秒", hint)
            with Image.open(BytesIO(base64.b64decode(source.split(",", 1)[1]))) as sheet:
                self.assertEqual(sheet.size, (272, 100))
                red, green, blue = [sheet.getpixel((48 + index * 88, 62)) for index in range(3)]
                self.assertGreater(red[0], 240)
                self.assertGreater(green[1], 110)
                self.assertGreater(blue[2], 240)

    def test_time_sampling_keeps_first_and_last_after_long_hold(self):
        data = gif_bytes([(i * 20, 0, 0) for i in range(10)], [10000] + [100] * 9)
        source, hint = prepare_animated_image(
            "data:image/gif;base64," + base64.b64encode(data).decode("ascii")
        )
        self.assertIn("2 个采样画面", hint)
        self.assertIn("原动图共 10 帧", hint)
        with Image.open(BytesIO(base64.b64decode(source.split(",", 1)[1]))) as sheet:
            self.assertLess(sheet.getpixel((48, 62))[0], 10)
            self.assertGreater(sheet.getpixel((136, 62))[0], 170)

    def test_static_images_and_remote_sources_remain_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "image.png"
            Image.new("RGB", (20, 20), "blue").save(path)
            for source in (str(path), "https://example.test/image.png"):
                self.assertEqual(prepare_animated_image(source), (source, ""))

    def test_file_uri_and_base64_sources_are_supported(self):
        data = gif_bytes(["red", "blue"])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "image with space.gif"
            path.write_bytes(data)
            for source in (path.as_uri(), "base64://" + base64.b64encode(data).decode("ascii")):
                prepared, hint = prepare_animated_image(source)
                self.assertTrue(prepared.startswith("data:image/jpeg;base64,"))
                self.assertIn("原动图共 2 帧", hint)

    def test_decode_limits_and_corrupt_gifs_fail_without_raw_fallback(self):
        data = gif_bytes(["red", "blue", "green"])
        inline = "data:image/gif;base64," + base64.b64encode(data).decode("ascii")
        for name, limit in (("MAX_SOURCE_BYTES", 10), ("MAX_SOURCE_PIXELS", 10),
                            ("MAX_SOURCE_FRAMES", 2), ("MAX_DECODED_PIXELS", 5000),
                            ("DECODE_TIMEOUT_SECONDS", -1)):
            with self.subTest(limit=name), patch(f"core.sight.animated.{name}", limit):
                with self.assertRaises(ValueError):
                    prepare_animated_image(inline)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "broken.gif"
            path.write_bytes(b"not-an-image")
            with self.assertRaises(ValueError):
                prepare_animated_image(str(path))

    def test_transparent_frames_are_composited_on_white(self):
        frames = [Image.new("RGBA", (80, 60), (0, 0, 0, 0)) for _ in range(2)]
        frames[0].paste("red", (20, 20, 40, 40))
        frames[1].paste("blue", (20, 20, 40, 40))
        output = BytesIO()
        frames[0].save(output, format="GIF", save_all=True, append_images=frames[1:],
                       duration=100, disposal=2)
        source, _ = prepare_animated_image(
            "base64://" + base64.b64encode(output.getvalue()).decode("ascii")
        )
        with Image.open(BytesIO(base64.b64decode(source.split(",", 1)[1]))) as sheet:
            self.assertTrue(all(channel > 245 for channel in sheet.getpixel((12, 36))))
            self.assertGreater(sheet.getpixel((126, 62))[2], 230)


class AnimatedVisionDeadlineTest(unittest.IsolatedAsyncioTestCase):
    def runtime(self, primary, fallback=None):
        runtime = DailyLifeRuntime.__new__(DailyLifeRuntime)
        runtime.config = LifeSettings.from_dict({"vision_config": {"provider": "primary"}})
        closed = []

        class Composer:
            async def _get_provider(self, provider_id=""):
                return primary if provider_id else (fallback or primary)

            async def _cleanup_conversation(self, session_id):
                closed.append(session_id)

        runtime.composer = Composer()
        return runtime, closed

    async def test_slow_primary_is_cancelled_and_fallback_finishes_in_budget(self):
        cancelled = asyncio.Event()

        class SlowProvider(Provider):
            async def text_chat(self, **kwargs):
                try:
                    await asyncio.sleep(10)
                finally:
                    cancelled.set()

        fallback = Provider(['{"summary":"猫挥手"}'], provider_id="fallback")
        runtime, closed = self.runtime(SlowProvider([], provider_id="primary"), fallback)
        runtime._EMOJI_VISION_PROVIDER_TIMEOUT_SECONDS = 0.02
        runtime._EMOJI_VISION_TIMEOUT_SECONDS = 0.2
        result = await runtime._call_emoji_vision_payload(
            "识别图片", "https://example.test/image.png",
            session_prefix="test", task_label="测试视觉",
        )
        self.assertEqual(result["summary"], "猫挥手")
        self.assertTrue(cancelled.is_set())
        self.assertEqual(len(closed), 2)

    async def test_total_timeout_cancels_request_and_closes_session(self):
        cancelled = asyncio.Event()

        class SlowProvider(Provider):
            async def text_chat(self, **kwargs):
                try:
                    await asyncio.sleep(10)
                finally:
                    cancelled.set()

        runtime, closed = self.runtime(SlowProvider([], provider_id="primary"))
        # 留给请求进入 provider 的时间，避免繁忙测试进程先在准备阶段超时。
        runtime._EMOJI_VISION_TIMEOUT_SECONDS = 0.5
        runtime._EMOJI_VISION_PROVIDER_TIMEOUT_SECONDS = 1
        with self.assertRaises(TimeoutError):
            await runtime._call_emoji_vision_payload(
                "识别图片", "https://example.test/image.png",
                session_prefix="test", task_label="测试视觉",
            )
        self.assertTrue(cancelled.is_set())
        self.assertEqual(len(closed), 1)

    async def test_decode_runs_off_event_loop_and_supported_retry_limit_is_passed(self):
        requests = []

        class ExplicitProvider(Provider):
            async def text_chat(self, prompt, image_urls, session_id, request_max_retries=None):
                requests.append((prompt, image_urls, request_max_retries))
                return types.SimpleNamespace(completion_text='{"summary":"多帧动作"}')

        runtime, _ = self.runtime(ExplicitProvider([], provider_id="primary"))
        ticks = []

        def decode(_source):
            time.sleep(0.05)
            return "data:image/jpeg;base64,dGVzdA==", "同一动图的多帧画面"

        async def heartbeat():
            for _ in range(5):
                await asyncio.sleep(0.005)
                ticks.append(1)

        with patch("core.runtime.capture.emoji.vision.prepare_animated_image", decode):
            result, _ = await asyncio.gather(runtime._call_emoji_vision_payload(
                "识别图片", "animation.gif", session_prefix="test", task_label="测试视觉"
            ), heartbeat())
        self.assertEqual(result["summary"], "多帧动作")
        self.assertEqual(len(ticks), 5)
        self.assertIn("同一动图的多帧画面", requests[0][0])
        self.assertEqual(requests[0][2], 1)
