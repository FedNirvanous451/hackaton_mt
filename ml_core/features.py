import numpy as np
import pandas as pd

TARGET = "target_delay_s"
FEATURES = ["target_stop_lon", "target_stop_lat", "time_to_target_s", "last_message_age_s", "last_speed_kmh", "mean_speed_1m_kmh", "mean_speed_3m_kmh", "mean_speed_5m_kmh", "speed_std_5m_kmh", "speed_change_5m_kmh", "stopped_share_5m", "valid_messages_5m", "distance_to_target_km", "current_dev_s", "time_sin", "time_cos"]

def prepare(data):
    data = data.copy()
    dt = pd.to_datetime(data["T"], errors="coerce")
    minutes = dt.dt.hour * 60 + dt.dt.minute + dt.dt.second / 60
    data["time_sin"] = np.sin(2 * np.pi * minutes / 1440)
    data["time_cos"] = np.cos(2 * np.pi * minutes / 1440)
    return data[FEATURES]
