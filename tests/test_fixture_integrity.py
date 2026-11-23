import json
from pathlib import Path
import unittest


class FixtureIntegrityTests(unittest.TestCase):
    def test_meter_revision_quota_and_late_allocation_are_present(self):
        data = json.loads((Path(__file__).parents[1] / "data/facts.json").read_text(encoding="utf-8"))
        self.assertEqual(len(data["meter_revisions"]), 2)
        self.assertEqual({row["district"] for row in data["district_quota"]}, {"D-A", "D-B"})
        self.assertEqual(sum(row["m3"] for row in data["late_allocation"]), 210)
        self.assertEqual(data["readings"][-1]["value"], data["delivery"][0]["total_m3"])


if __name__ == "__main__":
    unittest.main()
