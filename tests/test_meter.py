import sys
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from warehouse.meter import MeterLedger
from warehouse.model import MeterRevision, Reading


class MeterLedgerTests(unittest.TestCase):
    def test_swap_rebaselines_to_initial_value(self):
        led = MeterLedger()
        led.add_revision(MeterRevision("GATE-1", 1))
        led.add_revision(MeterRevision("GATE-1", 2, effective_at=datetime(2026, 11, 4)))
        led.add_reading(Reading("R1", "GATE-1", 1, datetime(2026, 11, 3, 8), 1000))
        led.add_reading(Reading("R2", "GATE-1", 2, datetime(2026, 11, 4, 8), 210))
        # 换表后新表底数 0：11-04 认定 210
        self.assertEqual(
            led.delivered_between("GATE-1", datetime(2026, 11, 4), datetime(2026, 11, 5)),
            210)
        # 原始口径以首读数为底：11-03 只确认增量 0
        self.assertEqual(
            led.delivered_between("GATE-1", datetime(2026, 11, 3), datetime(2026, 11, 4)),
            0.0)
        # 窗口内无任何读数：无法认定
        self.assertIsNone(
            led.delivered_between("GATE-1", datetime(2026, 11, 10), datetime(2026, 11, 11)))

    def test_reading_reversal_is_flagged_and_rebased(self):
        led = MeterLedger()
        led.add_revision(MeterRevision("M", 1))
        for i, value in enumerate([100, 150, 40, 90]):
            led.add_reading(Reading(f"R{i}", "M", 1, datetime(2026, 11, 3, 8 + i), value))
        segments = led.segments("M")
        # 50（正常增量）+ 40（反转后按归零重启认定）+ 50 = 140
        self.assertEqual(segments[0].volume, 140)
        self.assertTrue(any("反转" in a for a in led.anomalies("M")))

    def test_unknown_revision_is_rejected(self):
        led = MeterLedger()
        led.add_revision(MeterRevision("M", 1))
        led.add_reading(Reading("R0", "M", 99, datetime(2026, 11, 3, 8), 10))
        with self.assertRaises(ValueError):
            led.segments("M")


if __name__ == "__main__":
    unittest.main()
