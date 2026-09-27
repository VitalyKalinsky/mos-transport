ML-модуль
=========

HTTP-сервис ML (``services/ml_service/app``, в документации — пакет ``ml_service_app``) и ML-ядро
(``notebooks/data_functions.py``). ML-ядро используется как есть; сервис только вызывает его функции.
Описание модели, признаков и метрик — ``docs/ML.md``.

HTTP-сервис и адаптер
---------------------

.. automodule:: ml_service_app.main

.. automodule:: ml_service_app.adapter

Объяснение прогнозов (SHAP)
---------------------------

.. automodule:: ml_service_app.explain

Оценка модели (метрики)
-----------------------

.. automodule:: ml_service_app.evaluate

Дообучение (консоль)
--------------------

.. automodule:: ml_service_app.finetune

ML-ядро
-------

.. automodule:: data_functions
