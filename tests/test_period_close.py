import sys
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from warehouse import (Allocation, DistrictQuota, GateEvent, Request, Warehouse)


def make_warehouse() -> Warehouse:
    wh = Warehouse()
    wh.add_quota(DistrictQuota("D-A", "2026-11", 500))
    wh.add_quota(DistrictQuota("D-B", "2026-11", 300))
    return wh


class DailyCloseTests(unittest.TestCase):
    def test_incomplete_sections_are_queryable_until_splits_arrive(self):
        wh = make_warehouse()
        wh.book_request(Request("Q1", "H-A", "D-A", 200, "2026-11"),
                        at=date(2026, 11, 2), allow_incomplete=True)
        wh.book_request(Request("Q2", "H-B", "D-B", 180, "2026-11"),
                        at=date(2026, 11, 2), allow_incomplete=True)
        wh.post_delivery(GateEvent("G1", "GATE-1", date(2026, 11, 4), "2026-11", 210))
        wh.post_allocation(Allocation("G1", "H-A", 130, date(2026, 11, 4)))
        # 支渠分量未齐：渠段列入 pending，可查
        record = wh.close_day(date(2026, 11, 4))
        self.assertEqual(record["reconciled"], [])
        self.assertEqual(record["pending"], ["GATE-1"])
        self.assertEqual(record["sections"]["GATE-1"]["unallocated_m3"], 80)
        self.assertEqual(wh.incomplete_sections(),
                         [{"meter": "GATE-1", "date": "2026-11-04",
                           "delivery": "G1", "unallocated_m3": 80}])
        # 迟到补报到达后渠段齐全，日结先核对完整渠段
        wh.post_allocation(Allocation("G1", "H-B", 80, date(2026, 11, 9)))
        record = wh.close_day(date(2026, 11, 4))
        self.assertEqual(record["reconciled"], ["GATE-1"])
        self.assertEqual(record["pending"], [])
        self.assertEqual(wh.incomplete_sections(), [])


class PeriodCloseTests(unittest.TestCase):
    def make_closed_warehouse(self) -> Warehouse:
        wh = make_warehouse()
        wh.book_request(Request("Q1", "H-A", "D-A", 100, "2026-11"),
                        at=date(2026, 11, 2), allow_incomplete=True)
        wh.register_holder("H-A2", "D-A")
        wh.post_delivery(GateEvent("G1", "GATE-X", date(2026, 11, 4), "2026-11", 100))
        wh.post_allocation(Allocation("G1", "H-A", 100, date(2026, 11, 4)))
        wh.close_period("2026-11", at=date(2026, 11, 30))
        return wh

    def test_voucher_issued_and_period_closed_to_new_bookings(self):
        wh = self.make_closed_warehouse()
        voucher = wh.voucher("D-A", "2026-11")
        self.assertEqual(voucher.consumption_m3, 100)
        self.assertEqual(voucher.remaining_m3, 400)
        self.assertEqual(voucher.holders["H-A"]["consumed"], 100)
        dec = wh.book_request(Request("Q9", "H-A", "D-A", 10, "2026-11"),
                              at=date(2026, 12, 1), allow_incomplete=True)
        self.assertFalse(dec.approved)
        self.assertIn("已关闭", dec.reason)

    def test_late_metering_after_close_posts_holder_difference(self):
        wh = self.make_closed_warehouse()
        # 上一期的闸次与分量迟到：走权利户差额，凭据不动
        wh.post_delivery(GateEvent("G2", "GATE-X", date(2026, 11, 20), "2026-11", 40))
        wh.post_allocation(Allocation("G2", "H-A", 40, date(2026, 12, 2)))
        diffs = [e for e in wh.differences if e.reason == "late-allocation"]
        self.assertEqual(len(diffs), 1)
        self.assertEqual((diffs[0].holder, diffs[0].m3), ("H-A", 40))
        balance = wh.holder_balance("H-A", "2026-11")
        self.assertEqual(balance["voucher"]["consumed"], 100)
        self.assertEqual(balance["reconciled_consumption_m3"], 140)
        self.assertEqual(wh.voucher("D-A", "2026-11").consumption_m3, 100)
        self.assertEqual(wh.validate_period("2026-11"), [])

    def test_correction_keeps_district_total_but_fixes_holders(self):
        wh = self.make_closed_warehouse()
        wh.post_delivery(GateEvent("G2", "GATE-X", date(2026, 11, 20), "2026-11", 40))
        wh.post_allocation(Allocation("G2", "H-A", 40, date(2026, 12, 2)))
        # 误归更正：20 从 H-A 改归 H-A2，区级合计不变
        wh.apply_correction("G2", "H-A", "H-A2", 20, date(2026, 12, 5))
        correction = [e for e in wh.differences if e.reason == "correction"]
        self.assertEqual({e.holder: e.m3 for e in correction},
                         {"H-A": -20, "H-A2": 20})
        # 合计相同不能盖住户间错误：户级差额各自在案
        self.assertEqual(sum(e.m3 for e in correction), 0)
        self.assertEqual(wh.holder_balance("H-A", "2026-11")
                         ["reconciled_consumption_m3"], 120)
        self.assertEqual(wh.holder_balance("H-A2", "2026-11")
                         ["reconciled_consumption_m3"], 20)
        self.assertEqual(wh._district_consumption("D-A", "2026-11"), 140)
        self.assertEqual(wh.validate_period("2026-11"), [])


if __name__ == "__main__":
    unittest.main()
