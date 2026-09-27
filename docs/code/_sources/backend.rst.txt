Backend (services/backend)
==========================

Приём потока NDTP (TCP 9201), map matching на нитку графика, производные признаки, батч-прогноз через
ML-сервис, риск и инциденты, ETA, what-if, экспорт GTFS-RT. Точка входа — ``app.main:app``.

Точка входа и REST API
----------------------

.. automodule:: app.main

.. automodule:: app.api_ext

Оркестрация
-----------

.. automodule:: app.engine

Приём NDTP
----------

.. automodule:: app.ndtp_server

Состояние ТС и map matching
---------------------------

.. automodule:: app.tracker

.. automodule:: app.reference

.. automodule:: app.clock

Прогноз, риск, причины
----------------------

.. automodule:: app.ml_client

.. automodule:: app.risk

ETA и what-if
-------------

.. automodule:: app.eta

.. automodule:: app.whatif

Маршруты, геометрия, интеграции
-------------------------------

.. automodule:: app.routes_registry

.. automodule:: app.routing

.. automodule:: app.geometry

.. automodule:: app.geo

.. automodule:: app.export

.. automodule:: app.regions

Конфигурация и метрики
----------------------

.. automodule:: app.config

.. automodule:: app.metrics
