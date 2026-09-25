FROM python:3.12-slim

WORKDIR /srv
COPY model/requirements.txt /srv/requirements.txt
RUN pip install --no-cache-dir -r /srv/requirements.txt
COPY backend/app/__init__.py /srv/backend/app/__init__.py
COPY backend/app/models.py /srv/backend/app/models.py
COPY model /srv/model
ENV MODEL_PATH=/srv/model/catboost_all.cbm PYTHONUNBUFFERED=1
EXPOSE 8081
CMD ["uvicorn", "model.app:app", "--host", "0.0.0.0", "--port", "8081", "--workers", "1"]
