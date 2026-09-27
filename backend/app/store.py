"""Live vehicle identity, schedule and observed-arrival state."""

from bisect import bisect_left, bisect_right
from collections import defaultdict
from datetime import datetime, timedelta

from .models import ArrivalDeviation, ScheduledArrival, VehicleRegistration


class LiveStore:
    def __init__(self) -> None:
        self.unit_to_tr: dict[int, int] = {}
        self.tr_to_unit: dict[int, int] = {}
        self.arrivals: dict[int, ScheduledArrival] = {}
        self.by_vehicle: dict[int, list[ScheduledArrival]] = defaultdict(list)
        self.observed_arrivals: dict[int, datetime] = {}

    def register(self, item: VehicleRegistration) -> VehicleRegistration:
        owner = self.unit_to_tr.get(item.unit_id)
        if owner is not None and owner != item.tr_id:
            raise ValueError(f"unit_id {item.unit_id} already belongs to tr_id {owner}")
        old_unit = self.tr_to_unit.get(item.tr_id)
        if old_unit is not None and old_unit != item.unit_id:
            self.unit_to_tr.pop(old_unit, None)
        self.unit_to_tr[item.unit_id] = item.tr_id
        self.tr_to_unit[item.tr_id] = item.unit_id
        return item

    def upsert_arrival(self, item: ScheduledArrival) -> ScheduledArrival:
        previous = self.arrivals.get(item.arrival_id)
        if previous and previous.tr_id != item.tr_id:
            raise ValueError(f"arrival_id {item.arrival_id} belongs to another vehicle")
        if previous:
            self.by_vehicle[item.tr_id] = [x for x in self.by_vehicle[item.tr_id]
                                           if x.arrival_id != item.arrival_id]
        self.arrivals[item.arrival_id] = item
        group = self.by_vehicle[item.tr_id]
        group.append(item)
        group.sort(key=lambda x: (x.planned_at, x.arrival_id))
        return item

    def target_at(self, tr_id: int, at: datetime) -> ScheduledArrival | None:
        group = self.by_vehicle.get(tr_id, [])
        times = [item.planned_at for item in group]
        index = bisect_right(times, at + timedelta(minutes=10))
        if index < len(group) and group[index].planned_at <= at + timedelta(minutes=15):
            return group[index]
        return None

    def next_arrival_at(self, tr_id: int, at: datetime) -> ScheduledArrival | None:
        """Next scheduled stop for display while the ML forecast window is still ahead."""
        group = self.by_vehicle.get(tr_id, [])
        index = bisect_left([item.planned_at for item in group], at)
        return group[index] if index < len(group) else None

    def stops_between(self, start: datetime, end: datetime,
                      tr_id: int | None = None) -> list[ScheduledArrival]:
        groups = [self.by_vehicle.get(tr_id, [])] if tr_id is not None else self.by_vehicle.values()
        return [item for group in groups for item in group if start <= item.planned_at <= end]

    def observe_arrival(self, arrival_id: int, actual_at: datetime) -> ArrivalDeviation:
        item = self.arrivals.get(arrival_id)
        if item is None:
            raise KeyError(arrival_id)
        deviation = (actual_at - item.planned_at).total_seconds()
        self.observed_arrivals[arrival_id] = actual_at
        return ArrivalDeviation(arrival_id=arrival_id, tr_id=item.tr_id,
                                actual_at=actual_at, current_dev_s=deviation)

    def deviation_at(self, tr_id: int, at: datetime) -> float | None:
        passed = [(arrival.planned_at, arrival.arrival_id, actual_at)
                  for arrival in self.by_vehicle.get(tr_id, [])
                  if (actual_at := self.observed_arrivals.get(arrival.arrival_id)) is not None
                  and actual_at <= at]
        if not passed:
            return None
        planned_at, _, actual_at = max(passed)
        return (actual_at - planned_at).total_seconds()
