FROM python:3.12-slim

WORKDIR /srv
COPY backend/requirements.txt /srv/requirements.txt
RUN pip install --no-cache-dir -r /srv/requirements.txt
COPY backend/app /srv/app
ENV DATASET_DIR=/srv/dataset NDTP_ENABLED=1 PYTHONUNBUFFERED=1
EXPOSE 8000 9201
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
