import sys
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from warehouse import (Adjustment, Allocation, DistrictQuota, GateEvent,
                       Request, Warehouse)


def make_transfer_warehouse() -> Warehouse:
    """D-A 100 / D-B 100，调剂 D-A→D-B 上限 50，有效期 11-02 至 11-05。"""
    wh = Warehouse()
    wh.add_quota(DistrictQuota("D-A", "2026-11", 100))
    wh.add_quota(DistrictQuota("D-B", "2026-11", 100))
    wh.add_adjustment(Adjustment("ADJ-1", "D-A", "D-B", 50,
                                 date(2026, 11, 2), date(2026, 11, 5)))
    return wh


class AdjustmentTests(unittest.TestCase):
    def test_transfer_funds_over_quota_consumption_within_limit(self):
        wh = make_transfer_warehouse()
        dec = wh.book_request(Request("Q1", "H-B", "D-B", 140, "2026-11",
                                      plots=["P-1"],
                                      window_from=date(2026, 11, 3),
                                      window_to=date(2026, 11, 3)),
                              at=date(2026, 11, 2))
        self.assertTrue(dec.approved)  # 自有 100 + 调剂 50 = 150 ≥ 140
        wh.post_delivery(GateEvent("G1", "GATE-X", date(2026, 11, 3), "2026-11", 140))
        wh.post_allocation(Allocation("G1", "H-B", 140, date(2026, 11, 3)))
        settle = wh.district_settlement("D-B", "2026-11")
        # 耗用越出自有配额 40，由当时有效的调剂覆盖
        self.assertEqual(settle["transfers_in"][0]["used_m3"], 40)
        self.assertEqual(settle["unfunded_m3"], 0)
        self.assertEqual(settle["remaining_m3"], 0)

    def test_expired_transfer_does_not_pull_back_legal_consumption(self):
        wh = make_transfer_warehouse()
        wh.book_request(Request("Q1", "H-B", "D-B", 140, "2026-11"),
                        at=date(2026, 11, 2), allow_incomplete=True)
        wh.post_delivery(GateEvent("G1", "GATE-X", date(2026, 11, 3), "2026-11", 140))
        wh.post_allocation(Allocation("G1", "H-B", 140, date(2026, 11, 3)))
        # 11-05 到期后：结算口径不变，当时合法耗用不退回水权池
        settle = wh.district_settlement("D-B", "2026-11")
        self.assertEqual(settle["transfers_in"][0]["used_m3"], 40)
        self.assertEqual(settle["unfunded_m3"], 0)
        # 但到期调剂不再提供可许诺余量
        avail = wh.availability("D-B", "2026-11", date(2026, 11, 9))
        self.assertEqual(avail.transfer_in_remaining_m3, 0)
        self.assertFalse(wh.permit_check("H-B", "D-B", 1, date(2026, 11, 9)).approved)

    def test_transfer_cap_and_shortage_listing(self):
        wh = make_transfer_warehouse()
        wh.book_request(Request("Q1", "H-B1", "D-B", 120, "2026-11"),
                        at=date(2026, 11, 2), allow_incomplete=True)
        wh.register_holder("H-B2", "D-B")
        wh.post_delivery(GateEvent("G1", "GATE-X", date(2026, 11, 3), "2026-11", 200))
        wh.post_allocation(Allocation("G1", "H-B2", 130, date(2026, 11, 3)))
        wh.post_allocation(Allocation("G1", "H-B1", 50, date(2026, 11, 3)))
        # 还有 20 未归户，不触发结清，Q1 仍在册
        report = wh.shortage_report("D-B", "2026-11")
        self.assertTrue(report["shortage"])
        # 越出 80，调剂上限 50，未覆盖 30
        self.assertEqual(report["unfunded_m3"], 30)
        by_holder = {r["holder"]: r["m3"] for r in report["consumption_by_holder"]}
        self.assertEqual(by_holder, {"H-B1": 50, "H-B2": 130})
        self.assertEqual(report["pending_requests"],
                         [{"id": "Q1", "holder": "H-B1", "m3": 120, "open_m3": 70}])
        # 已确认放水不抹掉
        self.assertEqual(wh.deliveries["G1"].reported_total_m3, 200)

    def test_retroactive_revocation_posts_holder_differences(self):
        wh = make_transfer_warehouse()
        wh.book_request(Request("Q1", "H-B", "D-B", 140, "2026-11"),
                        at=date(2026, 11, 2), allow_incomplete=True)
        wh.post_delivery(GateEvent("G1", "GATE-X", date(2026, 11, 3), "2026-11", 140))
        wh.post_allocation(Allocation("G1", "H-B", 140, date(2026, 11, 3)))
        wh.close_period("2026-11", at=date(2026, 11, 30))
        self.assertEqual(wh.voucher("D-B", "2026-11").transfer_in_used_m3, 40)
        # 追溯撤销：当时由调剂覆盖的 40 失去来源，记权利户差额
        entries = wh.revoke_adjustment("ADJ-1", date(2026, 12, 1), retroactive=True)
        self.assertEqual(len(entries), 1)
        self.assertEqual((entries[0].holder, entries[0].m3), ("H-B", 40))
        self.assertEqual(entries[0].reason, "retroactive-revocation")
        # 已签凭据保留不改
        self.assertEqual(wh.voucher("D-B", "2026-11").transfer_in_used_m3, 40)
        # 台账现值按户反映
        self.assertEqual(wh.holder_position("H-B", "2026-11")["unfunded_m3"], 40)
        self.assertEqual(wh.validate_period("2026-11"), [])

    def test_non_retroactive_revocation_only_closes_the_future(self):
        wh = make_transfer_warehouse()
        wh.adjustments.clear()
        wh.add_adjustment(Adjustment("ADJ-2", "D-A", "D-B", 50,
                                     date(2026, 11, 2), date(2026, 11, 10)))
        wh.revoke_adjustment("ADJ-2", date(2026, 11, 6), retroactive=False)
        adj = wh.adjustments[0]
        self.assertTrue(adj.active_on(date(2026, 11, 5)))
        self.assertFalse(adj.active_on(date(2026, 11, 6)))
        self.assertFalse(adj.active_on(date(2026, 11, 7)))


if __name__ == "__main__":
    unittest.main()
