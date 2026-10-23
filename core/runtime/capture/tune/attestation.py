"""以完整证据版本复用记忆审阅，不把模型自述当作校验凭据。"""

import hashlib

from ....contextbudget import EvidenceCache, compact_json


def audit_cache(context) -> EvidenceCache:
    cache = getattr(context, "_memory_evidence_cache_store", None)
    if not isinstance(cache, EvidenceCache):
        cache = EvidenceCache(96)
        context._memory_evidence_cache_store = cache
    return cache


def audit_key(kind, value, meta, persona, provider) -> str:
    if kind == "target" and meta.get("interaction_messages_json"):
        # 批次保存时会切换发送者和投递编号；目标本人、完整有序消息及其余事实边界仍相同。
        meta = {key: item for key, item in meta.items() if key not in {
            "message_id", "platform", "user_id", "sender_profile_id", "sender_name",
        }}
    return hashlib.sha256(compact_json([kind, value, meta, persona, provider]).encode()).hexdigest()
