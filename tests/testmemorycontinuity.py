"""使用真实 SQLite 检查记忆归属、幂等写入及来源修订。"""

import tempfile
import sqlite3
import unittest

from support import LifeArchive, LongTermMemoryRecord


class MemoryContinuityTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.archive = LifeArchive(f"{self.directory.name}/daily_life.db")

    async def asyncTearDown(self):
        self.archive.close()
        self.directory.cleanup()

    async def save(self, **values):
        return await self.archive.upsert_long_term_memory(LongTermMemoryRecord(**values))

    async def test_self_memory_is_visible_while_other_private_scopes_are_isolated(self):
        own = await self.save(scope="global", category="autobiography", title="电影", content="我昨晚自己看了电影", source_table="continuous_life_entries", source_id="one")
        shared = await self.save(scope="friend:1", category="chat_summary", title="电影", content="我们一起看了电影")
        await self.save(scope="friend:2", category="chat_summary", content="另一个朋友的电影秘密")
        await self.save(scope="global", category="chat_summary", content="错误放在 global 的私人电影记录")
        await self.save(scope="global", category="autobiography", content="没有自身生活来源的电影记录")
        hits = await self.archive.search_long_term_memories("电影", scopes=["friend:1"], limit=12)
        self.assertEqual({item.id for item in hits}, {own.id, shared.id})
        all_visible = await self.archive.list_recent_long_term_memories(scopes=["friend:1"], limit=0)
        self.assertEqual({item.id for item in all_visible}, {own.id, shared.id})

    async def test_repeated_upsert_returns_right_record_and_keeps_distinct_counts(self):
        first = await self.save(scope="friend:1", category="episode", title="看电影", content="一起看到结尾", source_table="life_episodes", source_id="one")
        second = await self.save(scope="friend:1", category="episode", title="晚饭", content="后来吃了晚饭", source_table="life_episodes", source_id="two")
        for _ in range(3):
            saved = await self.archive.upsert_long_term_memory(first)
            self.assertEqual(saved.id, first.id)
        entities = await self.archive.get_memory_entities(scopes=["friend:1"], limit=10)
        clusters = await self.archive.get_memory_episode_clusters(scopes=["friend:1"], limit=10)
        self.assertEqual({item.name: item.mention_count for item in entities}, {"看电影": 1, "晚饭": 1})
        self.assertEqual({item.title: item.memory_count for item in clusters}, {"episode:看电影": 1, "episode:晚饭": 1})
        self.assertNotEqual(first.id, second.id)
        orders = self.archive._conn.execute("SELECT sort_order FROM memory_episode_cluster_items WHERE memory_id=?", (first.id,)).fetchall()
        self.assertEqual([row[0] for row in orders], [0])

    async def test_source_revision_with_different_title_stays_in_same_episode(self):
        first = await self.save(scope="friend:1", category="episode", title="准备电影", content="我们约好一起看", source_table="life_episodes", source_id="shared-film", date="2026-10-12")
        second = await self.save(scope="friend:1", category="episode", title="电影看完", content="后来一起看到结尾", source_table="life_episodes", source_id="shared-film", date="2026-10-13")
        clusters = await self.archive.get_memory_episode_clusters(scopes=["friend:1"], limit=10)
        self.assertEqual(len(clusters), 1)
        self.assertEqual(clusters[0].memory_count, 2)
        self.assertEqual(clusters[0].first_date, "2026-10-12")
        self.assertEqual(clusters[0].last_date, "2026-10-13")
        self.assertIn("看到结尾", clusters[0].summary)
        rows = self.archive._conn.execute("SELECT memory_id FROM memory_episode_cluster_items ORDER BY sort_order").fetchall()
        self.assertEqual([row[0] for row in rows], [first.id, second.id])
        await self.archive.upsert_long_term_memory(first)
        updated = await self.archive.get_memory_episode_clusters(scopes=["friend:1"], limit=10)
        self.assertEqual(updated[0].memory_count, 2)
        self.assertIn("看到结尾", updated[0].summary)

    async def test_vector_lookup_scales_past_sqlite_parameter_limit(self):
        identifiers = [str(identifier) for identifier in range(1300)]
        await self.archive.upsert_memory_vectors([{
            "target_type": "long_term_memory", "target_id": identifier,
            "content_hash": "hash", "provider_id": "embedding", "dimensions": 2,
            "vector_json": "[1,0]",
        } for identifier in identifiers])
        self.archive._conn.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 999)
        rows = await self.archive.get_memory_vectors("long_term_memory", identifiers)
        self.assertEqual(set(rows), set(identifiers))

    async def test_replaying_old_memory_preserves_entity_date_range(self):
        latest = await self.save(scope="friend:1", category="preference", title="睡前吹干头发", content="今天又提醒我吹干", date="2026-10-13")
        earlier = await self.save(scope="friend:1", category="preference", title="睡前吹干头发", content="第一次提醒我吹干", date="2026-10-12")
        await self.archive.upsert_long_term_memory(latest)
        await self.archive.upsert_long_term_memory(earlier)
        entities = await self.archive.get_memory_entities(scopes=["friend:1"], limit=10)
        self.assertEqual(len(entities), 1)
        self.assertEqual(entities[0].first_seen, "2026-10-12")
        self.assertEqual(entities[0].last_seen, "2026-10-13")
        self.assertEqual(entities[0].mention_count, 2)

    async def test_repeated_conflict_returns_its_own_scope_record(self):
        first = None
        for scope in ["friend:1", "friend:2"]:
            await self.save(scope=scope, category="preference:general", content="平时喜欢出门")
            short = await self.save(scope=scope, category="short_term", content="今天留在家里")
            if scope == "friend:1":
                first = short
        existing = await self.archive.get_memory_conflicts(scopes=["friend:1"], limit=10)
        repeated = await self.archive._run_db(self.archive._record_memory_conflicts_unlocked, first)
        self.assertEqual([item.id for item in repeated], [item.id for item in existing])
        self.assertEqual([item.scope for item in repeated], ["friend:1"])
