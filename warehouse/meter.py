"""表计台账：按口径版本（revision）切段认定放水量。

- 换表：每个 effective_at 非空的口径版本另起一段，以 initial_value 为底数。
- 原始口径（effective_at 为空）：建账前历史未知，以首条读数为底数，
  段内只认定其后的增量。
- 读数反转：同一段内读数不升反降，视为未登记的归零（如换表未报备），
  记录异常并按归零重启认定该读数起的累计量，不产生负水量。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from .model import MeterRevision, Reading


@dataclass
class Step:
    """一次读数确认的增量，at 为该增量被确认（读数）的时刻。"""
    at: datetime
    delta: float
    note: str | None = None


@dataclass
class Segment:
    meter: str
    revision: int
    baseline: float
    steps: list[Step] = field(default_factory=list)
    anomalies: list[str] = field(default_factory=list)

    @property
    def volume(self) -> float:
        return sum(s.delta for s in self.steps)


class MeterLedger:
    def __init__(self) -> None:
        self._revisions: dict[str, list[MeterRevision]] = {}
        self._readings: dict[str, list[Reading]] = {}

    def add_revision(self, rev: MeterRevision) -> None:
        versions = self._revisions.setdefault(rev.meter, [])
        versions.append(rev)
        versions.sort(key=lambda r: (r.effective_at is not None,
                                     r.effective_at or datetime.min,
                                     r.revision))

    def add_reading(self, reading: Reading) -> None:
        self._readings.setdefault(reading.meter, []).append(reading)

    def correct_revision(self, meter: str, revision: int, *,
                         initial_value: float | None = None,
                         effective_at: datetime | None = None) -> MeterRevision:
        """更正换表口径（底数或生效时间）。段量随下次查询重新认定。"""
        for rev in self._revisions.get(meter, []):
            if rev.revision == revision:
                if initial_value is not None:
                    rev.initial_value = initial_value
                if effective_at is not None:
                    rev.effective_at = effective_at
                return rev
        raise ValueError(f"未知量表口径版本 {meter}#{revision}")

    def revisions(self, meter: str) -> list[MeterRevision]:
        return list(self._revisions.get(meter, []))

    def readings(self, meter: str) -> list[Reading]:
        return sorted(self._readings.get(meter, []), key=lambda r: (r.at, r.id))

    def segments(self, meter: str) -> list[Segment]:
        revisions = {r.revision: r for r in self._revisions.get(meter, [])}
        unknown = {r.revision for r in self._readings.get(meter, [])} - set(revisions)
        if unknown:
            raise ValueError(f"{meter} 读数引用了未登记的口径版本: {sorted(unknown)}")
        segments: list[Segment] = []
        for rev in self.revisions(meter):
            readings = [r for r in self.readings(meter) if r.revision == rev.revision]
            if not readings:
                continue
            if rev.effective_at is None:
                baseline = readings[0].value  # 原始口径：首读数前的历史不作增量
            else:
                baseline = rev.initial_value  # 换表：以登记的新表底数为基线
            seg = Segment(meter=meter, revision=rev.revision, baseline=baseline)
            prev = baseline
            for reading in readings:
                delta = reading.value - prev
                if delta < 0:
                    note = (f"读数反转 {prev}->{reading.value}（{reading.id}），"
                            f"按归零重启认定")
                    seg.anomalies.append(note)
                    seg.steps.append(Step(at=reading.at, delta=reading.value, note=note))
                    prev = reading.value
                else:
                    seg.steps.append(Step(at=reading.at, delta=delta))
                    prev = reading.value
            segments.append(seg)
        return segments

    def delivered_between(self, meter: str, start: datetime, end: datetime) -> float | None:
        """[start, end) 内被读数确认的放水量；窗口内无任何读数则返回 None。"""
        deltas = [step.delta
                  for seg in self.segments(meter)
                  for step in seg.steps
                  if start <= step.at < end]
        return sum(deltas) if deltas else None

    def anomalies(self, meter: str | None = None) -> list[str]:
        meters = [meter] if meter else list(self._revisions)
        out: list[str] = []
        for m in meters:
            for seg in self.segments(m):
                out.extend(seg.anomalies)
        return out
