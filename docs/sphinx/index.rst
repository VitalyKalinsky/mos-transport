Предиктор задержек городского транспорта — документация по коду
================================================================

Документация сгенерирована Sphinx (autodoc) из docstrings исходного кода.
Спецификация HTTP API — в Swagger/OpenAPI: см. ``docs/API.md`` в репозитории.

Система из трёх модулей:

* **Backend** (``services/backend``) — приём NDTP, сопоставление с расписанием, оркестрация прогнозов, REST/WebSocket.
* **ML-модуль** (``services/ml_service`` + ML-ядро ``notebooks/data_functions.py``) — инференс CatBoost, SHAP, дообучение, метрики.
* **Дашборд** (``dashboard``) — SPA диспетчера (nginx); кода на Python нет.

Вспомогательные компоненты: NDTP-кодек (``libs/ndtp``) и replay-фидер потока (``services/feeder``).

.. toctree::
   :maxdepth: 2
   :caption: Модули

   backend
   ndtp
   feeder
   ml
