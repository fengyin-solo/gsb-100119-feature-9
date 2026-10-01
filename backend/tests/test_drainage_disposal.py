"""排水设施处置契约测试：边界分支、现场复测裁决、事务回滚与中断续传。"""
from __future__ import annotations

import unittest

from app.services.drainage import (
    HISTORY_MODULE,
    INTERRUPTION_MODULE,
    MODULE,
    MONITOR_MODULE,
    RESET_MODULE,
    TODO_MODULE,
    DrainageService,
)
from app.store import store

RELATED_TABLES = [
    MODULE,
    HISTORY_MODULE,
    RESET_MODULE,
    TODO_MODULE,
    MONITOR_MODULE,
    INTERRUPTION_MODULE,
]


class DrainageDisposalTest(unittest.TestCase):
    def setUp(self) -> None:
        for table in RELATED_TABLES:
            store.rows(table).clear()
        self.service = DrainageService()

    def count(self, table: str) -> int:
        return len(store.rows(table))

    def find_ledger(self, facility_code: str):
        return next((row for row in store.rows(MODULE) if row.get("设施编号") == facility_code), None)

    def valid_payload(self, facility_code: str = "D-TEST-001", observed_at: str = "2026-10-01T08:00") -> dict:
        return {
            "facility_code": facility_code,
            "facility_type": "雨水口",
            "road_name": "环城北路",
            "station": "K3+200",
            "observed_at": observed_at,
            "observations": {
                "monitoring": {"conclusion": "淤积", "observed_at": observed_at},
                "manual": {"conclusion": "堵塞", "observed_at": observed_at},
            },
        }

    def test_empty_and_boundary_branches_are_separate_from_normal_drainage(self) -> None:
        cases = [
            (
                {
                    "observed_at": "2026-10-01T08:01",
                    "observations": {"monitoring": {"conclusion": "堵塞"}},
                },
                "FACILITY_CODE_MISSING",
                "设施编号缺失",
            ),
            (
                {
                    "facility_code": "D-NO-WATER",
                    "observed_at": "2026-10-01T08:02",
                    "observations": {"monitoring": {"conclusion": "无积水"}},
                },
                "NO_STANDING_WATER",
                "无积水",
            ),
            (
                {
                    "facility_code": "D-PUMP",
                    "observed_at": "2026-10-01T08:03",
                    "pump_status": "离线",
                    "observations": {"monitoring": {"conclusion": "正常排水"}},
                },
                "PUMP_OFFLINE",
                "泵站离线",
            ),
            (
                {
                    "facility_code": "D-TIMEOUT",
                    "observed_at": "2026-10-01T08:04",
                    "observations": {"manual": {"conclusion": "淤积", "retest_status": "超时"}},
                },
                "RETEST_TIMEOUT",
                "复测超时",
            ),
        ]

        for payload, reason_code, branch_status in cases:
            with self.subTest(reason_code=reason_code):
                result = self.service.submit_disposal(payload)
                self.assertTrue(result["resettable"])
                self.assertEqual(result["reasonCode"], reason_code)
                self.assertEqual(result["branchStatus"], branch_status)
                self.assertEqual(self.count(TODO_MODULE), 0)
                self.assertEqual(self.count(MONITOR_MODULE), 0)

        normal = self.service.submit_disposal(
            {
                "facility_code": "D-NORMAL",
                "observed_at": "2026-10-01T08:05",
                "observations": {"monitoring": {"conclusion": "正常排水"}},
            }
        )
        self.assertFalse(normal["resettable"])
        self.assertEqual(normal["reasonCode"], "NORMAL_DRAINAGE")
        self.assertEqual(self.count(RESET_MODULE), 4)
        self.assertEqual(self.find_ledger("D-NORMAL")["status"], "正常")

    def test_manual_retest_overrides_monitoring_and_keeps_both_at_observation_time(self) -> None:
        result = self.service.submit_disposal(self.valid_payload())

        self.assertTrue(result["ok"])
        self.assertEqual(result["effectiveSource"], "人工复测")
        self.assertTrue(result["conflict"])
        ledger = self.find_ledger("D-TEST-001")
        self.assertEqual(ledger["status"], "堵塞")
        self.assertEqual(ledger["监测结论"], "淤积")
        self.assertEqual(ledger["人工复测结论"], "堵塞")
        history = store.rows(HISTORY_MODULE)[0]
        self.assertEqual(history["观测时点"], "2026-10-01T08:00")
        self.assertEqual(history["处置结论"], "堵塞")

    def test_all_three_conclusion_writes_share_one_transaction(self) -> None:
        for write_point in ("history", "ledger", "todo", "monitor"):
            payload = self.valid_payload(f"D-FAIL-{write_point}", f"2026-10-01T09:{write_point}")
            result = self.service.submit_disposal(payload, fail_once=write_point)

            self.assertTrue(result["interrupted"])
            expected_reason = "MONITORING_LIST_WRITE_FAILED" if write_point == "monitor" else f"{write_point.upper()}_WRITE_FAILED"
            self.assertEqual(result["reasonCode"], expected_reason)
            self.assertIsNone(self.find_ledger(f"D-FAIL-{write_point}"))
            self.assertFalse(
                any(row.get("设施编号") == f"D-FAIL-{write_point}" for row in store.rows(TODO_MODULE))
            )
            self.assertFalse(
                any(row.get("设施编号") == f"D-FAIL-{write_point}" for row in store.rows(MONITOR_MODULE))
            )
            self.assertFalse(
                any(row.get("设施编号") == f"D-FAIL-{write_point}" for row in store.rows(HISTORY_MODULE))
            )

        self.assertEqual(self.count(INTERRUPTION_MODULE), 4)

    def test_interruption_resumes_from_failed_branch_and_duplicate_is_idempotent(self) -> None:
        payload = self.valid_payload("D-RESUME", "2026-10-01T10:00")
        interrupted = self.service.submit_disposal(payload, fail_once="todo")
        self.assertTrue(interrupted["interrupted"])
        interruption_id = interrupted["interruptionId"]

        repeated = self.service.submit_disposal(payload)
        self.assertTrue(repeated["interrupted"])
        self.assertEqual(repeated["interruptionId"], interruption_id)
        self.assertEqual(self.count(INTERRUPTION_MODULE), 1)

        resumed = self.service.resume_interruption(interruption_id)
        self.assertTrue(resumed["ok"])
        self.assertEqual(self.find_ledger("D-RESUME")["status"], "堵塞")
        self.assertEqual(self.count(HISTORY_MODULE), 1)
        self.assertEqual(self.count(TODO_MODULE), 1)
        self.assertEqual(self.count(MONITOR_MODULE), 1)

        duplicate = self.service.submit_disposal(payload)
        self.assertTrue(duplicate["ok"])
        self.assertEqual(duplicate["written"]["todo"], resumed["written"]["todo"])
        self.assertEqual(self.count(HISTORY_MODULE), 1)
        self.assertEqual(self.count(TODO_MODULE), 1)
        self.assertEqual(self.count(MONITOR_MODULE), 1)

    def test_different_observation_times_create_separate_history(self) -> None:
        first = self.service.submit_disposal(self.valid_payload("D-TIME", "2026-10-01T10:01"))
        second = self.service.submit_disposal(self.valid_payload("D-TIME", "2026-10-01T10:02"))
        self.assertNotEqual(first["idempotencyKey"], second["idempotencyKey"])
        self.assertEqual(self.count(HISTORY_MODULE), 2)
        self.assertEqual(self.count(TODO_MODULE), 2)
        self.assertEqual(self.count(MONITOR_MODULE), 2)


if __name__ == "__main__":
    unittest.main()
