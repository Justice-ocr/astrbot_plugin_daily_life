from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from dataclasses import replace
from typing import Any, Protocol

from astrbot.api import logger

from .markers import LOG_PREFIX
from ..epistemic import knowledge_label
from ..models import LongTermMemoryRecord
from .recallrank import fuse_memory_routes


class MemoryRepository(Protocol):
    """长期记忆检索需要的归档能力。"""

    async def search_long_term_memories(
        self, query: str, *, scopes: list[str], categories: list[str], limit: int
    ) -> list[Any]: ...

    async def list_recent_long_term_memories(
        self, *, scopes: list[str], categories: list[str], limit: int
    ) -> list[Any]: ...


class LifeMemoryService:
    """基于当前事件范围检索长期记忆，归档在每次操作时取得。"""

    def __init__(
        self,
        *,
        archive: Callable[[], MemoryRepository],
        group_meta: Callable[[Any], tuple[str, str]],
        session_id: Callable[[Any], str],
        message_text: Callable[[Any], str],
        semantic_search: Callable[..., Awaitable[list[Any] | None]] | None = None,
        recent_messages: Callable[[str], Awaitable[list[dict[str, Any]]]] | None = None,
    ) -> None:
        self._archive = archive
        self._event_group_meta = group_meta
        self._event_session_id = session_id
        self._event_message_text = message_text
        self._semantic_search = semantic_search
        self._recent_messages = recent_messages

    def _event_memory_scopes(self, event: Any = None) -> list[str]:
        if event is None:
            return []
        scopes: list[str] = []
        group_id, _ = self._event_group_meta(event)
        session_id = self._event_session_id(event)
        for value in (group_id, session_id):
            text = str(value or "").strip()
            if text and text not in scopes:
                scopes.append(text)
        return scopes

    def _memory_query_from_event(self, event: Any = None, message: str = "") -> str:
        pieces: list[str] = []
        for item in (
            str(message or "").strip(),
            self._event_message_text(event) if event is not None else "",
        ):
            text = str(item or "").strip()
            if text and text not in pieces:
                pieces.append(text)
        return " ".join(item for item in pieces if item).strip()

    @staticmethod
    def _format_memory_hits(
        memories: list[Any], *, include_source: bool = True, limit: int = 5
    ) -> str:
        lines: list[str] = []
        for item in memories[: max(1, limit)]:
            content = str(getattr(item, "content", "") or "").strip()
            if not content:
                continue
            label = str(
                getattr(item, "title", "") or getattr(item, "category", "") or "记忆"
            ).strip()
            source = ""
            if include_source:
                source_table = str(getattr(item, "source_table", "") or "").strip()
                source_id = str(getattr(item, "source_id", "") or "").strip()
                date = str(getattr(item, "date", "") or "").strip()
                bits = [bit for bit in (f"记忆编号 {getattr(item, 'id', 0)}", date, source_table, source_id) if bit]
                source = f"（来源：{' / '.join(bits)}）" if bits else ""
            validity = str(getattr(item, "recall_context", "") or "")
            if validity:
                content = f"【历史摘要】{content}；【时效核对】{validity}"
            lines.append(f"- {label}：{content}{source}；{knowledge_label(item)}")
        return "\n".join(lines)

    async def _complete_recall(
        self, hits: list[Any], *, scopes: list[str], categories: list[str], limit: int,
        include_related: bool = False,
    ) -> list[Any]:
        """数据库补充也使用短预算，语义接口缓慢时仍能正常聊天。"""
        if not hits or not scopes:
            return hits
        archive = self._archive()
        try:
            async with asyncio.timeout(0.3):
                reader = getattr(archive, "get_related_long_term_memories", None)
                if include_related and callable(reader):
                    related = await reader(
                        [item.id for item in hits], scopes=scopes, categories=categories,
                        limit=min(24, limit * 2),
                    )
                    hits = fuse_memory_routes([(1.0, hits), (0.35, related)], limit)
                checker = getattr(archive, "get_memory_recall_context", None)
                if callable(checker):
                    notes = await checker([item.id for item in hits], scopes=scopes)
                    hits = [replace(item, recall_context=notes.get(item.id, ""))
                            if isinstance(item, LongTermMemoryRecord) else item for item in hits]
        except TimeoutError:
            # 没有完成事实核对时，不能把未经检查的旧摘要作为当前事实注入。
            return []
        except Exception as exc:
            logger.debug(f"{LOG_PREFIX} 记忆核对暂不可用：{type(exc).__name__}")
            return []
        return hits

    async def search(
        self,
        query: str,
        *,
        event: Any = None,
        mode: str = "search",
        category: str = "",
        limit: int = 5,
        semantic_timeout_seconds: float = 2.5,
        semantic_query: str = "",
    ) -> list[Any]:
        clean_query = str(query or "").strip()
        categories = [
            item.strip()
            for item in str(category or "").replace("，", ",").split(",")
            if item.strip()
        ]
        scopes = self._event_memory_scopes(event)
        limit = max(1, min(int(limit or 5), 12))
        if clean_query:
            lexical = await self._archive().search_long_term_memories(
                clean_query,
                scopes=scopes,
                categories=categories,
                limit=limit,
            )
            if self._semantic_search is not None:
                try:
                    # 语义增强共享前台短预算；超时后保留已有检索结果。
                    async with asyncio.timeout(semantic_timeout_seconds):
                        semantic = await self._semantic_search(
                            semantic_query or clean_query, lexical=lexical, scopes=scopes,
                            categories=categories, limit=limit,
                        )
                    if semantic is not None:
                        logger.debug(f"{LOG_PREFIX} 长期记忆语义召回：{len(semantic)} 条")
                        return await self._complete_recall(
                            semantic, scopes=scopes, categories=categories, limit=limit,
                        )
                except Exception as exc:
                    logger.debug(
                        f"{LOG_PREFIX} 长期记忆语义召回降级：{type(exc).__name__}"
                    )
            return await self._complete_recall(
                lexical, scopes=scopes, categories=categories, limit=limit, include_related=True,
            )
        if str(mode or "").strip().lower() in {"recent", "time"}:
            hits = await self._archive().list_recent_long_term_memories(
                scopes=scopes,
                categories=categories,
                limit=limit,
            )
            return await self._complete_recall(hits, scopes=scopes, categories=categories, limit=limit)
        return []

    async def search_text(
        self,
        event: Any,
        query: str = "",
        mode: str = "search",
        category: str = "",
        limit: int = 5,
        memory_id: int = 0,
        offset: int = 0,
    ) -> str:
        if mode == "evidence":
            reader = getattr(self._archive(), "read_memory_evidence", None)
            if not callable(reader) or memory_id <= 0:
                return "请先检索记忆，再使用返回的记忆编号查看原话。"
            result = await reader(
                memory_id, scopes=self._event_memory_scopes(event), offset=offset,
            )
            return json.dumps(result, ensure_ascii=False)
        hits = await self.search(
            query,
            event=event,
            mode=mode,
            category=category,
            limit=limit,
        )
        if not hits:
            return "没有检索到相关长期记忆。"
        body = self._format_memory_hits(hits, include_source=True, limit=limit)
        return f"长期记忆检索结果：\n{body}\n需要核对原话时可用 evidence 模式及记忆编号分页读取。"

    async def build_context(
        self, event: Any, message: str = "", limit: int = 4
    ) -> str:
        if limit <= 0 or not self._event_memory_scopes(event):
            return ""
        query = self._memory_query_from_event(event, message)
        if not query:
            return ""
        semantic_query = query
        if self._recent_messages is not None:
            try:
                async with asyncio.timeout(0.15):
                    messages = await self._recent_messages(self._event_session_id(event))
                recent = [
                    f"{item['role']}：{str(item.get('content') or '').strip()[:240]}"
                    for item in messages[-4:]
                    if item.get("role") in {"user", "assistant"}
                    and str(item.get("content") or "").strip()
                    and str(item.get("content") or "").strip() != query
                ]
                if recent:
                    semantic_query = f"本轮消息：{query}\n近期对话：\n" + "\n".join(recent)
            except Exception as exc:
                logger.debug(f"{LOG_PREFIX} 记忆检索读取近期对话失败：{type(exc).__name__}")
        hits = await self.search(
            query,
            event=event,
            mode="search",
            limit=limit,
            semantic_timeout_seconds=1.2,
            semantic_query=semantic_query,
        )
        if not hits:
            logger.debug(f"{LOG_PREFIX} 当前话题长期记忆召回：0 条")
            return ""
        logger.debug(f"{LOG_PREFIX} 当前话题长期记忆召回：{len(hits)} 条")
        body = self._format_memory_hits(hits, include_source=True, limit=limit)
        if not body:
            return ""
        return (
            "\n[HiddenLongTermMemory]\n"
            "以下是根据本轮真实消息检索到的相关长期记忆，只作为内部判断依据；不要逐字复述给用户。\n"
            "摘要是历史记录，当前事实以时效核对为准；需核对原话时可用 life_memory_search 的 evidence 模式。\n"
            f"{body}\n"
        )


__all__ = ["LifeMemoryService", "MemoryRepository"]
