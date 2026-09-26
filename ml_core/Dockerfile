FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY features.py .
COPY patterns.py .
COPY api ./api
COPY models/extratrees_all_42.joblib ./models/extratrees_all_42.joblib

ENV MODEL_PATH=/app/models/extratrees_all_42.joblib
ENV MODEL_VERSION=extratrees-all-42
EXPOSE 8000

CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000"]
