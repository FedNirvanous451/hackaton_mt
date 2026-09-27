Документация API
=================

Эта документация описывает код. Форматы HTTP-запросов и ответов публикуются
самими FastAPI-сервисами:

* `Swagger backend <http://127.0.0.1:8000/docs>`_ — телеметрия, расписание,
  исторический поток, карта, прогнозы и метрики.
* `Swagger ML <http://127.0.0.1:8081/docs>`_ — ``/health`` и ``/predict``.

Сохранённые схемы находятся в корне репозитория по путям
``backend/openapi.json`` и ``ml_core/openapi.json``. После изменения маршрутов
их можно обновить командой ``python scripts/export_openapi.py``.

WebSocket ``/api/v1/ws`` описан в разделе :doc:`architecture` и в
``backend/README.md``. Стандарт OpenAPI документирует HTTP-маршруты, поэтому
WebSocket не отображается в Swagger.
