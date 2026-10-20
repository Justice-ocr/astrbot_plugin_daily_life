from __future__ import annotations

import json
import time
from typing import Any

from astrbot.api import logger

from ...search.cache import SingleFlight
from ..markers import LOG_PREFIX

from typing import Protocol


class SnapshotPackComponentContext(Protocol):
    """此组件读取或更新的宿主接口。"""

    _INJECTION_SNAPSHOT_CACHE_LIMIT: Any
    _INJECTION_SNAPSHOT_CACHE_TTL_SECONDS: Any
    _cached_injection_snapshot: Any
    _event_group_meta: Any
    _event_session_id: Any
    _injection_snapshot_cache: Any
    _injection_snapshot_singleflight: Any
    _page_status_version: Any
    _settle_stale_reply_effects: Any
    archive: Any
    config: Any
    context_snapshot: Any


class SnapshotPackComponent:
    def __init__(self, context: SnapshotPackComponentContext) -> None:
        self.context = context

    _INJECTION_SNAPSHOT_CACHE_TTL_SECONDS = 8.0
    _INJECTION_SNAPSHOT_CACHE_LIMIT = 128

    def _store_injection_snapshot(self, cache: dict[str, Any], key: str, snapshot: dict) -> None:
        now = time.monotonic()
        for expired_key, value in list(cache.items()):
            if now - float(value.get("ts", 0.0) or 0.0) > self.context._INJECTION_SNAPSHOT_CACHE_TTL_SECONDS:
                cache.pop(expired_key, None)
        cache.pop(key, None)
        cache[key] = {"ts": now, "data": snapshot}
        while len(cache) > self.context._INJECTION_SNAPSHOT_CACHE_LIMIT:
            cache.pop(next(iter(cache)))

    def _injection_snapshot_flight(self) -> SingleFlight[dict[str, Any]]:
        flight = getattr(self.context, "_injection_snapshot_singleflight", None)
        if not isinstance(flight, SingleFlight):
            flight = SingleFlight[dict[str, Any]]()
            self.context._injection_snapshot_singleflight = flight
        return flight

    @classmethod
    def _cached_injection_snapshot(
        cls,
        cache: dict[str, Any],
        cache_key: str,
    ) -> dict[str, Any] | None:
        cached = cache.get(cache_key)
        if not isinstance(cached, dict):
            return None
        if (
            time.monotonic() - float(cached.get("ts", 0.0) or 0.0)
            > cls._INJECTION_SNAPSHOT_CACHE_TTL_SECONDS
        ):
            cache.pop(cache_key, None)
            return None
        return dict(cached.get("data") or {})

    async def _read_life_context_snapshot(
        self,
        *,
        cache: dict[str, Any],
        cache_key: str,
        max_summaries: int,
        experience_scope: str,
        session_id: str,
        allow_cached: bool,
    ) -> dict[str, Any]:
        if allow_cached:
            cached = self.context._cached_injection_snapshot(cache, cache_key)
            if cached is not None:
                return cached

        await self.context._settle_stale_reply_effects()

        started = time.perf_counter()
        snapshot_version = int(getattr(self.context, "_page_status_version", 0) or 0)
        repository = getattr(self.context, "context_snapshot", None)
        if repository is None:
            from ..context import ContextSnapshotRepository

            repository = ContextSnapshotRepository(self.context.archive)
        snapshot = await repository.read(
            max_summaries=max_summaries,
            experience_scope=experience_scope,
            session_id=session_id,
        )
        snapshot["experience_scope"] = experience_scope
        logger.debug(
            f"{LOG_PREFIX} 上下文快照读取完成，耗时 {time.perf_counter() - started:.3f} 秒"
        )

        if int(getattr(self.context, "_page_status_version", 0) or 0) == snapshot_version:
            self._store_injection_snapshot(cache, cache_key, snapshot)
        return dict(snapshot)

    async def _close_injection_snapshot_flight(self) -> None:
        flight = getattr(self.context, "_injection_snapshot_singleflight", None)
        if isinstance(flight, SingleFlight):
            await flight.close()

    async def _gather_life_context_snapshot(
        self,
        event: Any = None,
        *,
        use_cache: bool = True,
    ) -> dict[str, Any]:
        cache = getattr(self.context, "_injection_snapshot_cache", None)
        if not isinstance(cache, dict):
            self.context._injection_snapshot_cache = {}
            cache = self.context._injection_snapshot_cache

        max_summaries = self.context.config.memory.max_injection_items
        experience_scope = ""
        session_id = ""
        group_id = ""
        if event is not None:
            session_id = self.context._event_session_id(event)
            group_id, _ = self.context._event_group_meta(event)
            experience_scope = group_id or session_id

        cache_key = json.dumps(
            [max_summaries, experience_scope, session_id,
             int(getattr(getattr(self.context, "archive", None), "living_revision", 0))], ensure_ascii=False
        )
        if use_cache:
            cached = self.context._cached_injection_snapshot(cache, cache_key)
            if cached is not None:
                return cached
            snapshot, shared = await self._injection_snapshot_flight().run(
                cache_key,
                lambda: self._read_life_context_snapshot(
                    cache=cache,
                    cache_key=cache_key,
                    max_summaries=max_summaries,
                    experience_scope=experience_scope,
                    session_id=session_id,
                    allow_cached=True,
                ),
            )
            if shared:
                logger.debug(f"{LOG_PREFIX} 上下文快照合并了同键并发读取")
            return dict(snapshot)

        return await self._read_life_context_snapshot(
            cache=cache,
            cache_key=cache_key,
            max_summaries=max_summaries,
            experience_scope=experience_scope,
            session_id=session_id,
            allow_cached=False,
        )
