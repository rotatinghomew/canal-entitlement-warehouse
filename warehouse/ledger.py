"""灌区转供配额仓主账。

口径要点：
- 放水量由表计台账认定（换表底数、读数反转见 meter.py），闸次单只作核对；
  认定量以表计为准，分量归户不得超过认定量。
- 配额与调剂的有效性按闸次开始日判定；调剂到期不影响当时已发生的合法耗用。
- 预约登记按同期余量校验，不为重复许诺：余量 = 配额 + 有效转入剩余
  - 有效转出剩余 - 已确认耗用 - 在册预约 - 共享渠段未归户占用。
- 共享闸次分量未齐时，未归户部分按最保守口径全额占用每个候选区的余量：
  不预先均摊，也不允许任何权利户把这段量当成可用。
- 期结签出凭据后不再改动；追溯撤销、迟到计量、分量更正一律走权利户差额，
  校验按户进行——灌区合计相同不能盖住户间错误。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta

from .meter import MeterLedger
from .model import (Adjustment, Allocation, DifferenceEntry, DistrictQuota,
                    GateEvent, Request, Voucher, period_of)

EPS = 1e-9

# 差额原因影响的维度：consumption=户级耗用，funding=调剂覆盖
_DIFF_DIMENSION = {
    "late-allocation": "consumption",
    "correction": "consumption",
    "retroactive-revocation": "funding",
}


@dataclass
class Availability:
    """某区某期在某日的可许诺余量分解。"""
    district: str
    period: str
    at: date
    quota_m3: float
    transfer_in_remaining_m3: float
    transfer_out_remaining_m3: float
    confirmed_m3: float
    reserved_m3: float
    encumbrance_m3: float

    @property
    def available_m3(self) -> float:
        return (self.quota_m3
                + self.transfer_in_remaining_m3
                - self.transfer_out_remaining_m3
                - self.confirmed_m3
                - self.reserved_m3
                - self.encumbrance_m3)

    def as_dict(self) -> dict:
        return {
            "district": self.district,
            "period": self.period,
            "at": self.at.isoformat(),
            "quota_m3": self.quota_m3,
            "transfer_in_remaining_m3": self.transfer_in_remaining_m3,
            "transfer_out_remaining_m3": self.transfer_out_remaining_m3,
            "confirmed_m3": self.confirmed_m3,
            "reserved_m3": self.reserved_m3,
            "encumbrance_m3": self.encumbrance_m3,
            "available_m3": self.available_m3,
        }


@dataclass
class Decision:
    """许可决定。拒绝时附已发生耗用与待处理预约，供站务与水权岗核对。"""
    approved: bool
    reason: str
    availability: Availability | None = None
    consumption: list = field(default_factory=list)
    pending: list = field(default_factory=list)


class Warehouse:
    def __init__(self) -> None:
        self.meters = MeterLedger()
        self.quotas: dict[tuple[str, str], DistrictQuota] = {}
        self.adjustments: list[Adjustment] = []
        self.requests: dict[str, Request] = {}
        self.deliveries: dict[str, GateEvent] = {}
        self.allocations: list[Allocation] = []
        self.differences: list[DifferenceEntry] = []
        self.vouchers: dict[tuple[str, str], Voucher] = {}
        self.closed_periods: set[str] = set()
        self.daily_closes: dict[date, dict] = {}
        self.holder_district: dict[str, str] = {}
        self.warnings: list[str] = []
        self.clock: date | None = None

    # ------------------------------------------------------------------
    # 基础资料
    # ------------------------------------------------------------------
    def add_quota(self, quota: DistrictQuota) -> None:
        self.quotas[(quota.district, quota.period)] = quota

    def quota(self, district: str, period: str) -> float:
        q = self.quotas.get((district, period))
        return q.quota_m3 if q else 0.0

    def register_holder(self, holder: str, district: str) -> None:
        known = self.holder_district.setdefault(holder, district)
        if known != district:
            raise ValueError(f"权利户 {holder} 已登记在 {known}，不能改挂 {district}")

    def add_adjustment(self, adj: Adjustment) -> None:
        if adj.limit_m3 <= 0:
            raise ValueError("调剂上限必须为正")
        if adj.valid_from > adj.valid_to:
            raise ValueError("调剂有效期起止颠倒")
        self.adjustments.append(adj)

    def revoke_adjustment(self, adj_id: str, at: date, retroactive: bool = False) -> list[DifferenceEntry]:
        """撤销调剂。追溯撤销时，已签出期的户级未覆盖耗用重算并记差额。"""
        adj = self._adjustment(adj_id)
        adj.state = "revoked"
        adj.revoked_at = at
        adj.retroactive = retroactive
        self._touch(at)
        entries: list[DifferenceEntry] = []
        if retroactive:
            for (district, period), voucher in sorted(self.vouchers.items()):
                live_unfunded = self._holder_unfunded(district, period)
                for holder, snap in voucher.holders.items():
                    delta = live_unfunded.get(holder, 0.0) - snap.get("unfunded", 0.0)
                    if abs(delta) > EPS:
                        entries.append(self._post_difference(
                            holder, district, period, delta,
                            "retroactive-revocation", adj_id, at))
        return entries

    def _adjustment(self, adj_id: str) -> Adjustment:
        for adj in self.adjustments:
            if adj.id == adj_id:
                return adj
        raise ValueError(f"未知调剂 {adj_id}")

    # ------------------------------------------------------------------
    # 预约
    # ------------------------------------------------------------------
    def book_request(self, req: Request, at: date | None = None,
                     *, allow_incomplete: bool = False) -> Decision:
        """登记预约。按登记日余量校验，给同期其他请求保留不重复许诺的余量。"""
        if req.id in self.requests:
            raise ValueError(f"预约 {req.id} 已存在")
        at = at or req.booked_at or self.clock
        if at is None:
            raise ValueError("登记预约需要日期")
        if req.period in self.closed_periods:
            return Decision(False, f"结算期 {req.period} 已关闭，凭据已签出")
        if req.m3 <= 0:
            return Decision(False, "预约量必须为正")
        if not allow_incomplete and (not req.plots or not req.window_from or not req.window_to):
            return Decision(False, "取水请求须注明服务地块与取水时段")
        if req.window_from and req.window_to and req.window_from > req.window_to:
            return Decision(False, "取水时段起止颠倒")
        if req.holder in self.holder_district and self.holder_district[req.holder] != req.district:
            return Decision(False, f"权利户 {req.holder} 已登记在 {self.holder_district[req.holder]}")
        avail = self.availability(req.district, req.period, at)
        if req.m3 > avail.available_m3 + EPS:
            return Decision(False, "余量不足", avail,
                            consumption=self._consumption_listing(req.district, req.period),
                            pending=self._pending_listing(req.district, req.period))
        req.booked_at = at
        req.state = "reserved"
        self.requests[req.id] = req
        self.register_holder(req.holder, req.district)
        self._touch(at)
        return Decision(True, "ok", avail)

    def permit_check(self, holder: str, district: str, m3: float, at: date,
                     period: str | None = None) -> Decision:
        """站务查下一次许可：只查不记。"""
        period = period or period_of(at)
        avail = self.availability(district, period, at)
        if period in self.closed_periods:
            return Decision(False, f"结算期 {period} 已关闭，凭据已签出", avail)
        if m3 > avail.available_m3 + EPS:
            return Decision(False, "余量不足", avail,
                            consumption=self._consumption_listing(district, period),
                            pending=self._pending_listing(district, period))
        return Decision(True, "ok", avail)

    def availability(self, district: str, period: str, at: date) -> Availability:
        usage = self._adjustment_usage(period)
        transfer_in = transfer_out = 0.0
        for adj in self.adjustments:
            if not adj.active_on(at):
                continue
            remaining = max(0.0, adj.limit_m3 - usage.get(adj.id, 0.0))
            if adj.to_district == district:
                transfer_in += remaining
            if adj.from_district == district:
                transfer_out += remaining
        confirmed = self._district_consumption(district, period)
        reserved = sum(self._open_reserved_m3(h, period)
                       for h in self._district_holders(district, period))
        encumbrance = sum(self._unallocated(ev)
                          for ev in self.deliveries.values()
                          if ev.period == period
                          and district in self._candidate_districts(ev))
        return Availability(district, period, at,
                            quota_m3=self.quota(district, period),
                            transfer_in_remaining_m3=transfer_in,
                            transfer_out_remaining_m3=transfer_out,
                            confirmed_m3=confirmed,
                            reserved_m3=reserved,
                            encumbrance_m3=encumbrance)

    # ------------------------------------------------------------------
    # 放水认定与归户
    # ------------------------------------------------------------------
    def post_delivery(self, event: GateEvent) -> GateEvent:
        """登记闸次单并以表计台账认定放水量。"""
        if event.id in self.deliveries:
            raise ValueError(f"闸次 {event.id} 已存在")
        self._recognize(event)
        self.deliveries[event.id] = event
        self._refresh_delivery_status(event)
        self._touch(event.date)
        return event

    def post_allocation(self, alloc: Allocation) -> None:
        """登记归户分量（含迟到补报）。已确认放水不抹掉，分量不得超过认定量。"""
        ev = self.deliveries.get(alloc.delivery)
        if ev is None:
            raise ValueError(f"未知闸次 {alloc.delivery}")
        if alloc.holder not in self.holder_district:
            raise ValueError(f"权利户 {alloc.holder} 未登记，无法归户")
        recognized = self._recognized(ev)
        current = self._delivery_allocated(ev.id)
        if current + alloc.m3 > recognized + EPS:
            raise ValueError(
                f"归户分量超出闸次认定量：{ev.id} 已归 {current}，"
                f"本次 {alloc.m3}，认定量 {recognized}")
        self.allocations.append(alloc)
        self._refresh_delivery_status(ev)
        self._touch(alloc.received_at)
        if ev.period in self.closed_periods:
            reason = "late-allocation" if alloc.kind == "split-report" else "correction"
            self._post_difference(alloc.holder, self.holder_district[alloc.holder],
                                  ev.period, alloc.m3, reason, alloc.delivery,
                                  alloc.received_at)
        self._auto_settle(ev)

    def apply_correction(self, delivery_id: str, from_holder: str, to_holder: str,
                         m3: float, at: date) -> None:
        """更正误归的分量：先冲正转出户，再归到转入户。合计不变，户间各自在案。"""
        if from_holder == to_holder:
            raise ValueError("冲正双方不能是同一权利户")
        ev = self.deliveries.get(delivery_id)
        if ev is None:
            raise ValueError(f"未知闸次 {delivery_id}")
        held = sum(a.m3 for a in self.allocations
                   if a.delivery == delivery_id and a.holder == from_holder)
        if held < m3 - EPS:
            raise ValueError(f"{from_holder} 在 {delivery_id} 仅归户 {held}，不足冲正 {m3}")
        self.post_allocation(Allocation(delivery_id, from_holder, -m3, at, kind="correction"))
        self.post_allocation(Allocation(delivery_id, to_holder, m3, at, kind="correction"))

    def apply_meter_correction(self, meter: str, revision: int, *,
                               initial_value: float | None = None,
                               effective_at: datetime | None = None) -> list[GateEvent]:
        """更正换表口径：重新认定受影响闸次，已归户分量不动，差异在结算中列示。"""
        self.meters.correct_revision(meter, revision,
                                     initial_value=initial_value,
                                     effective_at=effective_at)
        affected = []
        for ev in self.deliveries.values():
            if ev.meter != meter:
                continue
            self._recognize(ev)
            self._refresh_delivery_status(ev)
            affected.append(ev)
        return affected

    def _recognize(self, ev: GateEvent) -> None:
        start = datetime.combine(ev.date, time.min)
        derived = self.meters.delivered_between(ev.meter, start, start + timedelta(days=1))
        ev.derived_total_m3 = derived
        if derived is None:
            ev.reconciliation = "unverified"
        elif abs(derived - ev.reported_total_m3) > EPS:
            ev.reconciliation = "discrepancy"
        else:
            ev.reconciliation = "confirmed"

    def _recognized(self, ev: GateEvent) -> float:
        return ev.derived_total_m3 if ev.derived_total_m3 is not None else ev.reported_total_m3

    def _delivery_allocated(self, delivery_id: str) -> float:
        return sum(a.m3 for a in self.allocations if a.delivery == delivery_id)

    def _unallocated(self, ev: GateEvent) -> float:
        return max(0.0, self._recognized(ev) - self._delivery_allocated(ev.id))

    def _candidate_districts(self, ev: GateEvent) -> set[str]:
        if ev.districts:
            return set(ev.districts)
        return {d for (d, p) in self.quotas if p == ev.period}

    def _refresh_delivery_status(self, ev: GateEvent) -> None:
        allocated = self._delivery_allocated(ev.id)
        if allocated <= EPS:
            ev.status = "unallocated"
        elif self._unallocated(ev) > EPS:
            ev.status = "partially-allocated"
        else:
            ev.status = "allocated"

    def _auto_settle(self, ev: GateEvent) -> None:
        """渠段分量齐全后，把可核耗用转成归户事实：释放结余、登记超量。"""
        for district in self._candidate_districts(ev):
            pending = [e for e in self.deliveries.values()
                       if e.period == ev.period
                       and district in self._candidate_districts(e)
                       and self._unallocated(e) > EPS]
            if pending:
                continue
            for holder in self._district_holders(district, ev.period):
                if any(r.holder == holder and r.period == ev.period and r.state == "reserved"
                       for r in self.requests.values()):
                    self.settle_holder_period(holder, ev.period)

    # ------------------------------------------------------------------
    # 结算
    # ------------------------------------------------------------------
    def settle_holder_period(self, holder: str, period: str) -> None:
        """预约量与实际耗用有出入时：释放剩余，超量部分留在台账登记。"""
        consumption = self._holder_consumption(holder, period)
        remaining = consumption
        for req in self._open_requests(holder, period):
            take = min(req.m3, max(0.0, remaining))
            remaining -= take
            req.settled_consumed_m3 = take
            if take >= req.m3 - EPS:
                req.state = "fulfilled"
                req.released_m3 = 0.0
            else:
                req.state = "released"
                req.released_m3 = req.m3 - take

    def holder_position(self, holder: str, period: str) -> dict:
        district = self.holder_district[holder]
        consumption = self._holder_consumption(holder, period)
        reqs = [r for r in self.requests.values() if r.holder == holder and r.period == period]
        reserved_total = sum(r.m3 for r in reqs)
        return {
            "holder": holder,
            "district": district,
            "period": period,
            "consumed_m3": consumption,
            "reserved_total_m3": reserved_total,
            "open_reserved_m3": self._open_reserved_m3(holder, period),
            "released_m3": sum(r.released_m3 for r in reqs),
            "overuse_m3": max(0.0, consumption - reserved_total),
            "unfunded_m3": self._holder_unfunded(district, period).get(holder, 0.0),
            "requests": [{
                "id": r.id, "m3": r.m3, "state": r.state,
                "released_m3": r.released_m3,
                "settled_consumed_m3": r.settled_consumed_m3,
            } for r in sorted(reqs, key=self._request_order)],
        }

    def district_settlement(self, district: str, period: str) -> dict:
        """水权岗对结算：配额、调剂、归户耗用、预约、占用与凭据差额。"""
        usage = self._adjustment_usage(period)
        consumption = self._district_consumption(district, period)
        transfer_in_used = sum(usage.get(a.id, 0.0) for a in self.adjustments
                               if a.to_district == district)
        transfer_out_used = sum(usage.get(a.id, 0.0) for a in self.adjustments
                                if a.from_district == district)
        unfunded = self._holder_unfunded(district, period)
        voucher = self.vouchers.get((district, period))
        return {
            "district": district,
            "period": period,
            "quota_m3": self.quota(district, period),
            "transfers_in": [self._adjustment_row(a, usage) for a in self.adjustments
                             if a.to_district == district],
            "transfers_out": [self._adjustment_row(a, usage) for a in self.adjustments
                              if a.from_district == district],
            "consumption_m3": consumption,
            "consumption_by_holder": self._consumption_listing(district, period),
            "requests": [self._request_row(r) for r in self.requests.values()
                         if r.district == district and r.period == period],
            "open_reserved_m3": sum(self._open_reserved_m3(h, period)
                                    for h in self._district_holders(district, period)),
            "overuse_m3": sum(self.holder_position(h, period)["overuse_m3"]
                              for h in self._district_holders(district, period)),
            "unfunded_m3": sum(unfunded.values()),
            "encumbrance_m3": sum(self._unallocated(ev) for ev in self.deliveries.values()
                                  if ev.period == period
                                  and district in self._candidate_districts(ev)),
            "remaining_m3": self.quota(district, period) + transfer_in_used - consumption,
            "delivery_anomalies": self._delivery_anomalies(district, period),
            "voucher": self._voucher_row(voucher) if voucher else None,
            "differences": [self._difference_row(e) for e in self.differences
                            if e.district == district and e.period == period],
        }

    def shortage_report(self, district: str, period: str) -> dict:
        """配额不足时列出已发生耗用与待处理预约；已确认放水不抹掉。"""
        settle = self.district_settlement(district, period)
        return {
            "district": district,
            "period": period,
            "shortage": settle["remaining_m3"] < -EPS or settle["unfunded_m3"] > EPS,
            "effective_quota_m3": settle["quota_m3"] + sum(
                t["used_m3"] for t in settle["transfers_in"]),
            "consumption_m3": settle["consumption_m3"],
            "consumption_by_holder": settle["consumption_by_holder"],
            "pending_requests": self._pending_listing(district, period),
            "unfunded_m3": settle["unfunded_m3"],
            "note": "已确认放水不抹掉；缺口按户列示，期结后走差额",
        }

    # ------------------------------------------------------------------
    # 日结
    # ------------------------------------------------------------------
    def close_day(self, day: date) -> dict:
        """日结：完整渠段先核对，未齐渠段列入 pending 可查。"""
        sections: dict[str, dict] = {}
        for ev in self.deliveries.values():
            if ev.date != day:
                continue
            sec = sections.setdefault(ev.meter, {
                "deliveries": [], "recognized_m3": 0.0,
                "allocated_m3": 0.0, "unallocated_m3": 0.0,
            })
            recognized = self._recognized(ev)
            allocated = self._delivery_allocated(ev.id)
            sec["deliveries"].append({
                "id": ev.id, "recognized_m3": recognized,
                "allocated_m3": allocated, "status": ev.status,
                "reconciliation": ev.reconciliation,
            })
            sec["recognized_m3"] += recognized
            sec["allocated_m3"] += allocated
            sec["unallocated_m3"] += self._unallocated(ev)
        record = {
            "date": day.isoformat(),
            "sections": sections,
            "reconciled": sorted(m for m, s in sections.items()
                                 if s["unallocated_m3"] <= EPS),
            "pending": sorted(m for m, s in sections.items()
                              if s["unallocated_m3"] > EPS),
        }
        self.daily_closes[day] = record
        self._touch(day)
        return record

    def incomplete_sections(self) -> list[dict]:
        """未齐渠段实时查询。"""
        out = []
        for ev in self.deliveries.values():
            unallocated = self._unallocated(ev)
            if unallocated > EPS:
                out.append({"meter": ev.meter, "date": ev.date.isoformat(),
                            "delivery": ev.id, "unallocated_m3": unallocated})
        return sorted(out, key=lambda r: (r["date"], r["meter"]))

    # ------------------------------------------------------------------
    # 期结与凭据
    # ------------------------------------------------------------------
    def close_period(self, period: str, at: date | None = None) -> list[Voucher]:
        """期结：结清全部权利户，签出配额凭据。凭据签出后保留，不再改动。"""
        if period in self.closed_periods:
            raise ValueError(f"结算期 {period} 已关闭")
        at = at or self.clock
        if at is None:
            raise ValueError("期结需要结账日期")
        usage = self._adjustment_usage(period)
        vouchers: list[Voucher] = []
        for (district, p), quota in sorted(self.quotas.items()):
            if p != period:
                continue
            holders = self._district_holders(district, period)
            for holder in holders:
                self.settle_holder_period(holder, period)
            consumption = self._district_consumption(district, period)
            transfer_in_used = sum(usage.get(a.id, 0.0) for a in self.adjustments
                                   if a.to_district == district)
            transfer_out_used = sum(usage.get(a.id, 0.0) for a in self.adjustments
                                    if a.from_district == district)
            unfunded = self._holder_unfunded(district, period)
            snapshots = {}
            for holder in holders:
                pos = self.holder_position(holder, period)
                snapshots[holder] = {
                    "consumed": pos["consumed_m3"],
                    "reserved": pos["reserved_total_m3"],
                    "released": pos["released_m3"],
                    "overuse": pos["overuse_m3"],
                    "unfunded": unfunded.get(holder, 0.0),
                }
            voucher = Voucher(
                district=district, period=period,
                serial=f"{district}/{period}/1", issued_at=at,
                quota_m3=quota.quota_m3,
                transfer_in_used_m3=transfer_in_used,
                transfer_out_used_m3=transfer_out_used,
                consumption_m3=consumption,
                unfunded_m3=sum(unfunded.values()),
                overuse_m3=sum(s["overuse"] for s in snapshots.values()),
                remaining_m3=quota.quota_m3 + transfer_in_used - consumption,
                holders=snapshots)
            self.vouchers[(district, period)] = voucher
            vouchers.append(voucher)
        self.closed_periods.add(period)
        self._touch(at)
        return vouchers

    def voucher(self, district: str, period: str) -> Voucher | None:
        return self.vouchers.get((district, period))

    def holder_balance(self, holder: str, period: str) -> dict:
        """户级余额：已签凭据 + 差额 = 当前台账值。"""
        district = self.holder_district[holder]
        voucher = self.vouchers.get((district, period))
        diffs = [e for e in self.differences if e.holder == holder and e.period == period]
        result = {
            "holder": holder,
            "district": district,
            "period": period,
            "current": self.holder_position(holder, period),
            "differences": [self._difference_row(e) for e in diffs],
            "voucher": voucher.holders.get(holder) if voucher else None,
            "reconciled_consumption_m3": None,
        }
        if voucher:
            base = voucher.holders.get(holder, {}).get("consumed", 0.0)
            result["reconciled_consumption_m3"] = base + sum(
                e.m3 for e in diffs if _DIFF_DIMENSION.get(e.reason) == "consumption")
        return result

    def validate_period(self, period: str) -> list[str]:
        """按户勾稽：凭据+差额=台账现值。灌区合计相同不能盖住户间错误。"""
        problems: list[str] = []
        seen_deliveries: set[str] = set()
        for (district, p), _quota in sorted(self.quotas.items()):
            if p != period:
                continue
            holders = self._district_holders(district, period)
            live = {h: self._holder_consumption(h, p) for h in holders}
            voucher = self.vouchers.get((district, p))
            if voucher is not None:
                for holder in sorted(set(live) | set(voucher.holders)):
                    base = voucher.holders.get(holder, {}).get("consumed", 0.0)
                    diff = self._difference_sum(holder, p, "consumption")
                    if abs(base + diff - live.get(holder, 0.0)) > EPS:
                        problems.append(
                            f"{district}/{p} {holder} 耗用勾稽不符："
                            f"凭据{base}+差额{diff}≠台账{live.get(holder, 0.0)}")
                live_unfunded = self._holder_unfunded(district, p)
                for holder in sorted(set(live_unfunded) | set(voucher.holders)):
                    base = voucher.holders.get(holder, {}).get("unfunded", 0.0)
                    diff = self._difference_sum(holder, p, "funding")
                    if abs(base + diff - live_unfunded.get(holder, 0.0)) > EPS:
                        problems.append(
                            f"{district}/{p} {holder} 未覆盖耗用勾稽不符："
                            f"凭据{base}+差额{diff}≠台账{live_unfunded.get(holder, 0.0)}")
                if abs(voucher.consumption_m3
                       - sum(s["consumed"] for s in voucher.holders.values())) > EPS:
                    problems.append(f"{district}/{p} 凭据合计与户级明细不符")
            for ev in self.deliveries.values():
                if ev.id in seen_deliveries or ev.period != p:
                    continue
                if district not in self._candidate_districts(ev):
                    continue
                seen_deliveries.add(ev.id)
                allocated = self._delivery_allocated(ev.id)
                if allocated - self._recognized(ev) > EPS:
                    problems.append(
                        f"闸次 {ev.id} 归户 {allocated} 超出认定量 {self._recognized(ev)}")
        return problems

    # ------------------------------------------------------------------
    # 内部：耗用、调剂用量与户级未覆盖
    # ------------------------------------------------------------------
    def _district_consumption(self, district: str, period: str) -> float:
        return sum(a.m3 for a in self._district_allocations(district, period))

    def _holder_consumption(self, holder: str, period: str) -> float:
        return sum(a.m3 for a in self.allocations
                   if a.holder == holder
                   and self.deliveries[a.delivery].period == period)

    def _district_allocations(self, district: str, period: str) -> list[Allocation]:
        return [a for a in self.allocations
                if self.holder_district.get(a.holder) == district
                and self.deliveries[a.delivery].period == period]

    def _district_holders(self, district: str, period: str) -> list[str]:
        holders = set()
        for holder, d in self.holder_district.items():
            if d != district:
                continue
            if any(r.holder == holder and r.period == period
                   for r in self.requests.values()):
                holders.add(holder)
            elif any(a.holder == holder
                     and self.deliveries[a.delivery].period == period
                     for a in self.allocations):
                holders.add(holder)
        return sorted(holders)

    def _open_requests(self, holder: str, period: str) -> list[Request]:
        return sorted((r for r in self.requests.values()
                       if r.holder == holder and r.period == period
                       and r.state == "reserved"),
                      key=self._request_order)

    @staticmethod
    def _request_order(req: Request):
        return (req.booked_at or date.min, req.id)

    def _open_reserved_m3(self, holder: str, period: str) -> float:
        """仍在册的预约占用：在册预约量减去已耗用部分（先冲已结清部分）。"""
        reqs = [r for r in self.requests.values()
                if r.holder == holder and r.period == period]
        open_m3 = sum(r.m3 for r in reqs if r.state == "reserved")
        settled_consumed = sum(r.settled_consumed_m3 for r in reqs
                               if r.state != "reserved")
        fill_open = max(0.0, self._holder_consumption(holder, period) - settled_consumed)
        return max(0.0, open_m3 - fill_open)

    def _adjustment_usage(self, period: str) -> dict[str, float]:
        """调剂用量：耗用先吃自有配额，越出部分按闸次开始日找当时有效的调剂，
        先到期先用，受上限约束。调剂到期不回溯当时合法耗用。"""
        usage = {a.id: 0.0 for a in self.adjustments}
        districts = {d for (d, p) in self.quotas if p == period}
        for district in districts:
            own = self.quota(district, period)
            by_day: dict[date, float] = {}
            for alloc in self._district_allocations(district, period):
                day = self.deliveries[alloc.delivery].date
                by_day[day] = by_day.get(day, 0.0) + alloc.m3
            cumulative = 0.0
            for day in sorted(by_day):
                prev = cumulative
                cumulative += by_day[day]
                excess = cumulative - max(own, prev)
                if excess <= EPS:
                    continue
                for adj in sorted(self.adjustments,
                                  key=lambda a: (a.valid_from, a.id)):
                    if adj.to_district != district or not adj.active_on(day):
                        continue
                    room = adj.limit_m3 - usage[adj.id]
                    if room <= EPS:
                        continue
                    take = min(room, excess)
                    usage[adj.id] += take
                    excess -= take
                    if excess <= EPS:
                        break
        return usage

    def _holder_unfunded(self, district: str, period: str) -> dict[str, float]:
        """户级未覆盖耗用：配额与当时有效调剂都覆盖不了的部分，按户在案。"""
        usage = self._adjustment_usage(period)
        pool = self.quota(district, period) + sum(
            usage.get(a.id, 0.0) for a in self.adjustments if a.to_district == district)
        unfunded: dict[str, float] = {}
        allocations = sorted(
            self._district_allocations(district, period),
            key=lambda a: (self.deliveries[a.delivery].date, a.received_at, a.holder))
        for alloc in allocations:
            funded = min(alloc.m3, pool)
            pool -= funded
            rest = alloc.m3 - funded
            if abs(rest) > EPS:
                unfunded[alloc.holder] = unfunded.get(alloc.holder, 0.0) + rest
        return unfunded

    # ------------------------------------------------------------------
    # 内部：差额与报表行
    # ------------------------------------------------------------------
    def _post_difference(self, holder: str, district: str, period: str,
                         m3: float, reason: str, source: str,
                         at: date) -> DifferenceEntry:
        entry = DifferenceEntry(holder=holder, district=district, period=period,
                                m3=m3, reason=reason, source=source, created_at=at)
        self.differences.append(entry)
        return entry

    def _difference_sum(self, holder: str, period: str, dimension: str) -> float:
        return sum(e.m3 for e in self.differences
                   if e.holder == holder and e.period == period
                   and _DIFF_DIMENSION.get(e.reason) == dimension)

    def _consumption_listing(self, district: str, period: str) -> list[dict]:
        by_holder: dict[str, dict] = {}
        for alloc in self._district_allocations(district, period):
            row = by_holder.setdefault(alloc.holder, {"holder": alloc.holder,
                                                      "m3": 0.0, "by_delivery": {}})
            row["m3"] += alloc.m3
            ev = alloc.delivery
            row["by_delivery"][ev] = row["by_delivery"].get(ev, 0.0) + alloc.m3
        return [by_holder[h] for h in sorted(by_holder)]

    def _pending_listing(self, district: str, period: str) -> list[dict]:
        out = []
        for holder in self._district_holders(district, period):
            settled_consumed = sum(
                r.settled_consumed_m3 for r in self.requests.values()
                if r.holder == holder and r.period == period and r.state != "reserved")
            fill = max(0.0, self._holder_consumption(holder, period) - settled_consumed)
            for req in self._open_requests(holder, period):
                take = min(req.m3, fill)
                fill -= take
                out.append({"id": req.id, "holder": holder, "m3": req.m3,
                            "open_m3": req.m3 - take})
        return out

    def _delivery_anomalies(self, district: str, period: str) -> list[dict]:
        out = []
        for ev in self.deliveries.values():
            if ev.period != period or district not in self._candidate_districts(ev):
                continue
            allocated = self._delivery_allocated(ev.id)
            over = allocated - self._recognized(ev)
            if ev.reconciliation == "discrepancy" or over > EPS:
                out.append({
                    "delivery": ev.id, "reconciliation": ev.reconciliation,
                    "recognized_m3": self._recognized(ev),
                    "allocated_m3": allocated,
                    "over_allocated_m3": max(0.0, over),
                })
        return out

    @staticmethod
    def _adjustment_row(adj: Adjustment, usage: dict[str, float]) -> dict:
        used = usage.get(adj.id, 0.0)
        return {
            "id": adj.id, "from": adj.from_district, "to": adj.to_district,
            "limit_m3": adj.limit_m3, "used_m3": used,
            "remaining_m3": max(0.0, adj.limit_m3 - used),
            "state": adj.state,
            "valid_from": adj.valid_from.isoformat(),
            "valid_to": adj.valid_to.isoformat(),
        }

    @staticmethod
    def _request_row(req: Request) -> dict:
        return {"id": req.id, "holder": req.holder, "m3": req.m3,
                "state": req.state, "released_m3": req.released_m3,
                "settled_consumed_m3": req.settled_consumed_m3}

    @staticmethod
    def _voucher_row(voucher: Voucher) -> dict:
        return {"serial": voucher.serial, "issued_at": voucher.issued_at.isoformat(),
                "quota_m3": voucher.quota_m3,
                "transfer_in_used_m3": voucher.transfer_in_used_m3,
                "transfer_out_used_m3": voucher.transfer_out_used_m3,
                "consumption_m3": voucher.consumption_m3,
                "unfunded_m3": voucher.unfunded_m3,
                "overuse_m3": voucher.overuse_m3,
                "remaining_m3": voucher.remaining_m3,
                "holders": voucher.holders}

    @staticmethod
    def _difference_row(entry: DifferenceEntry) -> dict:
        return {"holder": entry.holder, "m3": entry.m3, "reason": entry.reason,
                "source": entry.source, "created_at": entry.created_at.isoformat()}

    def _touch(self, day: date) -> None:
        if self.clock is None or day > self.clock:
            self.clock = day
