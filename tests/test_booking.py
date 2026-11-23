import sys
import unittest
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from warehouse import (Adjustment, Allocation, DistrictQuota, GateEvent,
                       MeterRevision, Reading, Request, Warehouse)


def make_warehouse() -> Warehouse:
    """与 data/facts.json 同口径的台账：GATE-1 换表，D-A 500 / D-B 300，
    调剂 D-A→D-B 上限 100，有效期 11-02 至 11-05。"""
    wh = Warehouse()
    wh.meters.add_revision(MeterRevision("GATE-1", 1))
    wh.meters.add_revision(MeterRevision("GATE-1", 2, effective_at=datetime(2026, 11, 4)))
    wh.meters.add_reading(Reading("R1", "GATE-1", 1, datetime(2026, 11, 3, 8), 1000))
    wh.meters.add_reading(Reading("R2", "GATE-1", 2, datetime(2026, 11, 4, 8), 210))
    wh.add_quota(DistrictQuota("D-A", "2026-11", 500))
    wh.add_quota(DistrictQuota("D-B", "2026-11", 300))
    wh.add_adjustment(Adjustment("ADJ-1", "D-A", "D-B", 100,
                                 date(2026, 11, 2), date(2026, 11, 5)))
    return wh


def book_fixture_requests(wh: Warehouse) -> None:
    assert wh.book_request(Request("Q1", "H-A", "D-A", 200, "2026-11"),
                           at=date(2026, 11, 2), allow_incomplete=True).approved
    assert wh.book_request(Request("Q2", "H-B", "D-B", 180, "2026-11"),
                           at=date(2026, 11, 2), allow_incomplete=True).approved


def post_fixture_delivery(wh: Warehouse) -> GateEvent:
    return wh.post_delivery(GateEvent("G1", "GATE-1", date(2026, 11, 4),
                                      "2026-11", 210))


class BookingTests(unittest.TestCase):
    def test_booking_keeps_margin_for_concurrent_requests(self):
        wh = make_warehouse()
        dec = wh.book_request(Request("Q1", "H-A", "D-A", 200, "2026-11"),
                              at=date(2026, 11, 2), allow_incomplete=True)
        self.assertTrue(dec.approved)
        # 有效转出 100 在调剂期内占用 D-A 余量
        self.assertEqual(dec.availability.available_m3, 400)
        # 同期第二笔超额请求不得重复许诺
        dec = wh.book_request(Request("QX", "H-A", "D-A", 250, "2026-11"),
                              at=date(2026, 11, 2), allow_incomplete=True)
        self.assertFalse(dec.approved)
        self.assertEqual(dec.availability.available_m3, 200)
        self.assertEqual([p["id"] for p in dec.pending], ["Q1"])

    def test_unallocated_shared_delivery_blocks_all_holders(self):
        wh = make_warehouse()
        book_fixture_requests(wh)
        event = post_fixture_delivery(wh)
        self.assertEqual(event.reconciliation, "confirmed")
        self.assertEqual(event.status, "unallocated")
        # 未归户的 210 全额占用每个候选区：不均摊（不是 105）
        avail_a = wh.availability("D-A", "2026-11", date(2026, 11, 4))
        avail_b = wh.availability("D-B", "2026-11", date(2026, 11, 4))
        self.assertEqual(avail_a.encumbrance_m3, 210)
        self.assertEqual(avail_b.encumbrance_m3, 210)
        self.assertEqual(avail_a.available_m3, 500 - 100 - 200 - 210)
        self.assertEqual(avail_b.available_m3, 300 + 100 - 180 - 210)
        # 任何权利户都不能把这段量当成可用
        self.assertFalse(wh.permit_check("H-A", "D-A", 1, date(2026, 11, 4)).approved)
        self.assertFalse(wh.permit_check("H-B", "D-B", 11, date(2026, 11, 4)).approved)
        self.assertTrue(wh.permit_check("H-B", "D-B", 10, date(2026, 11, 4)).approved)

    def test_late_split_releases_remainder_and_reopens_booking(self):
        wh = make_warehouse()
        book_fixture_requests(wh)
        post_fixture_delivery(wh)
        wh.post_allocation(Allocation("G1", "H-A", 130, date(2026, 11, 4)))
        event = wh.deliveries["G1"]
        self.assertEqual(event.status, "partially-allocated")
        self.assertEqual(wh.requests["Q1"].state, "reserved")  # 分量未齐不结清
        # 迟到补报到达，闸次归户齐全
        wh.post_allocation(Allocation("G1", "H-B", 80, date(2026, 11, 9)))
        self.assertEqual(event.status, "allocated")
        # 预约结余释放
        self.assertEqual(wh.requests["Q1"].state, "released")
        self.assertEqual(wh.requests["Q1"].released_m3, 70)
        self.assertEqual(wh.requests["Q2"].released_m3, 100)
        # 调剂已到期，余量按自有配额减已确认耗用
        self.assertEqual(wh.availability("D-A", "2026-11", date(2026, 11, 10)).available_m3, 370)
        self.assertEqual(wh.availability("D-B", "2026-11", date(2026, 11, 10)).available_m3, 220)
        # 权利户可再预约
        self.assertTrue(wh.permit_check("H-A", "D-A", 370, date(2026, 11, 10)).approved)
        self.assertFalse(wh.permit_check("H-A", "D-A", 371, date(2026, 11, 10)).approved)

    def test_overuse_is_registered_and_delivery_is_kept(self):
        wh = make_warehouse()
        wh.book_request(Request("Q1", "H-A", "D-A", 50, "2026-11"),
                        at=date(2026, 11, 2), allow_incomplete=True)
        wh.post_delivery(GateEvent("G2", "GATE-X", date(2026, 11, 3), "2026-11", 80))
        wh.post_allocation(Allocation("G2", "H-A", 80, date(2026, 11, 3)))
        pos = wh.holder_position("H-A", "2026-11")
        self.assertEqual(pos["consumed_m3"], 80)
        self.assertEqual(pos["overuse_m3"], 30)  # 超量登记
        # 已确认放水不抹掉
        self.assertEqual(wh.deliveries["G2"].status, "allocated")
        settle = wh.district_settlement("D-A", "2026-11")
        self.assertEqual(settle["consumption_m3"], 80)
        self.assertEqual(settle["overuse_m3"], 30)

    def test_allocation_beyond_recognized_total_is_rejected(self):
        wh = make_warehouse()
        book_fixture_requests(wh)
        post_fixture_delivery(wh)
        wh.post_allocation(Allocation("G1", "H-A", 130, date(2026, 11, 4)))
        with self.assertRaises(ValueError):
            wh.post_allocation(Allocation("G1", "H-B", 81, date(2026, 11, 9)))

    def test_meter_caliber_correction_rederives_delivery(self):
        wh = make_warehouse()
        book_fixture_requests(wh)
        post_fixture_delivery(wh)
        wh.post_allocation(Allocation("G1", "H-A", 130, date(2026, 11, 4)))
        wh.post_allocation(Allocation("G1", "H-B", 80, date(2026, 11, 9)))
        # 更正换表口径：新表底数实为 10，认定量随之改为 200
        wh.apply_meter_correction("GATE-1", 2, initial_value=10)
        event = wh.deliveries["G1"]
        self.assertEqual(event.derived_total_m3, 200)
        self.assertEqual(event.reconciliation, "discrepancy")
        # 已归户分量不动，超归部分在结算与勾稽中列示
        settle = wh.district_settlement("D-A", "2026-11")
        self.assertEqual(settle["delivery_anomalies"][0]["over_allocated_m3"], 10)
        problems = wh.validate_period("2026-11")
        self.assertTrue(any("G1" in p for p in problems))


if __name__ == "__main__":
    unittest.main()
