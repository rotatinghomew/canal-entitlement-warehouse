import sys
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from warehouse import Allocation, GateEvent, load_facts

DATA = Path(__file__).resolve().parents[1] / "data" / "facts.json"


class FixtureFlowTests(unittest.TestCase):
    """用 data/facts.json 走全流程：认定 → 预约 → 归户 → 日结 → 期结 → 差额。"""

    def test_end_to_end(self):
        wh = load_facts(DATA)

        # 放水认定：换表后新表底数 0，认定 210，与闸次单一致
        event = wh.deliveries["G1"]
        self.assertEqual(event.derived_total_m3, 210)
        self.assertEqual(event.reconciliation, "confirmed")
        self.assertEqual(event.status, "allocated")

        # 迟到补报后预约结余释放
        self.assertEqual(wh.requests["Q1"].state, "released")
        self.assertEqual(wh.requests["Q1"].released_m3, 70)
        self.assertEqual(wh.requests["Q2"].released_m3, 100)
        pos = wh.holder_position("H-A", "2026-11")
        self.assertEqual(pos["consumed_m3"], 130)
        self.assertEqual(pos["overuse_m3"], 0)

        # 历史资料缺服务地块/取水时段，登记时留了警告
        self.assertEqual(len(wh.warnings), 2)

        # 站务查下一次许可（调剂 11-05 已到期）
        self.assertEqual(
            wh.availability("D-A", "2026-11", date(2026, 11, 10)).available_m3, 370)
        self.assertEqual(
            wh.availability("D-B", "2026-11", date(2026, 11, 10)).available_m3, 220)
        self.assertTrue(wh.permit_check("H-A", "D-A", 370, date(2026, 11, 10)).approved)
        dec = wh.permit_check("H-A", "D-A", 371, date(2026, 11, 10))
        self.assertFalse(dec.approved)
        self.assertEqual(dec.consumption,
                         [{"holder": "H-A", "m3": 130, "by_delivery": {"G1": 130}}])

        # 水权岗对结算
        settle = wh.district_settlement("D-B", "2026-11")
        self.assertEqual(settle["consumption_m3"], 80)
        self.assertEqual(settle["transfers_in"][0]["used_m3"], 0)
        self.assertEqual(settle["remaining_m3"], 220)

        # 期结签出凭据，按户勾稽
        vouchers = {v.district: v for v in wh.close_period("2026-11", at=date(2026, 11, 30))}
        self.assertEqual(vouchers["D-A"].remaining_m3, 370)
        self.assertEqual(vouchers["D-A"].holders["H-A"]["consumed"], 130)
        self.assertEqual(wh.validate_period("2026-11"), [])

        # 期结后迟到的计量走权利户差额，凭据保留
        wh.post_delivery(GateEvent("G2", "GATE-X", date(2026, 11, 20), "2026-11", 50))
        wh.post_allocation(Allocation("G2", "H-A", 50, date(2026, 12, 2)))
        balance = wh.holder_balance("H-A", "2026-11")
        self.assertEqual(balance["reconciled_consumption_m3"], 180)
        self.assertEqual(wh.voucher("D-A", "2026-11").consumption_m3, 130)
        self.assertEqual(wh.validate_period("2026-11"), [])

        # 已确认放水始终不抹掉
        self.assertEqual(wh.deliveries["G1"].derived_total_m3, 210)


if __name__ == "__main__":
    unittest.main()
