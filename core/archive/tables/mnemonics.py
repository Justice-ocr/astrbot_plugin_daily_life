RECOLLECTION_SQL = """
-- 摘要保留的原话与时间事实依据；不把模型摘要当作原始证据。
CREATE TABLE IF NOT EXISTS memory_source_messages (
    summary_id INTEGER NOT NULL,
    event_key TEXT NOT NULL,
    session_id TEXT NOT NULL,
    message_id TEXT NOT NULL DEFAULT '',
    role TEXT NOT NULL,
    sender_name TEXT NOT NULL DEFAULT '',
    occurred_at TEXT NOT NULL,
    message_text TEXT NOT NULL,
    PRIMARY KEY(summary_id, event_key),
    FOREIGN KEY(summary_id) REFERENCES chat_summaries(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_memory_source_message
ON memory_source_messages(session_id, message_id);
CREATE TABLE IF NOT EXISTS memory_fact_links (
    memory_id INTEGER NOT NULL,
    fact_id INTEGER NOT NULL,
    PRIMARY KEY(memory_id, fact_id),
    FOREIGN KEY(memory_id) REFERENCES long_term_memories(id) ON DELETE CASCADE,
    FOREIGN KEY(fact_id) REFERENCES temporal_facts(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_memory_fact_link_fact ON memory_fact_links(fact_id);
"""
