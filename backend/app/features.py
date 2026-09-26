"""Online aggregates required by ml_core's published /predict contract.

The five-minute windows and GPS rules mirror ml_core/prepare_data.py. The ML
service itself remains an independent, unmodified component.
"""

import math
from datetime import datetime, timedelta

from .models import MLPredictionRequest, ScheduledArrival, Telemetry

EARTH_RADIUS_KM = 6371.0088


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _distance_km(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    lon1, lat1, lon2, lat2 = map(math.radians, (lon1, lat1, lon2, lat2))
    a = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(min(1.0, max(0.0, a))))


def movement_metrics(history: list[Telemetry]) -> tuple[float | None, float | None]:
    """Mean segment speed from successive valid GPS fixes, and observed idle seconds."""
    ordered = sorted(history, key=lambda item: item.event_time)
    distance_km = duration_s = idle_s = 0.0
    previous = None
    for item in ordered:
        if previous is not None:
            elapsed = (item.event_time - previous.event_time).total_seconds()
            if 0 < elapsed <= 120:
                if (item.location_valid and previous.location_valid and
                    None not in (item.lon, item.lat, previous.lon, previous.lat)):
                    distance_km += _distance_km(previous.lon, previous.lat, item.lon, item.lat)
                    duration_s += elapsed
                if previous.speed_kmh is not None and item.speed_kmh is not None and max(previous.speed_kmh, item.speed_kmh) <= 1:
                    idle_s += elapsed
        previous = item
    return (distance_km * 3600 / duration_s if duration_s else None,
            idle_s if len(ordered) > 1 else None)


def build_ml_request(at: datetime, target: ScheduledArrival,
                     history: list[Telemetry], current_dev_s: float | None) -> MLPredictionRequest:
    packets = sorted((item for item in history
                      if item.tr_id == target.tr_id and item.event_time <= at),
                     key=lambda item: item.event_time)
    if not packets:
        raise ValueError("no telemetry at forecast time")
    last = packets[-1]
    recent = [item for item in packets if item.event_time >= at - timedelta(minutes=5)]

    def speeds(minutes: int) -> list[float]:
        start = at - timedelta(minutes=minutes)
        return [float(item.speed_kmh) for item in recent
                if item.event_time >= start and item.speed_kmh is not None
                and math.isfinite(item.speed_kmh)]

    speeds_5m = speeds(5)
    mean_5m = _mean(speeds_5m)
    speed_std = (math.sqrt(sum((x - mean_5m) ** 2 for x in speeds_5m) / len(speeds_5m))
                 if mean_5m is not None else None)
    valid = lambda item: (item.location_valid and item.lon is not None and item.lat is not None
                          and math.isfinite(item.lon) and math.isfinite(item.lat))
    return MLPredictionRequest(
        forecast_time=at,
        target_time_begin=target.planned_at,
        target_stop_lon=target.lon,
        target_stop_lat=target.lat,
        last_message_age_s=max(0.0, (at - last.event_time).total_seconds()),
        last_speed_kmh=float(last.speed_kmh) if last.speed_kmh is not None and math.isfinite(last.speed_kmh) else None,
        mean_speed_1m_kmh=_mean(speeds(1)),
        mean_speed_3m_kmh=_mean(speeds(3)),
        mean_speed_5m_kmh=mean_5m,
        speed_std_5m_kmh=speed_std,
        speed_change_5m_kmh=speeds_5m[-1] - speeds_5m[0] if speeds_5m else None,
        stopped_share_5m=sum(speed <= 1 for speed in speeds_5m) / len(speeds_5m) if speeds_5m else None,
        valid_messages_5m=float(sum(valid(item) for item in recent)),
        distance_to_target_km=_distance_km(last.lon, last.lat, target.lon, target.lat) if valid(last) else None,
        current_dev_s=current_dev_s,
    )
