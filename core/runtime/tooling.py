"""按工具声明校验调用参数，保持原执行器的权限、任务与消息语义。"""

from __future__ import annotations

import asyncio
import inspect
import json
from copy import deepcopy
from functools import wraps
from typing import Any

from jsonschema import Draft202012Validator

from astrbot.core.agent.tool import FunctionTool


def _argument_error(name: str, parameters: dict, issues: list[dict]) -> str:
    """只反馈参数结构，不把传入正文或敏感值写入错误结果。"""

    return json.dumps(
        {
            "status": "invalid_arguments",
            "tool": name,
            "executed": False,
            "message": "工具尚未执行，请根据参数定义修正后重新调用。",
            "accepted_parameters": list(parameters.get("properties", {})),
            "required_parameters": list(parameters.get("required", [])),
            "issues": issues,
        },
        ensure_ascii=False,
    )


def validate_tool_arguments(name: str, parameters: dict, arguments: Any) -> str | None:
    """依据 JSON Schema 验证类型和必填项，不猜测或改写参数含义。"""

    return _validate_arguments(
        name, parameters, arguments, Draft202012Validator(parameters)
    )


def _validate_arguments(name, parameters, arguments, validator) -> str | None:
    issues = []
    for error in validator.iter_errors(arguments):
        issue = {"path": list(error.absolute_path), "rule": error.validator}
        # ValidationError.message 可能包含正文、密钥或地址，不能直接返回。
        if error.validator == "additionalProperties" and isinstance(
            error.instance, dict
        ):
            properties = error.schema.get("properties", {})
            issue["unexpected_parameters"] = sorted(
                set(error.instance) - set(properties)
            )
        elif error.validator == "required" and isinstance(error.instance, dict):
            issue["missing_parameters"] = [
                key for key in error.validator_value if key not in error.instance
            ]
        elif error.validator == "type":
            issue["expected_type"] = error.validator_value
        issues.append(issue)
        if len(issues) >= 6:
            break
    return _argument_error(name, parameters, issues) if issues else None


def guard_tool_arguments(func):
    """保护插件的直接工具入口；函数体内的 TypeError 仍按真实执行错误处理。"""

    signature = inspect.signature(func)
    names = [name for name in signature.parameters if name not in {"self", "event"}]
    required = [
        name
        for name in names
        if signature.parameters[name].default is inspect.Parameter.empty
    ]
    parameters = {"properties": dict.fromkeys(names), "required": required}

    def check(self, event, args, kwargs):
        try:
            signature.bind(self, event, *args, **kwargs)
        except TypeError:
            return _argument_error(
                func.__name__.removeprefix("tool_"),
                parameters,
                [
                    {
                        "rule": "signature",
                        "unexpected_parameters": sorted(set(kwargs) - set(names)),
                    }
                ],
            )
        return None

    if inspect.isasyncgenfunction(func):

        @wraps(func)
        async def generator(self, event, *args, **kwargs):
            if error := check(self, event, args, kwargs):
                yield error
                return
            iterator = func(self, event, *args, **kwargs)
            try:
                async for result in iterator:
                    yield result
            finally:
                await iterator.aclose()

        return generator

    @wraps(func)
    async def guarded(self, event, *args, **kwargs):
        if error := check(self, event, args, kwargs):
            return error
        return await func(self, event, *args, **kwargs)

    return guarded


def tool_argument_parameters(tool: FunctionTool) -> dict:
    parameters = deepcopy(tool.parameters)
    local_handler = tool.handler is not None or bool(tool.handler_module_path)
    call_parameters = inspect.signature(tool.call).parameters.values()
    fixed_call = not any(
        param.kind is inspect.Parameter.VAR_KEYWORD for param in call_parameters
    )
    # 本地固定参数入口采用封闭对象；MCP 等可扩展入口保留原 Schema。
    if (
        (local_handler or fixed_call)
        and parameters.get("type") == "object"
        and "properties" in parameters
    ):
        parameters.setdefault("additionalProperties", False)
    return parameters


class ArgumentCheckedTool(FunctionTool):
    """只校验参数，执行仍委托给原工具与 AstrBot 执行器。"""

    def __init__(self, wrapped: FunctionTool) -> None:
        parameters = tool_argument_parameters(wrapped)
        super().__init__(
            name=wrapped.name, description=wrapped.description, parameters=parameters
        )
        self._wrapped = wrapped
        self._validator = Draft202012Validator(parameters)
        self.active = wrapped.active
        self.handler_module_path = wrapped.handler_module_path
        # 后台任务由原执行器提交一次，外层不能再次创建后台任务。
        self.is_background_task = False

    def argument_error(self, arguments: Any) -> str | None:
        return _validate_arguments(
            self.name, self.parameters, arguments, self._validator
        )

    async def call(self, context, **kwargs):
        if error := self.argument_error(kwargs):
            yield error
            return
        from astrbot.core.astr_agent_tool_exec import FunctionToolExecutor

        iterator = FunctionToolExecutor.execute(self._wrapped, context, **kwargs)
        try:
            async for result in iterator:
                yield result
        finally:
            await iterator.aclose()


def install_tool_argument_checks(toolset: Any) -> int:
    """只替换当前请求的工具，不修改全局注册表或缓存参数定义。"""

    adder = getattr(toolset, "add_tool", None)
    if not callable(adder):
        return 0
    installed = 0
    for tool in list(getattr(toolset, "tools", []) or []):
        if not isinstance(tool, FunctionTool) or isinstance(tool, ArgumentCheckedTool):
            continue
        adder(ArgumentCheckedTool(tool))
        installed += 1
    return installed


async def prepare_tool_argument_checks(toolset: Any) -> int:
    """批量工具构造包含框架 Schema 校验，交给工作线程避免阻塞事件循环。"""

    if not getattr(toolset, "tools", None):
        return 0
    return await asyncio.to_thread(install_tool_argument_checks, toolset)
