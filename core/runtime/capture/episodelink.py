"""校验记忆批次提出的经历续接，不从标题或关键词判断是否同一件事。"""

from __future__ import annotations

from typing import Protocol


class EpisodeArchive(Protocol):
    async def link_memory_episode(
        self, *, scope: str, previous_id: int, summary_id: int, evidence: str
    ) -> bool: ...


def episode_scope(batch: dict) -> str:
    rows = batch.get("messages") or []
    return str((rows[-1].get("group_id") if rows else "") or batch.get("session_id") or "")


async def link_batch_episode(
    archive: EpisodeArchive, payload: dict, batch: dict, summary_id: int
) -> bool:
    link = payload.get("episode_link")
    if not isinstance(link, dict):
        return False
    previous = next((item for item in batch.get("correctable_memories", [])
                     if item.get("id") == link.get("memory_id")), None)
    row = next((row for row in batch.get("messages", [])
                if str(row.get("message_id") or row.get("id")) == str(link.get("source_message_id"))
                and row.get("role", "user") == "user" and not row.get("is_quoted")), None)
    previous_quote = str(link.get("previous_quote") or "").strip()
    evidence = str(link.get("evidence") or "").strip()
    if (not previous or not row or not previous_quote or not evidence
            or previous_quote not in str(previous.get("content") or "")
            or evidence != str(row.get("message_text") or "").strip()):
        return False
    return await archive.link_memory_episode(
        scope=episode_scope(batch), previous_id=previous["id"], summary_id=summary_id,
        evidence=f"此前：{previous_quote}；本次：{evidence}",
    )
