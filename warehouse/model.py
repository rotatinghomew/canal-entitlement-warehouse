"""灌区转供配额仓的数据模型。

时间口径：一律按灌区本地墙钟处理。解析带时区的 ISO 时间时丢弃偏移，
只保留本地日期时间；日期一律为本地日期。结算期口径为自然月（如 2026-11）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime


def parse_dt(raw: str) -> datetime:
    """解析 ISO 时间戳为本地墙钟（丢弃时区偏移）。"""
    dt = datetime.fromisoformat(raw)
    return dt.replace(tzinfo=None) if dt.tzinfo is not None else dt


def parse_date(raw: str) -> date:
    return date.fromisoformat(raw)


def period_of(day: date) -> str:
    """结算期口径：自然月，如 2026-11。"""
    return f"{day.year:04d}-{day.month:02d}"


@dataclass
class MeterRevision:
    """量表口径版本。

    effective_at 为空表示建账前已在用的原始口径，以首条读数为底数；
    非空表示一次换表，initial_value 为新表底数（通常为 0）。
    """
    meter: str
    revision: int
    unit: str = "m3"
    direction: str = "cumulative"
    effective_at: datetime | None = None
    initial_value: float = 0.0


@dataclass
class Reading:
    id: str
    meter: str
    revision: int
    at: datetime
    value: float


@dataclass
class DistrictQuota:
    district: str
    period: str
    quota_m3: float


@dataclass
class Adjustment:
    """临时调剂：from_district 让渡额度给 to_district，limit_m3 为上限。

    撤销时 retroactive 表示是否追溯：追溯则自始至终无效，
    不追溯则自 revoked_at 起无效，此前已发生的合法耗用不动。
    """
    id: str
    from_district: str
    to_district: str
    limit_m3: float
    valid_from: date
    valid_to: date
    state: str = "active"  # active | revoked
    revoked_at: date | None = None
    retroactive: bool = False

    def active_on(self, day: date) -> bool:
        if self.state == "revoked":
            if self.retroactive:
                return False
            if self.revoked_at is not None and day >= self.revoked_at:
                return False
        return self.valid_from <= day <= self.valid_to


@dataclass
class Request:
    """取水请求 / 预约。plots 为服务地块，window_from/to 为取水时段。"""
    id: str
    holder: str
    district: str
    m3: float
    period: str
    plots: list[str] = field(default_factory=list)
    window_from: date | None = None
    window_to: date | None = None
    booked_at: date | None = None
    state: str = "reserved"  # reserved | fulfilled | released
    released_m3: float = 0.0
    settled_consumed_m3: float = 0.0


@dataclass
class GateEvent:
    """闸次单。date 为闸次开始日（本地），配额与调剂有效性按此日判定。"""
    id: str
    meter: str
    date: date
    period: str
    reported_total_m3: float
    districts: list[str] | None = None
    started_at: datetime | None = None
    derived_total_m3: float | None = None
    reconciliation: str = "unverified"  # confirmed | discrepancy | unverified
    status: str = "unallocated"  # unallocated | partially-allocated | allocated


@dataclass
class Allocation:
    """归户分量。kind=correction 时允许负数（冲正误归的分量）。"""
    delivery: str
    holder: str
    m3: float
    received_at: date
    kind: str = "split-report"  # split-report | correction


@dataclass
class DifferenceEntry:
    """权利户差额：期结签出凭据后的追溯调整，按户记账，不改动凭据。"""
    holder: str
    district: str
    period: str
    m3: float
    reason: str  # late-allocation | correction | retroactive-revocation
    source: str
    created_at: date


@dataclass
class Voucher:
    """期结签出的配额凭据。签出后不再改动；后续更正走权利户差额。"""
    district: str
    period: str
    serial: str
    issued_at: date
    quota_m3: float
    transfer_in_used_m3: float
    transfer_out_used_m3: float
    consumption_m3: float
    unfunded_m3: float
    overuse_m3: float
    remaining_m3: float
    holders: dict[str, dict] = field(default_factory=dict)
