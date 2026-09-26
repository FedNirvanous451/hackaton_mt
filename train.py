import argparse
from pathlib import Path

import joblib
import pandas as pd
from sklearn.ensemble import ExtraTreesRegressor
from sklearn.impute import SimpleImputer
from sklearn.metrics import mean_absolute_error
from sklearn.pipeline import Pipeline

from features import TARGET, prepare

def make_model():
    return Pipeline([
        ("imputer", SimpleImputer(strategy="median")),
        ("model", ExtraTreesRegressor(n_estimators=400, criterion="squared_error", max_depth=15, min_samples_split=5, min_samples_leaf=4, max_features=1.0, random_state=42, n_jobs=-1))
    ])

def main():
    parser = argparse.ArgumentParser(description="Обучение ExtraTrees для прогноза задержки")
    parser.add_argument("--data-dir", type=Path, default=Path("data/prepared"))
    parser.add_argument("--output", type=Path, default=Path("models/extratrees_all_42.joblib"))
    parser.add_argument("--final", action="store_true", help="После проверки переобучить модель с добавлением размеченного test")
    args = parser.parse_args()
    train = pd.read_csv(args.data_dir / "train_all.csv", index_col="sample_id")
    val = pd.read_csv(args.data_dir / "val_all.csv", index_col="sample_id")
    test = pd.read_csv(args.data_dir / "test.csv", index_col="sample_id")
    train_full = pd.concat([train, val])
    model = make_model()
    model.fit(prepare(train_full), train_full[TARGET])
    test_mae = mean_absolute_error(test[TARGET], model.predict(prepare(test)))
    print(f"Test MAE: {test_mae:.2f} сек.")
    if args.final:
        final_data = pd.concat([train_full, test])
        model = make_model()
        model.fit(prepare(final_data), final_data[TARGET])
        print(f"Финальная модель обучена на {len(final_data)} наблюдениях.")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(model, args.output)
    print(f"Модель сохранена: {args.output}")

if __name__ == "__main__":
    main()
