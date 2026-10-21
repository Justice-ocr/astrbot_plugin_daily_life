"""后台遗忘与存储清理共用的记忆保护条件。"""


def unprotected_memory_clause(alias: str = "m") -> str:
    # alias 仅由内部 SQL 调用点提供，不接受工具参数。
    return f"""
        {alias}.weight < 3
        AND {alias}.category NOT LIKE 'correction%'
        AND {alias}.source_table <> 'memory_corrections'
        AND NOT EXISTS (SELECT 1 FROM memory_fact_links l WHERE l.memory_id = {alias}.id)
        AND NOT EXISTS (SELECT 1 FROM life_episodes e WHERE
            {alias}.source_table = 'life_episodes' AND {alias}.source_id = CAST(e.id AS TEXT)
            AND (e.protected = 1 OR e.status = 'open'))
        AND NOT EXISTS (SELECT 1 FROM commitments c WHERE c.status = 'active' AND (
            ({alias}.source_table = 'commitments' AND {alias}.source_id = CAST(c.id AS TEXT)) OR
            ({alias}.session_id <> '' AND {alias}.session_id = c.source_session AND
             {alias}.message_id <> '' AND {alias}.message_id = c.source_message_id) OR
            ({alias}.source_table = 'chat_summaries' AND EXISTS (
                SELECT 1 FROM memory_source_messages s WHERE
                CAST(s.summary_id AS TEXT) = {alias}.source_id
                AND s.session_id = c.source_session AND s.message_id = c.source_message_id
            ))))
    """


__all__ = ["unprotected_memory_clause"]
