# ruff: noqa: I001

import asyncio
import base64
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from support import LifeSettings
from PIL import Image

from core.media import GeminiImageService
from core.media.picture import openai, routes
from core.media.picture import polling as tasks
from core.media.picture.pipe import ImageRoute


class _Response:
    def __init__(self, status=200, payload=None, headers=None):
        self.status = status
        self.payload = payload
        self.headers = headers or {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def json(self):
        return self.payload

    async def text(self):
        return "test error"


class _Session:
    closed = False

    def __init__(self, posts, gets=()):
        self.posts = list(posts)
        self.gets = list(gets)
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        response = self.posts.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        response = self.gets.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def _route(timeout_seconds=300):
    return ImageRoute(
        api_url="https://api.scywl.cc/v1",
        api_key="test-key",
        model="gpt-image-2.5-flare",
        label="test group",
        protocol="openai",
        resolution="1K",
        aspect_ratio="9:16",
        timeout_seconds=timeout_seconds,
        origin="https://api.scywl.cc",
    )


def _request(route, reference=False):
    parts = [{"text": "green apple"}]
    if reference:
        parts.append(
            {
                "inlineData": {
                    "mimeType": "image/png",
                    "data": base64.b64encode(b"reference").decode("ascii"),
                }
            }
        )
    return openai.build_request(
        route, parts, resolution=route.resolution, aspect_ratio=route.aspect_ratio
    )


def _accepted(**kwargs):
    return _Response(
        202,
        {
            "task_id": "imgtask_test",
            "status": "processing",
            "poll_url": "https://untrusted.example/steal-key",
            **kwargs,
        },
    )


class ImageTaskTests(unittest.IsolatedAsyncioTestCase):
    async def test_submit_once_poll_same_key_and_return_url_for_b64_request(self):
        for reference in (False, True):
            with self.subTest(reference=reference):
                route = _route()
                request = _request(route, reference)
                session = _Session(
                    [_accepted()],
                    [
                        _Response(
                            payload={"status": "processing"},
                            headers={"Retry-After": "7"},
                        ),
                        _Response(
                            payload={
                                "status": "completed",
                                "http_status": 200,
                                "result": {
                                    "data": [{"url": "https://cdn.example/apple.png"}]
                                },
                            }
                        ),
                    ],
                )
                with patch.object(
                    tasks.asyncio, "sleep", new_callable=AsyncMock
                ) as sleep:
                    result = await tasks.request_image_task(session, route, request)
                self.assertIsInstance(result, tasks.ImageTaskResult)
                self.assertEqual(result.task_id, "imgtask_test")
                self.assertEqual(
                    result["data"][0]["url"], "https://cdn.example/apple.png"
                )
                self.assertEqual(len(session.calls), 3)
                endpoint = "edits" if reference else "generations"
                self.assertEqual(
                    session.calls[0][1],
                    f"https://api.scywl.cc/v1/images/{endpoint}/async",
                )
                submitted = session.calls[0][2]
                self.assertEqual(submitted["json"]["size"], "864x1536")
                self.assertEqual(submitted["json"]["response_format"], "b64_json")
                self.assertNotIn("quality", submitted["json"])
                self.assertEqual(submitted["timeout"].kwargs["total"], route.timeout_seconds)
                self.assertEqual(
                    submitted["headers"]["Idempotency-Key"],
                    submitted["headers"]["X-Client-Request-ID"],
                )
                for method, url, options in session.calls[1:]:
                    self.assertEqual(method, "GET")
                    self.assertEqual(
                        url, "https://api.scywl.cc/v1/images/tasks/imgtask_test"
                    )
                    self.assertEqual(
                        options["headers"], {"Authorization": "Bearer test-key"}
                    )
                    self.assertFalse(options["allow_redirects"])
                    self.assertEqual(options["timeout"].kwargs["total"], 30)
                self.assertEqual(
                    [call.args[0] for call in sleep.call_args_list], [3.0, 7.0]
                )

    async def test_disabled_async_endpoint_returns_control_without_polling(self):
        route = _route()
        session = _Session([_Response(404)])
        self.assertIsNone(
            await tasks.request_image_task(session, route, _request(route))
        )
        self.assertEqual(len(session.calls), 1)

    async def test_query_errors_keep_polling_original_task_without_resubmitting(self):
        route = _route()
        session = _Session(
            [_accepted()],
            [
                _Response(502),
                asyncio.TimeoutError(),
                _Response(
                    payload={
                        "status": "completed",
                        "http_status": 200,
                        "result": {"data": [{"url": "https://cdn.example/apple.png"}]},
                    }
                ),
            ],
        )
        with patch.object(tasks.asyncio, "sleep", new_callable=AsyncMock):
            result = await tasks.request_image_task(session, route, _request(route))
        self.assertEqual(result.task_id, "imgtask_test")
        self.assertEqual(sum(call[0] == "POST" for call in session.calls), 1)
        self.assertEqual(len({call[1] for call in session.calls[1:]}), 1)

    async def test_failed_task_is_not_mistaken_for_a_successful_http_lookup(self):
        route = _route()
        session = _Session(
            [_accepted()],
            [
                _Response(
                    payload={
                        "status": "failed",
                        "http_status": 502,
                        "error": {"message": "generation failed"},
                    }
                )
            ],
        )
        with patch.object(tasks.asyncio, "sleep", new_callable=AsyncMock):
            with self.assertRaisesRegex(tasks.ImageTaskError, "502.*generation failed"):
                await tasks.request_image_task(session, route, _request(route))
        self.assertEqual(len(session.calls), 2)

    async def test_timeout_preserves_accepted_task_id_and_never_submits_again(self):
        route = _route(timeout_seconds=0.01)
        session = _Session([_accepted()])
        with self.assertRaisesRegex(tasks.ImageTaskError, "imgtask_test.*避免重复生成") as error:
            await tasks.request_image_task(session, route, _request(route))
        self.assertEqual(error.exception.task_id, "imgtask_test")
        self.assertEqual(error.exception.phase, "查询")
        self.assertEqual(len(session.calls), 1)

    async def test_submission_timeout_has_idempotency_trace_and_no_resubmission(self):
        route = _route()
        request = _request(route)
        session = _Session([asyncio.TimeoutError()])
        with self.assertRaises(tasks.ImageTaskError) as error:
            await tasks.request_image_task(session, route, request)
        self.assertIn(request.headers["Idempotency-Key"], str(error.exception))
        self.assertEqual(error.exception.task_id, "")
        self.assertEqual(error.exception.request_id, request.headers["Idempotency-Key"])
        self.assertEqual(error.exception.phase, "提交")
        self.assertEqual(len(session.calls), 1)

    async def test_submission_error_log_exposes_phase_but_not_exception_body(self):
        route = _route()
        request = _request(route)
        session = _Session([asyncio.TimeoutError("Bearer private-key; private prompt")])
        with patch.object(tasks.logger, "warning") as warning:
            with self.assertRaises(tasks.ImageTaskError):
                await tasks.request_image_task(session, route, request)
        diagnostic = warning.call_args.args[0]
        self.assertIn("阶段=提交", diagnostic)
        self.assertIn("任务=未取得", diagnostic)
        self.assertIn("原因=TimeoutError", diagnostic)
        self.assertIn(request.headers["Idempotency-Key"], diagnostic)
        self.assertNotIn("private-key", diagnostic)
        self.assertNotIn("private prompt", diagnostic)

    async def test_missing_task_id_is_distinct_from_a_query_error(self):
        route = _route()
        for submission, gets, expected_id, phase in (
            (_Response(202, {}), [], "", "读取提交回执"),
            (_accepted(), [_Response(404)], "imgtask_test", "查询"),
        ):
            with self.subTest(phase=phase):
                with patch.object(tasks.asyncio, "sleep", new_callable=AsyncMock):
                    with self.assertRaises(tasks.ImageTaskError) as error:
                        await tasks.request_image_task(_Session([submission], gets), route, _request(route))
                self.assertEqual(error.exception.task_id, expected_id)
                self.assertEqual(error.exception.phase, phase)

    async def test_timeout_during_acceptance_waits_for_registration_to_finish(self):
        saved = []

        async def register(task_id, route):
            await asyncio.sleep(0.03)
            saved.append(task_id)

        session = _Session([_accepted()])
        with self.assertRaises(tasks.ImageTaskError) as error:
            await tasks.request_image_task(session, _route(0.01), _request(_route()), on_accepted=register)
        self.assertEqual(saved, ["imgtask_test"])
        self.assertEqual(error.exception.phase, "保存恢复任务")
        self.assertEqual(error.exception.task_id, "imgtask_test")
        self.assertEqual(len(session.calls), 1)

    async def test_invalid_accepted_responses_and_lookup_failures_stop_failover(self):
        route = _route()
        for submission, lookup in (
            (_Response(202, {}), []),
            (_Response(202, []), []),
            (_accepted(), [_Response(404)]),
            (_accepted(), [_Response(payload={"status": "completed", "result": {}})]),
            (
                _accepted(),
                [_Response(payload={"status": "completed", "http_status": 502})],
            ),
            (_accepted(), [_Response(payload={"status": "unknown"})]),
            (_Response(200, {"data": [{"url": "https://cdn.example/apple.png"}]}), []),
        ):
            with self.subTest(submission=submission.payload, lookup=lookup):
                session = _Session([submission], lookup)
                with patch.object(tasks.asyncio, "sleep", new_callable=AsyncMock):
                    with self.assertRaises(tasks.ImageTaskError):
                        await tasks.request_image_task(session, route, _request(route))
                self.assertEqual(sum(call[0] == "POST" for call in session.calls), 1)

    def _service(self, session, *, mode="text", async_backup=False):
        settings = LifeSettings.from_dict(
            {
                "image_generation_config": {
                    "enabled": True,
                    f"{mode}_channels": [
                        {
                            "__template_key": "openai",
                            "api_url": "https://api.scywl.cc/v1",
                            "api_key": "test-key",
                            "model": "gpt-image-2.5-flare",
                            "aspect_ratio": "9:16",
                        },
                        {
                            "__template_key": "openai",
                            "api_url": (
                                "https://api.scywl.cc/v1"
                                if async_backup
                                else "https://backup.example/v1"
                            ),
                            "api_key": "backup-key",
                            "model": "gpt-image-2.5-flare",
                        },
                    ],
                }
            }
        ).image_generation
        service = GeminiImageService(settings, Path(tempfile.mkdtemp()))
        service._get_session = AsyncMock(return_value=session)
        return service

    async def test_confirmed_task_failure_tries_next_channel_for_text_and_edit(self):
        for mode in ("text", "edit"):
            for async_backup in (False, True):
                with self.subTest(mode=mode, async_backup=async_backup):
                    result = {"data": [{"b64_json": base64.b64encode(b"image").decode()}]}
                    failed = _Response(payload={
                        "status": "failed", "http_status": 400,
                        "error": {"message": "当前系统过载"},
                    })
                    session = _Session(
                        [_accepted(), _accepted(task_id="imgtask_backup")]
                        if async_backup else [_accepted(), _Response(payload=result)],
                        [failed, _Response(payload={
                            "status": "completed", "result": result,
                        })] if async_backup else [failed],
                    )
                    service = self._service(session, mode=mode, async_backup=async_backup)
                    service._load_reference_image = AsyncMock(return_value=(b"reference", "image/png"))
                    accepted, on_failed = AsyncMock(), AsyncMock()
                    with service.track_async_tasks(accepted, on_failed=on_failed):
                        with patch.object(tasks.asyncio, "sleep", new_callable=AsyncMock):
                            generated = (
                                await service.edit_image("green apple", "reference.png")
                                if mode == "edit" else await service.generate_image("green apple")
                            )
                    self.assertEqual(generated.path.read_bytes(), b"image")
                    posts = [call for call in session.calls if call[0] == "POST"]
                    self.assertEqual(len(posts), 2)
                    self.assertEqual(on_failed.await_count, 1)
                    self.assertEqual(on_failed.await_args.args[0], "imgtask_test")
                    self.assertIsInstance(on_failed.await_args.args[2], tasks.ImageTaskFailed)
                    if async_backup:
                        self.assertNotEqual(posts[0][2]["headers"]["Idempotency-Key"], posts[1][2]["headers"]["Idempotency-Key"])
                        self.assertEqual(accepted.await_args.args[0], "imgtask_backup")
                    if mode == "edit":
                        self.assertTrue(all("/edits" in call[1] for call in posts))

    async def test_exhausted_confirmed_failures_are_terminal_and_bounded(self):
        session = _Session(
            [_accepted(), _accepted(task_id="imgtask_backup")],
            [_Response(payload={"status": "failed", "http_status": 400}) for _ in range(2)],
        )
        on_failed = AsyncMock()
        service = self._service(session, async_backup=True)
        with service.track_async_tasks(AsyncMock(), on_failed=on_failed):
            with patch.object(tasks.asyncio, "sleep", new_callable=AsyncMock):
                with self.assertRaises(tasks.ImageTaskFailed) as error:
                    await service.generate_image("green apple")
        self.assertEqual(on_failed.await_count, 2)
        self.assertEqual(error.exception.task_id, "imgtask_backup")
        self.assertTrue(error.exception.request_id)
        self.assertEqual(error.exception.phase, "查询")
        self.assertEqual([call[0] for call in session.calls], ["POST", "GET", "POST", "GET"])

    async def test_failure_receipt_error_blocks_new_submission(self):
        session = _Session([_accepted()], [_Response(payload={"status": "failed", "http_status": 400})])
        service = self._service(session)
        with service.track_async_tasks(AsyncMock(), on_failed=AsyncMock(side_effect=RuntimeError("租约丢失"))):
            with patch.object(tasks.asyncio, "sleep", new_callable=AsyncMock):
                with self.assertRaises(tasks.ImageTaskError) as error:
                    await service.generate_image("green apple")
        self.assertNotIsInstance(error.exception, tasks.ImageTaskFailed)
        self.assertEqual(sum(call[0] == "POST" for call in session.calls), 1)

    async def test_suite_metadata_preserves_failure_listener_and_request_isolation(self):
        service = self._service(_Session([]))
        accepted, failed = AsyncMock(), AsyncMock()
        with service.track_async_tasks(accepted, on_failed=failed):
            with service.async_task_metadata({"slot_index": 2}):
                await service._task_listener.get()("slot-task", _route())
                self.assertIs(service._task_failure_listener.get(), failed)
            accepted.assert_awaited_once()
            self.assertEqual(accepted.await_args.args[2], {"slot_index": 2})
        self.assertIsNone(service._task_failure_listener.get())
        self.assertIsNone(service._task_listener.get())

    async def test_resume_uses_original_credentials_after_channel_reorder(self):
        output = io.BytesIO()
        Image.new("RGB", (8, 8), "white").save(output, format="PNG")
        image = {"data": [{"b64_json": base64.b64encode(output.getvalue()).decode()}]}
        for original_mode in ("text", "edit"):
            with self.subTest(original_mode=original_mode):
                primary, original = _route(), _route()
                original.api_key = "original-group-key"
                reference = routes.task_reference(original)
                self.assertNotIn(original.api_key, json.dumps(reference))
                self.assertNotIn("api_key", reference)
                session = _Session([], [_Response(payload={"status": "completed", "result": image})])
                service = self._service(session)
                service._request_routes = AsyncMock(side_effect=lambda mode: (
                    [primary, original] if mode == original_mode else [primary]
                ))
                with patch.object(tasks.asyncio, "sleep", new_callable=AsyncMock):
                    generated = await service.resume_async_image("original-task", reference)
                self.assertEqual(generated.path.read_bytes(), output.getvalue())
                self.assertEqual(len(session.calls), 1)
                method, url, options = session.calls[0]
                self.assertEqual(method, "GET")
                self.assertTrue(url.endswith("/images/tasks/original-task"))
                self.assertEqual(options["headers"], {"Authorization": "Bearer original-group-key"})

    async def test_resume_never_guesses_credentials_for_ambiguous_or_removed_group(self):
        original = _route()
        original.api_key = "removed-group-key"
        reference = routes.task_reference(original)
        for credential_id in (None, reference["credential_id"]):
            with self.subTest(credential_id=credential_id):
                session = _Session([])
                service = self._service(session, async_backup=True)
                if credential_id is None:
                    reference.pop("credential_id")
                else:
                    reference["credential_id"] = credential_id
                with self.assertRaises(tasks.ImageTaskError):
                    await service.resume_async_image("original-task", reference)
                self.assertEqual(session.calls, [])
                service._get_session.assert_not_awaited()

    async def test_pending_task_does_not_generate_again_using_backup_channel(self):
        session = _Session([_accepted()], [_Response(404)])
        service = self._service(session)
        with patch.object(tasks.asyncio, "sleep", new_callable=AsyncMock):
            with self.assertRaises(tasks.ImageTaskError):
                await service.generate_image("green apple")
        self.assertEqual(sum(call[0] == "POST" for call in session.calls), 1)

    async def test_completed_task_download_failure_does_not_generate_again(self):
        session = _Session(
            [_accepted()],
            [
                _Response(
                    payload={
                        "status": "completed",
                        "http_status": 200,
                        "result": {"data": [{"url": "https://cdn.example/apple.png"}]},
                    }
                )
            ],
        )
        service = self._service(session)
        service._download_generated_image = AsyncMock(
            side_effect=ValueError("invalid image")
        )
        with patch.object(tasks.asyncio, "sleep", new_callable=AsyncMock):
            with self.assertRaisesRegex(tasks.ImageTaskError, "获取成品失败") as error:
                await service.generate_image("green apple")
        self.assertEqual(error.exception.task_id, "imgtask_test")
        self.assertTrue(error.exception.request_id)
        self.assertEqual(error.exception.phase, "下载成品")
        self.assertEqual(sum(call[0] == "POST" for call in session.calls), 1)

    async def test_async_404_falls_back_to_sync_with_same_idempotency_key(self):
        session = _Session(
            [
                _Response(404),
                _Response(
                    payload={
                        "data": [
                            {"b64_json": base64.b64encode(b"image").decode("ascii")}
                        ],
                    }
                ),
            ]
        )
        generated = await self._service(session).generate_image("green apple")
        self.assertEqual(generated.path.read_bytes(), b"image")
        self.assertEqual(
            [call[1] for call in session.calls],
            [
                "https://api.scywl.cc/v1/images/generations/async",
                "https://api.scywl.cc/v1/images/generations",
            ],
        )
        self.assertEqual(
            session.calls[0][2]["headers"]["Idempotency-Key"],
            session.calls[1][2]["headers"]["Idempotency-Key"],
        )

    def test_retry_after_invalid_values_and_http_date(self):
        for value in ("", "nan", "inf", "-5", "invalid", "1"):
            self.assertEqual(tasks._poll_delay({"Retry-After": value}), 3.0)
        with patch.object(tasks.time, "time", return_value=0):
            self.assertEqual(
                tasks._poll_delay({"Retry-After": "Thu, 01 Jan 1970 00:00:07 GMT"}), 7.0
            )

    def test_async_protocol_is_limited_to_documented_models_and_hosts(self):
        route = _route()
        self.assertTrue(openai.uses_async_tasks(route))
        route.api_url = "https://api.scywl.cc.untrusted.example/v1"
        self.assertFalse(openai.uses_async_tasks(route))
        route.api_url = "https://api.scywl.cc/v1"
        route.model = "other-image-model"
        self.assertFalse(openai.uses_async_tasks(route))
