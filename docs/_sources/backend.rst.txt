Backend: Python-модули
======================

HTTP API и настройка приложения
-------------------------------

``backend.app.main`` создаёт FastAPI-приложение. В нём находятся маршруты
телеметрии, расписания, прогнозов, воспроизведения истории и WebSocket.

.. automodule:: backend.app.main

Схемы данных
------------

Pydantic-модели задают форматы внутренних объектов и ответов API.

.. automodule:: backend.app.models

Приём и обработка телеметрии
----------------------------

``backend.app.ndtp`` проверяет и декодирует бинарные NDTP-кадры. Сервис
накапливает состояние транспорта, рассчитывает прогнозы и рассылает срезы.

.. automodule:: backend.app.ndtp

.. automodule:: backend.app.service

Признаки, хранилище и история
-----------------------------

``backend.app.features`` строит вход ML по телеметрии. ``store`` хранит
регистрации, расписание и фактические прибытия; ``history`` загружает CSV и
воспроизводит события по времени.

.. automodule:: backend.app.features

.. automodule:: backend.app.store

.. automodule:: backend.app.history
