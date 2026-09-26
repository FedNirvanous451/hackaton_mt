import argparse
import re
from pathlib import Path

import numpy as np
import pandas as pd

SYNTHETIC_FROM = 9_000_000

def parse_point(value):
    match = re.search(r"POINT\s*\(\s*([-+0-9.eE]+)\s+([-+0-9.eE]+)\s*\)", str(value))
    return (float(match.group(1)), float(match.group(2))) if match else (np.nan, np.nan)

def haversine_km(lon1, lat1, lon2, lat2):
    lon1, lat1, lon2, lat2 = map(np.radians, [lon1, lat1, lon2, lat2])
    dlon, dlat = lon2 - lon1, lat2 - lat1
    a = np.sin(dlat / 2) ** 2 + np.cos(lat1) * np.cos(lat2) * np.sin(dlon / 2) ** 2
    return 6371.0088 * 2 * np.arcsin(np.sqrt(a))

def main():
    parser = argparse.ArgumentParser(description="Подготовка признаков без временной утечки")
    parser.add_argument("--dataset-dir", type=Path, required=True, help="Папка исходного dataset")
    parser.add_argument("--output-dir", type=Path, default=Path("data/prepared"))
    args = parser.parse_args()
    traffic = pd.read_csv(args.dataset_dir / "train" / "traffic.csv", low_memory=False)
    schedule = pd.read_csv(args.dataset_dir / "train" / "schedule.csv", low_memory=False)
    test_labels = pd.read_csv(args.dataset_dir / "labels" / "labels_test.csv", low_memory=False)
    traffic["event_time"] = pd.to_datetime(traffic["event_time"], errors="coerce")
    schedule["time_begin"] = pd.to_datetime(schedule["time_begin"], errors="coerce")
    schedule["time_fact_begin"] = pd.to_datetime(schedule["time_fact_begin"], errors="coerce")
    test_labels["target_time_begin"] = pd.to_datetime(test_labels["target_time_begin"], errors="coerce")
    for data in [traffic, schedule, test_labels]:
        data["tr_id"] = pd.to_numeric(data["tr_id"], errors="coerce")
    schedule["tt_action_item_id"] = pd.to_numeric(schedule["tt_action_item_id"], errors="coerce")
    test_labels["target_stop_id"] = pd.to_numeric(test_labels["target_stop_id"], errors="coerce")
    traffic = traffic.dropna(subset=["tr_id", "event_time"]).copy()
    schedule = schedule.dropna(subset=["tr_id", "tt_action_item_id", "time_begin", "time_fact_begin"]).copy()
    traffic["tr_id"] = traffic["tr_id"].astype(int)
    schedule["tr_id"] = schedule["tr_id"].astype(int)
    schedule["tt_action_item_id"] = schedule["tt_action_item_id"].astype(int)
    test_labels = test_labels.dropna(subset=["tr_id", "target_stop_id", "target_time_begin"]).copy()
    test_labels[["tr_id", "target_stop_id"]] = test_labels[["tr_id", "target_stop_id"]].astype(int)
    schedule[["target_stop_lon", "target_stop_lat"]] = pd.DataFrame(schedule["geom"].map(parse_point).tolist(), index=schedule.index)
    traffic = traffic.sort_values(["tr_id", "event_time"])
    traffic_by_vehicle = {tr_id: group.reset_index(drop=True) for tr_id, group in traffic.groupby("tr_id")}
    history_by_vehicle = {}
    for tr_id, group in schedule.groupby("tr_id", sort=False):
        history_by_vehicle[tr_id] = (group["time_begin"].astype("int64").to_numpy(), group["time_fact_begin"].astype("int64").to_numpy(), (group["time_fact_begin"] - group["time_begin"]).dt.total_seconds().to_numpy())
    forbidden = set(map(tuple, test_labels[["tr_id", "target_stop_id", "target_time_begin"]].to_numpy()))
    rows = []
    for point in schedule.itertuples(index=False):
        tr_id, target_stop_id = int(point.tr_id), int(point.tt_action_item_id)
        target_time, forecast_time = pd.Timestamp(point.time_begin), pd.Timestamp(point.time_begin) - pd.Timedelta(minutes=12)
        if (tr_id, target_stop_id, target_time) in forbidden or tr_id not in traffic_by_vehicle:
            continue
        data = traffic_by_vehicle[tr_id]
        times = data["event_time"].astype("int64").to_numpy()
        end = np.searchsorted(times, forecast_time.value, side="right")
        if end == 0:
            continue
        lon = pd.to_numeric(data["lon"], errors="coerce").to_numpy(float)
        lat = pd.to_numeric(data["lat"], errors="coerce").to_numpy(float)
        speed = pd.to_numeric(data["speed"], errors="coerce").to_numpy(float)
        valid = data["location_valid"].fillna(False).astype(bool).to_numpy() & np.isfinite(lon) & np.isfinite(lat)
        last = end - 1
        plan_times, fact_times, deviations = history_by_vehicle[tr_id]
        passed = np.flatnonzero(fact_times <= forecast_time.value)
        if len(passed):
            latest_plan = plan_times[passed].max()
            current_dev = deviations[passed[plan_times[passed] == latest_plan][0]]
        else:
            current_dev = np.nan
        row = {"sample_id": f"{tr_id}_{int(forecast_time.timestamp())}_{target_stop_id}", "tr_id": tr_id, "T": forecast_time, "target_stop_id": target_stop_id, "target_time_begin": target_time,
               "target_stop_lon": point.target_stop_lon, "target_stop_lat": point.target_stop_lat, "time_to_target_s": 720.0,
               "last_message_age_s": max(0, (forecast_time.value - times[last]) / 1e9), "last_speed_kmh": speed[last] if np.isfinite(speed[last]) else np.nan,
               "mean_speed_1m_kmh": np.nan, "mean_speed_3m_kmh": np.nan, "mean_speed_5m_kmh": np.nan, "speed_std_5m_kmh": np.nan,
               "speed_change_5m_kmh": np.nan, "stopped_share_5m": np.nan, "valid_messages_5m": 0, "distance_to_target_km": np.nan,
               "current_dev_s": current_dev, "target_delay_s": (pd.Timestamp(point.time_fact_begin) - target_time).total_seconds()}
        for minutes, column in [(1, "mean_speed_1m_kmh"), (3, "mean_speed_3m_kmh"), (5, "mean_speed_5m_kmh")]:
            start = np.searchsorted(times, forecast_time.value - minutes * 60 * 1_000_000_000, side="left")
            values = speed[start:end][np.isfinite(speed[start:end])]
            if len(values): row[column] = float(values.mean())
        start = np.searchsorted(times, forecast_time.value - 5 * 60 * 1_000_000_000, side="left")
        values = speed[start:end][np.isfinite(speed[start:end])]
        if len(values):
            row["speed_std_5m_kmh"] = float(values.std())
            row["speed_change_5m_kmh"] = float(values[-1] - values[0])
            row["stopped_share_5m"] = float((values <= 1).mean())
        row["valid_messages_5m"] = int(valid[start:end].sum())
        if valid[last] and np.isfinite(point.target_stop_lon) and np.isfinite(point.target_stop_lat):
            row["distance_to_target_km"] = float(haversine_km(lon[last], lat[last], point.target_stop_lon, point.target_stop_lat))
        rows.append(row)
    features = pd.DataFrame(rows).sort_values(["T", "tr_id", "target_stop_id"]).reset_index(drop=True)
    times = features["T"].drop_duplicates().sort_values().reset_index(drop=True)
    cutoff = times.iloc[int(len(times) * 0.75)]
    train_all, val_all = features[features["T"] < cutoff].copy(), features[features["T"] >= cutoff].copy()
    train_real, val_real = train_all[train_all["tr_id"] < SYNTHETIC_FROM].copy(), val_all[val_all["tr_id"] < SYNTHETIC_FROM].copy()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    for name, data in [("train_all", train_all), ("train_real", train_real), ("val_all", val_all), ("val_real", val_real)]:
        data.to_csv(args.output_dir / f"{name}.csv", index=False)
    print({"cutoff": str(cutoff), "train_all": len(train_all), "train_real": len(train_real), "val_all": len(val_all), "val_real": len(val_real)})

if __name__ == "__main__":
    main()
