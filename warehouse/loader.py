"""载入本地合成资料（data/facts.json）建账。

资料为时点快照，按业务时序入账：量表口径与读数 → 配额 → 调剂 →
预约 → 闸次 → 归户分量（含迟到补报，按 received_at 先后入账）。
历史资料缺服务地块/取水时段的预约按原样登记并记入 warnings。
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from .ledger import Warehouse
from .model import (Adjustment, Allocation, DistrictQuota, GateEvent,
                    MeterRevision, Reading, Request, parse_date, parse_dt,
                    period_of)


def load_facts(path: str | Path) -> Warehouse:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    wh = Warehouse()

    for row in raw.get("meter_revisions", []):
        wh.meters.add_revision(MeterRevision(
            meter=row["meter"], revision=row["revision"],
            unit=row.get("unit", "m3"), direction=row.get("direction", "cumulative"),
            effective_at=parse_dt(row["effective_at"]) if row.get("effective_at") else None,
            initial_value=row.get("initial_value", 0.0)))
    for row in raw.get("readings", []):
        wh.meters.add_reading(Reading(
            id=row["id"], meter=row["meter"], revision=row["revision"],
            at=parse_dt(row["at"]), value=row["value"]))
    for row in raw.get("district_quota", []):
        wh.add_quota(DistrictQuota(district=row["district"], period=row["period"],
                                   quota_m3=row["quota_m3"]))

    adjustments = raw.get("adjustment") or []
    if isinstance(adjustments, dict):
        adjustments = [adjustments]
    for i, row in enumerate(adjustments, 1):
        wh.add_adjustment(Adjustment(
            id=row.get("id", f"ADJ-{i}"),
            from_district=row["from"], to_district=row["to"],
            limit_m3=row["limit_m3"],
            valid_from=parse_date(row["valid_from"]),
            valid_to=parse_date(row["valid_to"]),
            state=row.get("state", "active")))

    default_booked = min((a.valid_from for a in wh.adjustments), default=None)
    for row in raw.get("requests", []):
        periods = sorted({p for (d, p) in wh.quotas if d == row["district"]})
        period = row.get("period") or (periods[0] if len(periods) == 1 else None)
        if period is None:
            raise ValueError(f"{row['id']} 无法确定结算期")
        booked_at = (parse_date(row["booked_at"]) if row.get("booked_at")
                     else default_booked
                     or date(int(period[:4]), int(period[5:7]), 1))
        req = Request(
            id=row["id"], holder=row["holder"], district=row["district"],
            m3=row["m3"], period=period,
            plots=list(row.get("plots", [])),
            window_from=parse_date(row["window_from"]) if row.get("window_from") else None,
            window_to=parse_date(row["window_to"]) if row.get("window_to") else None,
            booked_at=booked_at)
        decision = wh.book_request(req, allow_incomplete=True)
        if not decision.approved:
            raise ValueError(f"预约登记被拒：{row['id']} {decision.reason}")
        if not req.plots or not req.window_from or not req.window_to:
            wh.warnings.append(f"{req.id} 缺少服务地块或取水时段，按历史资料登记")

    for row in raw.get("delivery", []):
        day = parse_date(row["period"])
        wh.post_delivery(GateEvent(
            id=row["id"], meter=row["meter"], date=day, period=period_of(day),
            reported_total_m3=row["total_m3"], districts=row.get("districts")))

    allocations = []
    for row in raw.get("late_allocation", []):
        received = (parse_date(row["received_at"]) if row.get("received_at")
                    else parse_date(next(d["period"] for d in raw["delivery"]
                                         if d["id"] == row["delivery"])))
        allocations.append(Allocation(
            delivery=row["delivery"], holder=row["holder"], m3=row["m3"],
            received_at=received, kind=row.get("kind", "split-report")))
    for alloc in sorted(allocations, key=lambda a: (a.received_at, a.holder)):
        wh.post_allocation(alloc)

    return wh
