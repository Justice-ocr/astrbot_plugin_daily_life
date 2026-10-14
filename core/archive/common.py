from __future__ import annotations

from typing import Any

from typing import Protocol


class CommonArchiveComponentContext(Protocol):
    """此组件读取或更新的宿主接口。"""

    _conn: Any
    _text: Any


class CommonArchiveComponent:
    def __init__(self, context: CommonArchiveComponentContext) -> None:
        self.context = context

    @staticmethod
    def _text(value: Any) -> str:
        return str(value or "").strip()

    @staticmethod
    def _int(value: Any, default: int = 0) -> int:
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _flag(value: Any) -> int:
        return 1 if bool(value) else 0

    def _get_ordered_texts_unlocked(
        self, table: str, owner_column: str, owner_id: str, text_column: str
    ) -> list[str]:
        return [
            row[text_column]
            for row in self.context._conn.execute(
                f"SELECT {text_column} FROM {table} WHERE {owner_column} = ? ORDER BY sort_order",
                (owner_id,),
            ).fetchall()
            if row[text_column]
        ]

    def _replace_texts_unlocked(
        self,
        table: str,
        owner_column: str,
        owner_id: int | str,
        text_column: str,
        values: list[str],
    ) -> None:
        self.context._conn.execute(f"DELETE FROM {table} WHERE {owner_column} = ?", (owner_id,))
        for idx, value in enumerate(values):
            text = self.context._text(value)
            if text:
                self.context._conn.execute(
                    f"INSERT INTO {table}({owner_column}, sort_order, {text_column}) VALUES (?, ?, ?)",
                    (owner_id, idx, text),
                )

    def _replace_people_unlocked(
        self, table: str, id_column: str, owner_id: int, people: list[str]
    ) -> None:
        self.context._conn.execute(f"DELETE FROM {table} WHERE {id_column} = ?", (owner_id,))
        for idx, person in enumerate(people):
            text = self.context._text(person)
            if text:
                self.context._conn.execute(
                    f"INSERT INTO {table}({id_column}, sort_order, person) VALUES (?, ?, ?)",
                    (owner_id, idx, text),
                )

    def _get_people_unlocked(
        self, table: str, id_column: str, owner_id: int
    ) -> list[str]:
        return [
            row["person"]
            for row in self.context._conn.execute(
                f"SELECT person FROM {table} WHERE {id_column} = ? ORDER BY sort_order",
                (owner_id,),
            ).fetchall()
            if row["person"]
        ]
