import datetime
import json
import unittest

import support  # noqa: F401 - 安装轻量级 AstrBot 测试替身
from core.models import DayRecord
from componentfixtures import RefreshComponent


class RefreshOutfitTest(unittest.TestCase):
    def test_pending_commitment_outfit_activates_before_change_time(self):
        day = DayRecord(
            date="2026-08-06",
            meta={
                "pending_commitment_outfit": json.dumps(
                    {
                        "date": "2026-08-06",
                        "instruction": "适合傍晚同行的外出穿搭",
                        "effective_time": "17:30",
                    }
                )
            },
        )

        early, effective_at = RefreshComponent._pending_commitment_outfit(
            day, datetime.datetime(2026, 8, 6, 14, 0)
        )
        due, _ = RefreshComponent._pending_commitment_outfit(
            day, datetime.datetime(2026, 8, 6, 16, 5)
        )

        self.assertEqual(early, "")
        self.assertEqual(effective_at, datetime.datetime(2026, 8, 6, 17, 30))
        self.assertEqual(due, "适合傍晚同行的外出穿搭")


if __name__ == "__main__":
    unittest.main()
