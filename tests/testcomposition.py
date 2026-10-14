from __future__ import annotations

import ast
import asyncio
import inspect
import unittest
from contextlib import asynccontextmanager
from functools import cached_property
from pathlib import Path

import pytest

import support  # noqa: F401 - 安装 AstrBot 测试替身
from core.composition import ComponentCall
from core.models import TemporalFactRecord
from core.runtime.live import DailyLifeRuntime


ROOT = Path(__file__).resolve().parents[1]


def test_production_has_no_mixin_or_component_inheritance():
    violations = []
    for path in [ROOT / "main.py", *(ROOT / "core").rglob("*.py")]:
        for node in ast.walk(ast.parse(path.read_text())):
            if not isinstance(node, ast.ClassDef):
                continue
            if "Mixin" in node.name or (
                node.name.endswith("Component") and node.bases
            ):
                violations.append((str(path.relative_to(ROOT)), node.name))
            for base in node.bases:
                if isinstance(base, ast.Name) and base.id.endswith("Component"):
                    violations.append((str(path.relative_to(ROOT)), node.name, base.id))
    assert violations == []


def test_runtime_routes_to_owned_component_and_isolates_hosts():
    left = DailyLifeRuntime.__new__(DailyLifeRuntime)
    right = DailyLifeRuntime.__new__(DailyLifeRuntime)
    left._init_continuous_turn_state()
    right._init_continuous_turn_state()
    first = left._part_continuous_turn_component
    assert first is left._part_continuous_turn_component
    assert first.context is left
    assert first is not right._part_continuous_turn_component
    left._continuous_turn_revisions["scope"] = 3
    assert right._continuous_turn_revisions == {}
    assert left.note_continuous_turn_incoming.__self__ is first
    assert DailyLifeRuntime.__bases__ == (object,)


def test_record_serialization_has_no_mapping_base():
    record = TemporalFactRecord(
        scope="chat", subject="bot", predicate="outfit",
        provenance={"evidence": ["receipt"]},
    )
    value = record.as_dict()
    value["provenance"]["evidence"].append("changed")
    assert record.provenance == {"evidence": ["receipt"]}
    assert TemporalFactRecord.from_value(record).scope == "chat"
    assert TemporalFactRecord.__bases__ == (object,)


class _Operations:
    def __init__(self, context):
        self.context = context

    async def wait(self, value: str, *, count: int = 1) -> str:
        self.context.started.set()
        await self.context.resume.wait()
        return value * count

    @asynccontextmanager
    async def lease(self):
        self.context.active += 1
        try:
            yield self.context.active
        finally:
            self.context.active -= 1

    async def stream(self):
        self.context.active += 1
        try:
            yield "first"
            yield "second"
        finally:
            self.context.active -= 1


class _Host:
    def __init__(self):
        self.active = 0
        self.started = asyncio.Event()
        self.resume = asyncio.Event()

    @cached_property
    def operations(self):
        return _Operations(self)

    wait = ComponentCall("operations", _Operations, "wait")
    lease = ComponentCall("operations", _Operations, "lease")
    stream = ComponentCall("operations", _Operations, "stream")


def test_delegated_signature_and_function_kinds_are_preserved():
    host = _Host()
    assert inspect.signature(_Host.wait) == inspect.signature(_Operations.wait)
    assert inspect.signature(host.wait) == inspect.signature(host.operations.wait)
    assert inspect.iscoroutinefunction(_Host.wait)
    assert inspect.iscoroutinefunction(host.wait)
    assert inspect.isasyncgenfunction(_Host.stream)
    assert inspect.isasyncgenfunction(host.stream)
    assert not inspect.isasyncgenfunction(_Host.lease)
    assert not inspect.iscoroutinefunction(_Host.lease)


class AsyncCompositionTests(unittest.IsolatedAsyncioTestCase):
    async def test_context_manager_releases_on_error(self):
        host = _Host()
        with pytest.raises(ValueError):
            async with _Host.lease(host) as active:
                assert active == 1
                async with host.lease():
                    assert host.active == 2
                raise ValueError("结束")
        assert host.active == 0

    async def test_generator_close_releases_owned_resources(self):
        host = _Host()
        stream = _Host.stream(host)
        assert await anext(stream) == "first"
        assert host.active == 1
        await stream.aclose()
        assert host.active == 0

    async def test_cancelled_delegate_does_not_hold_another_host(self):
        left, right = _Host(), _Host()
        task = asyncio.create_task(_Host.wait(left, "left"))
        await left.started.wait()
        right.resume.set()
        assert await right.wait("right", count=2) == "rightright"
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
