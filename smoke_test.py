import joblib
import pandas as pd

from features import FEATURES

model = joblib.load("models/extratrees_all_42.joblib")
test = pd.read_csv("data/prepared/test.csv", index_col="sample_id")
row = test.iloc[[0]].copy()
dt = pd.to_datetime(row["T"])
minutes = dt.dt.hour * 60 + dt.dt.minute + dt.dt.second / 60
row["time_sin"] = __import__("numpy").sin(2 * __import__("numpy").pi * minutes / 1440)
row["time_cos"] = __import__("numpy").cos(2 * __import__("numpy").pi * minutes / 1440)
prediction = float(model.predict(row[FEATURES])[0])
print({"status": "ok", "prediction_delay_s": prediction})
