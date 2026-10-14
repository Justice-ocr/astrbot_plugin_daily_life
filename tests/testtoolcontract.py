import asyncio
import ast
import inspect
import json
import sys
import threading
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from support import Event  # noqa: F401
from astrbot.core.agent.tool import FunctionTool
from core.runtime.tooling import (
    ArgumentCheckedTool,
    guard_tool_arguments,
    install_tool_argument_checks,
    prepare_tool_argument_checks,
    validate_tool_arguments,
)
from core.runtime.voicecall.toolbridge import VoiceCallToolBridge


class ImageTool(FunctionTool):
    def __init__(self):
        super().__init__(
            name="life_image_generate",
            description="生成生活照",
            parameters={
                "type": "object",
                "properties": {
                    "prompt": {"type": "string"},
                    "count": {"type": "integer"},
                },
                "required": ["prompt"],
            },
        )

    async def call(self, context, prompt, count=1):
        raise AssertionError("隔离测试不能调用真实服务")


class ToolContractTest(unittest.IsolatedAsyncioTestCase):
    def test_rejects_reason_only_without_exposing_value(self):
        original = ImageTool()
        checked = ArgumentCheckedTool(original)
        error = checked.argument_error({"reason": "SECRET正文"})
        result = json.loads(error)
        self.assertFalse(result["executed"])
        self.assertEqual(result["accepted_parameters"], ["prompt", "count"])
        self.assertIn(
            {"path": [], "rule": "required", "missing_parameters": ["prompt"]},
            result["issues"],
        )
        self.assertIn(
            {
                "path": [],
                "rule": "additionalProperties",
                "unexpected_parameters": ["reason"],
            },
            result["issues"],
        )
        self.assertNotIn("SECRET", error)
        self.assertNotIn("additionalProperties", original.parameters)

    def test_validates_types_and_nested_required_fields(self):
        parameters = {
            "type": "object",
            "properties": {
                "messages": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {"text": {"type": "string"}},
                        "required": ["text"],
                    },
                }
            },
        }
        for arguments, path in [
            ({"messages": [{"text": 17}]}, ["messages", 0, "text"]),
            ({"messages": [{}]}, ["messages", 0]),
        ]:
            with self.subTest(arguments=arguments):
                result = json.loads(
                    validate_tool_arguments("send", parameters, arguments)
                )
                self.assertEqual(result["issues"][0]["path"], path)
        self.assertIsNone(
            validate_tool_arguments(
                "send", parameters, {"messages": [{"text": "你好"}]}
            )
        )

    def test_preserves_extensible_external_schemas(self):
        class ExternalTool(FunctionTool):
            async def call(self, context, **kwargs):
                return kwargs

        for additional in (None, True, {"type": "string"}):
            schema = {"type": "object", "properties": {}}
            if additional is not None:
                schema["additionalProperties"] = additional
            original = ExternalTool(name="external", description="", parameters=schema)
            checked = ArgumentCheckedTool(original)
            self.assertIsNone(checked.argument_error({"dynamic": "value"}))
            self.assertEqual(checked.parameters, original.parameters)

    async def test_invalid_call_never_reaches_executor_and_valid_retry_does(self):
        calls = []
        module = types.ModuleType("astrbot.core.astr_agent_tool_exec")

        class Executor:
            @classmethod
            async def execute(cls, tool, context, **kwargs):
                calls.append((tool, context, kwargs))
                yield "图片任务已受理"

        module.FunctionToolExecutor = Executor
        original = ImageTool()
        checked = ArgumentCheckedTool(original)
        context = object()
        with patch.dict(sys.modules, {module.__name__: module}):
            invalid = [item async for item in checked.call(context, reason="看看自拍")]
            self.assertFalse(json.loads(invalid[0])["executed"])
            self.assertEqual(calls, [])
            valid = [
                item async for item in checked.call(context, prompt="居家随手自拍")
            ]
        self.assertEqual(valid, ["图片任务已受理"])
        self.assertEqual(calls, [(original, context, {"prompt": "居家随手自拍"})])

    async def test_delegates_permissions_and_background_submission_once(self):
        module = types.ModuleType("astrbot.core.astr_agent_tool_exec")
        calls = []

        class Executor:
            @classmethod
            async def execute(cls, tool, context, **kwargs):
                calls.append(tool)
                yield (
                    "background submitted"
                    if tool.is_background_task
                    else "permission denied"
                )

        module.FunctionToolExecutor = Executor
        for background in (False, True):
            original = ImageTool()
            original.is_background_task = background
            checked = ArgumentCheckedTool(original)
            self.assertFalse(checked.is_background_task)
            with patch.dict(sys.modules, {module.__name__: module}):
                results = [item async for item in checked.call(None, prompt="场景")]
            self.assertEqual(
                results, ["background submitted" if background else "permission denied"]
            )
            self.assertIs(calls[-1], original)
        self.assertEqual(len(calls), 2)

    async def test_cancellation_and_generator_close_release_executor(self):
        module = types.ModuleType("astrbot.core.astr_agent_tool_exec")
        released = []
        ready = asyncio.Event()

        class Executor:
            @classmethod
            async def execute(cls, tool, context, **kwargs):
                try:
                    yield "first"
                    ready.set()
                    await asyncio.Future()
                finally:
                    released.append(True)

        module.FunctionToolExecutor = Executor
        checked = ArgumentCheckedTool(ImageTool())
        with patch.dict(sys.modules, {module.__name__: module}):
            iterator = checked.call(None, prompt="场景")
            self.assertEqual(await anext(iterator), "first")
            await iterator.aclose()
            self.assertEqual(released, [True])
            iterator = checked.call(None, prompt="场景")
            await anext(iterator)
            task = asyncio.create_task(anext(iterator))
            await ready.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(released, [True, True])

    def test_request_install_is_idempotent_and_leaves_global_tool_unchanged(self):
        class ToolSet:
            def __init__(self, tool):
                self.tools = [tool]

            def add_tool(self, tool):
                self.tools = [tool]

        original = ImageTool()
        toolset = ToolSet(original)
        self.assertEqual(install_tool_argument_checks(toolset), 1)
        self.assertEqual(install_tool_argument_checks(toolset), 0)
        self.assertIs(toolset.tools[0]._wrapped, original)
        self.assertNotIn("additionalProperties", original.parameters)

    async def test_schema_construction_does_not_block_event_loop(self):
        started = threading.Event()
        release = threading.Event()

        def busy_constructor(_toolset):
            started.set()
            if not release.wait(2):
                raise AssertionError("工具初始化占住了事件循环")
            return 1

        with patch(
            "core.runtime.tooling.install_tool_argument_checks", busy_constructor
        ):
            task = asyncio.create_task(
                prepare_tool_argument_checks(types.SimpleNamespace(tools=[ImageTool()]))
            )
            try:
                self.assertTrue(await asyncio.to_thread(started.wait, 1))
                self.assertFalse(task.done())
                # 该回调需要事件循环继续运行，不能等构造函数结束后才执行。
                progressed = asyncio.Event()
                asyncio.get_running_loop().call_soon(progressed.set)
                await asyncio.wait_for(progressed.wait(), 0.2)
            finally:
                release.set()
                self.assertEqual(await task, 1)

    async def test_direct_entry_invalid_arguments_do_not_enter_lifecycle_guard(self):
        calls = []

        class Plugin:
            @guard_tool_arguments
            async def tool_image(self, event, prompt=""):
                calls.append(prompt)
                return "queued"

        plugin = Plugin()
        signature = inspect.signature(plugin.tool_image)
        self.assertEqual(list(signature.parameters), ["event", "prompt"])
        result = json.loads(await plugin.tool_image(None, reason="不要静默生成空图"))
        self.assertFalse(result["executed"])
        self.assertEqual(calls, [])
        self.assertEqual(await plugin.tool_image(None, prompt="自拍"), "queued")
        self.assertEqual(calls, ["自拍"])

    async def test_does_not_disguise_typeerror_inside_function(self):
        class Plugin:
            @guard_tool_arguments
            async def tool_image(self, event, prompt=""):
                raise TypeError("函数体内部的真实错误")

        with self.assertRaisesRegex(TypeError, "真实错误"):
            await Plugin().tool_image(None, prompt="自拍")

    async def test_direct_generator_entry_closes_underlying_iterator(self):
        released = []

        class Plugin:
            @guard_tool_arguments
            async def tool_image(self, event, prompt=""):
                try:
                    yield prompt
                finally:
                    released.append(True)

        plugin = Plugin()
        invalid = [item async for item in plugin.tool_image(None, reason="场景")]
        self.assertFalse(json.loads(invalid[0])["executed"])
        iterator = plugin.tool_image(None, prompt="场景")
        self.assertEqual(await anext(iterator), "场景")
        await iterator.aclose()
        self.assertEqual(released, [True])

    def test_all_registered_plugin_tools_have_entry_checks(self):
        source = Path(__file__).resolve().parents[1] / "main.py"
        count = 0
        for node in ast.walk(ast.parse(source.read_text())):
            if not isinstance(node, ast.AsyncFunctionDef):
                continue
            if any(
                isinstance(d, ast.Call)
                and isinstance(d.func, ast.Attribute)
                and d.func.attr == "llm_tool"
                for d in node.decorator_list
            ):
                self.assertTrue(
                    any(
                        isinstance(d, ast.Name) and d.id == "guard_tool_arguments"
                        for d in node.decorator_list
                    ),
                    node.name,
                )
                count += 1
        self.assertGreater(count, 0)

    async def test_voice_invalid_arguments_neither_execute_nor_increment_count(self):
        original = ImageTool()
        runtime = types.SimpleNamespace(
            config=types.SimpleNamespace(
                realtime_voice_call=types.SimpleNamespace(allow_function_calls=True)
            )
        )
        invite = types.SimpleNamespace(tool_call_count=0)
        bridge = VoiceCallToolBridge(runtime, invite)
        bridge._find = lambda _name: original
        for arguments in ({"reason": "SECRET"}, ["不是参数对象"]):
            result = json.loads(await bridge.call(original.name, arguments))
            self.assertFalse(result["executed"])
            self.assertEqual(invite.tool_call_count, 0)

    async def test_voice_end_invalid_arguments_do_not_hang_up(self):
        calls = []
        manager = types.SimpleNamespace(request_hangup=lambda *args: calls.append(args))
        bridge = VoiceCallToolBridge(
            types.SimpleNamespace(), types.SimpleNamespace(), manager=manager
        )
        result = json.loads(
            await bridge.call(
                "life_voice_call_end", {"reason": {"unexpected": "SECRET"}}
            )
        )
        self.assertFalse(result["executed"])
        self.assertEqual(calls, [])
