from __future__ import annotations

import asyncio
import datetime
import time
import uuid
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager, nullcontext
from dataclasses import dataclass, field
from typing import Any

from astrbot.api import logger

from .markers import LOG_PREFIX

DAILY_REFRESH_GENERATED_DATE = "daily_refresh_generated_date"


class DailyGenerationBusy(RuntimeError):
    pass


@dataclass(slots=True)
class DailyGenerationOperation:
    operation_id: str
    date: str
    source: str
    source_label: str
    phase: str = "queued"
    started_at: str = ""
    finished_at: str = ""
    error: str = ""
    task: asyncio.Task | None = field(default=None, repr=False)
    started_monotonic: float = field(default_factory=time.monotonic, repr=False)

    def as_dict(self) -> dict[str, Any]:
        return {
            "operation_id": self.operation_id,
            "date": self.date,
            "source": self.source,
            "source_label": self.source_label,
            "phase": self.phase,
            "running": self.phase not in {"completed", "failed"},
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "error": self.error,
        }


@dataclass(frozen=True, slots=True)
class DailyGenerationResult:
    day: Any
    web_inspiration: str
    operation_id: str
    reused: bool = False


class DailyGenerationService:
    """按日期协调日程生成；跨配置切换的任务持有自己的服务租约。"""

    HISTORY_LIMIT = 64

    _DAILY_GENERATION_SOURCE_LABELS = {
        "dashboard_reset": "面板重生",
        "command_reset": "命令重生",
        "outfit_seed": "穿搭补全",
        "injection_seed": "即时补全",
        "startup_seed": "首次启动补全",
        "daily_refresh": "每日刷新",
        "residence_change": "居住地变化",
    }

    def __init__(
        self,
        *,
        archive: Callable[[], Any],
        composer: Callable[[], Any],
        persona: Callable[[], Awaitable[str]],
        notify_status: Callable[[str], Awaitable[Any]],
        failed_dates: Callable[[], dict[str, Any] | None],
        lease_factory: Callable[[], AbstractAsyncContextManager[Any]] | None = None,
    ) -> None:
        self._archive = archive
        self._composer = composer
        self._persona = persona
        self._notify_status = notify_status
        self._failed_dates = failed_dates
        self._lease_factory = lease_factory
        self._guard = asyncio.Lock()
        self._closed = False
        self._active: dict[str, DailyGenerationOperation] = {}
        self._last: dict[str, DailyGenerationOperation] = {}

    def daily_generation_status(self, date_str: str = "") -> dict[str, Any]:
        key = str(date_str or "").strip()
        operation = self._active.get(key)
        if operation is None:
            operation = self._last.get(key)
        return operation.as_dict() if operation is not None else {}

    async def _notify_daily_generation_status(self, reason: str) -> None:
        await self._notify_status(reason)

    async def _set_daily_generation_phase(
        self,
        operation: DailyGenerationOperation,
        phase: str,
    ) -> None:
        operation.phase = phase
        await self._notify_daily_generation_status(f"daily_generation_{phase}")

    async def _mark_daily_refresh_generated(
        self,
        day: Any,
        operation: DailyGenerationOperation,
    ) -> Any:
        if operation.source != "daily_refresh":
            return day

        def mark(current_day: Any) -> bool:
            meta = getattr(current_day, "meta", None)
            if not isinstance(meta, dict):
                meta = {}
                current_day.meta = meta
            if str(meta.get(DAILY_REFRESH_GENERATED_DATE) or "") == operation.date:
                return False
            meta[DAILY_REFRESH_GENERATED_DATE] = operation.date
            return True

        mutate_day = getattr(self._archive(), "mutate_day", None)
        if callable(mutate_day):
            persisted = await mutate_day(operation.date, mark)
            if persisted is not None:
                return persisted
        mark(day)
        return day

    @staticmethod
    def _consume_daily_generation_task(task: asyncio.Task) -> None:
        if task.cancelled():
            return
        try:
            task.exception()
        except asyncio.CancelledError:
            return
        except Exception as exc:
            task_name = str(task.get_name() or "日程任务")
            logger.error(
                f"{LOG_PREFIX} 后台日程任务异常：任务={task_name}；"
                f"异常={type(exc).__name__}: {exc}"
            )

    async def run_daily_generation(
        self,
        *,
        date: datetime.datetime,
        source: str,
        force: bool,
        extra: str = "",
        target_hour: int | None = None,
        delete_existing: bool = False,
        use_web: bool = False,
        search_keyword: str = "今日生活",
        search_prompt: str = "",
        search_category: str = "今日生活背景",
        reject_if_busy: bool = False,
    ) -> DailyGenerationResult:
        date_str = date.strftime("%Y-%m-%d")
        async with self._guard:
            if self._closed:
                raise RuntimeError("日程生成服务已关闭")
            current = self._active.get(date_str)
            if (
                current is not None
                and current.task is not None
                and not current.task.done()
            ):
                if reject_if_busy:
                    raise DailyGenerationBusy(f"{date_str} 的生活安排正在重生，请稍等")
                task = current.task
                reused = True
            else:
                operation = DailyGenerationOperation(
                    operation_id=uuid.uuid4().hex[:8],
                    date=date_str,
                    source=source,
                    source_label=self._DAILY_GENERATION_SOURCE_LABELS.get(
                        source, source
                    ),
                    started_at=datetime.datetime.now()
                    .astimezone()
                    .isoformat(timespec="seconds"),
                )
                task = asyncio.create_task(
                    self._execute_daily_generation(
                        operation,
                        date=date,
                        force=force,
                        extra=extra,
                        target_hour=target_hour,
                        delete_existing=delete_existing,
                        use_web=use_web,
                        search_keyword=search_keyword,
                        search_prompt=search_prompt,
                        search_category=search_category,
                    )
                )
                operation.task = task
                self._active[date_str] = operation
                task.add_done_callback(self._consume_daily_generation_task)
                reused = False

        result = await asyncio.shield(task)
        if reused:
            return DailyGenerationResult(
                day=result.day,
                web_inspiration=result.web_inspiration,
                operation_id=result.operation_id,
                reused=True,
            )
        return result

    async def _execute_daily_generation(
        self,
        operation: DailyGenerationOperation,
        *,
        date: datetime.datetime,
        force: bool,
        extra: str,
        target_hour: int | None,
        delete_existing: bool,
        use_web: bool,
        search_keyword: str,
        search_prompt: str,
        search_category: str,
    ) -> DailyGenerationResult:
        task_id = operation.operation_id
        logger.info(
            f"{LOG_PREFIX} 日程任务开始：任务={task_id}；日期={operation.date}；"
            f"来源={operation.source_label}"
        )
        web_inspiration = ""
        try:
            # shield 只保护任务不被前台取消；租约由任务本身持有，直到结束。
            lease = self._lease_factory() if self._lease_factory else nullcontext()
            async with lease:
                regenerate_existing = bool(
                    force
                    and operation.source in {"dashboard_reset", "command_reset"}
                    and await self._archive().get_day(operation.date) is not None
                )
                if use_web:
                    await self._set_daily_generation_phase(operation, "searching")
                    logger.debug(f"{LOG_PREFIX} 日程任务进入联网搜索：任务={task_id}")
                    web_inspiration = await self._composer().search.inspiration(
                        search_keyword,
                        search_prompt,
                        category=search_category,
                        persona=await self._persona(),
                        today=operation.date,
                        trace_id=task_id,
                    )
                    logger.debug(f"{LOG_PREFIX} 日程任务联网搜索结束：任务={task_id}")

                await self._set_daily_generation_phase(operation, "waiting")
                if delete_existing:
                    logger.debug(
                        f"{LOG_PREFIX} 日程任务采用生成成功后原子替换：任务={task_id}"
                    )
                if not force:
                    existing = await self._archive().get_day(operation.date)
                    if existing is not None:
                        day = existing
                    else:
                        await self._set_daily_generation_phase(operation, "generating")
                        day = await self._composer().generate_daily(
                            date=date,
                            force=False,
                            target_hour=target_hour,
                            extra=extra,
                            web_inspiration=web_inspiration,
                        )
                else:
                    await self._set_daily_generation_phase(operation, "generating")
                    day = await self._composer().generate_daily(
                        date=date,
                        force=True,
                        target_hour=target_hour,
                        extra=extra,
                        web_inspiration=web_inspiration,
                        regenerate_existing=regenerate_existing,
                    )

                if day is None:
                    raise RuntimeError("日程模型未生成有效生活安排")
                day = await self._mark_daily_refresh_generated(day, operation)
                failed_dates = self._failed_dates()
                if isinstance(failed_dates, dict):
                    failed_dates.pop(operation.date, None)
            operation.phase = "completed"
            operation.finished_at = (
                datetime.datetime.now().astimezone().isoformat(timespec="seconds")
            )
            logger.info(
                f"{LOG_PREFIX} 日程任务完成：任务={task_id}；"
                f"节点={len(getattr(day, 'timeline', []) or [])}；"
                f"总耗时={time.monotonic() - operation.started_monotonic:.2f} 秒"
            )
            return DailyGenerationResult(day, web_inspiration, task_id)
        except asyncio.CancelledError:
            operation.phase = "failed"
            operation.error = "日程生成已取消"
            operation.finished_at = (
                datetime.datetime.now().astimezone().isoformat(timespec="seconds")
            )
            raise
        except Exception as exc:
            operation.phase = "failed"
            operation.error = str(exc) or "日程生成失败"
            operation.finished_at = (
                datetime.datetime.now().astimezone().isoformat(timespec="seconds")
            )
            logger.warning(
                f"{LOG_PREFIX} 日程任务失败：任务={task_id}；原因={operation.error}；"
                f"总耗时={time.monotonic() - operation.started_monotonic:.2f} 秒"
            )
            raise
        finally:
            async with self._guard:
                current = self._active.get(operation.date)
                if current is operation:
                    self._active.pop(operation.date, None)
                self._remember_finished(operation)
            await self._notify_daily_generation_status(
                "daily_generation_completed"
                if operation.phase == "completed"
                else "daily_generation_failed"
            )

    def _remember_finished(self, operation: DailyGenerationOperation) -> None:
        self._last.pop(operation.date, None)
        self._last[operation.date] = operation
        while len(self._last) > self.HISTORY_LIMIT:
            self._last.pop(next(iter(self._last)))

    async def close(self) -> None:
        async with self._guard:
            self._closed = True
            operations = list(self._active.values())
            tasks = [
                operation.task
                for operation in self._active.values()
                if operation.task is not None and not operation.task.done()
            ]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        # 尚未获得执行机会的任务没有进入 try/finally，由关闭方补齐终态。
        async with self._guard:
            for operation in operations:
                if self._active.get(operation.date) is operation:
                    self._active.pop(operation.date, None)
                    operation.phase = "failed"
                    operation.error = "日程生成已取消"
                    operation.finished_at = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
                    self._remember_finished(operation)


__all__ = [
    "DAILY_REFRESH_GENERATED_DATE",
    "DailyGenerationBusy",
    "DailyGenerationService",
    "DailyGenerationOperation",
    "DailyGenerationResult",
]
