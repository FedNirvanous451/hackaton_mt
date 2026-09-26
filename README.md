# Transport Delay ML Core

Отдельное ML-ядро для прогноза задержки транспорта за 10–15 минут до планового прибытия.

## Состав

- `train.py` — воспроизводимое обучение ExtraTrees и проверка MAE на размеченном test.
- `prepare_data.py` — подготовка признаков из исходной телеметрии и расписания.
- `features.py` — единый список и подготовка признаков для обучения.
- `patterns.py` — выявление наблюдаемых паттернов перед возможной задержкой.
- `models/extratrees_all_42.joblib` — готовая модель с test MAE 42.82 сек.
- `api/main.py` — FastAPI для инференса.
- `Dockerfile` — контейнер ML API.
- `data/prepared` — минимальные подготовленные таблицы для воспроизведения обучения.
- `notebooks/data_preparation.ipynb` — пояснение процесса подготовки данных.
- `notebooks/model_research.ipynb` — исследование и сравнение моделей.
- `PROJECT_REPORT.md` — краткая история экспериментов и выбора модели.

## Подготовка данных

```bash
python prepare_data.py --dataset-dir /path/to/dataset --output-dir data/prepared
```

Скрипт строит агрегаты скорости за 1/3/5 минут, признаки остановок, расстояние и текущее отклонение. Телеметрия после момента прогноза `T` не используется. Для точного воспроизведения зафиксированного результата готовые очищенные CSV уже включены в пакет.

## Обучение и проверка

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python train.py
```

Для запуска всех исследовательских ячеек ноутбука:

```bash
pip install -r requirements-research.txt
jupyter notebook notebooks/model_research.ipynb
```

Для финальной модели после выбора параметров можно добавить размеченный test:

```bash
python train.py --final --output models/extratrees_final.joblib
```

## Запуск API без Docker

```bash
uvicorn api.main:app --host 0.0.0.0 --port 8000
```

Swagger: `http://localhost:8000/docs`.

## Запуск в Docker

```bash
docker build -t transport-delay-ml .
docker run --rm -p 8000:8000 transport-delay-ml
```

Проверка:

```bash
curl http://localhost:8000/health
curl -X POST http://localhost:8000/predict -H "Content-Type: application/json" --data @api/example_request.json
```

API проверяет, что плановое прибытие находится в окне `(T+10 минут, T+15 минут]`. Фактическое время прибытия и целевая задержка в инференсе не используются.

Ответ `/predict` содержит прогноз и наблюдаемые паттерны: резкое снижение скорости, длительную остановку, устаревшую телеметрию и недостаточную скорость для прибытия по расписанию. Это факторы прогноза, а не доказанные причины задержки.
