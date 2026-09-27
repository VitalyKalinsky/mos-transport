# Архитектура сервиса и API

## Оглавление

- [1. Архитектура системы](#1-архитектура-системы)
- [2. Backend: компоненты и функциональность](#2-backend-компоненты-и-функциональность)
- [3. Конвейер обработки пакета](#3-конвейер-обработки-пакета)
- [4. Дашборд: архитектура и функциональность](#4-дашборд-архитектура-и-функциональность)
- [5. Фидер потока](#5-фидер-потока)
- [6. Swagger / OpenAPI и документация по коду](#6-swagger--openapi-и-документация-по-коду)
- [7. Backend API (порт 8000)](#7-backend-api-порт-8000)
- [8. WebSocket `/ws`](#8-websocket-ws)
- [9. Приём NDTP (TCP 9201)](#9-приём-ndtp-tcp-9201)
- [10. ML-сервис API (порт 8001)](#10-ml-сервис-api-порт-8001)
- [11. Фидер API (порт 8090)](#11-фидер-api-порт-8090)
- [12. Коды ответов](#12-коды-ответов)

Связанные документы: [README](../README.md) · [Инструкция для жюри](JURY_GUIDE.md) · [ML-модуль](ML.md) ·
[Sphinx по коду](code/index.html)

---

## 1. Архитектура системы

Четыре контейнера (`docker-compose.yml`), независимые модули со своими API:

| Модуль | Контейнер / порт | Роль |
|---|---|---|
| **Backend** | `backend` :8000 (HTTP), :9201 (TCP NDTP) | Приём и парсинг NDTP, map matching на расписание, производные признаки, оркестрация прогнозов, риск и инциденты, ETA, what-if, REST и WebSocket |
| **ML-модуль** | `ml-service` :8001 | Инференс CatBoost, SHAP, метрики, дообучение ([ML.md](ML.md)) |
| **BI-дашборд** | `dashboard` :8080 (nginx) | SPA диспетчера; проксирует `/api`, `/ws`, `/feeder` |
| Источник потока | `feeder` :8090 | Реплей реальной телеметрии как NDTP-потока и сценарии отказов |
| Штатный эмулятор (опц.) | `emulator` :18080 (профиль `emulator`) | `ndtp-telemetry-emulator` организаторов → backend:9201 |

```
feeder / эмулятор / терминалы ──NDTP/TCP──▶ NDTPServer ─▶ Engine.on_frame ─▶ VehicleState (map matching, признаки)
                                                                                   │ событие «новые данные»
                                                        Engine.run (цикл ≤ 1 с) ◀──┘
                                                          ├─ MLClient ──HTTP /v1/predict (батч)──▶ ML-сервис
                                                          ├─ risk: P(late), уровень, причины
                                                          ├─ EtaEngine: ETA 8 остановок, счисление пути
                                                          ├─ инциденты, таймлайн, метрики
                                                          └─ snapshot ──▶ WebSocket /ws, REST /api ──▶ дашборд
```

**Принципы:**

- **Развязка приёма и прогноза.** Разбор пакета занимает микросекунды и только обновляет состояние ТС. Прогноз идёт
  в отдельном событийном цикле батчем по всем ТС, поэтому поток NDTP не ждёт ML.
- **Время данных.** Все расчёты идут во времени потока, а не по системным часам. Реплей с ускорением и обрывы
  обрабатываются корректно.
- **Независимость модулей.** Backend стартует и работает без ML-сервиса (baseline), дашборд — без backend
  (последнее состояние + переподключение).
- **Без внешних БД и брокеров.** Состояние в памяти процесса, минимальная задержка, предсказуемый холодный старт (~8 с).

## 2. Backend: компоненты и функциональность

Код: [services/backend/app/](../services/backend/app/). Подробные контракты — в [Sphinx](code/backend.html).

| Модуль | Функциональность |
|---|---|
| [ndtp_server.py](../services/backend/app/ndtp_server.py) | Асинхронный TCP-сервер NDTP: соединение на терминал, handshake и `NPH_RESULT`, пересинхронизация на мусоре, таймаут простоя 120 с, метрики соединений |
| [libs/ndtp/codec.py](../libs/ndtp/codec.py) | Кодек NDTP: NPL+NPH, CRC-16/Modbus, ячейки Nav00, IntSensor02, Usi08, Can10, Lls15, Termo16 и ячейки §6.7; потоковый декодер |
| [tracker.py](../services/backend/app/tracker.py) | Состояние ТС и **map matching**: геозона 45 м с проверкой отрезка трека (момент ближайшего подхода), сопоставление по порядку расписания в окне [−8, +12] мин, `cur_dev_s`, средняя и плановая скорость сегмента, простой, тренд отклонения, серия сбоев GPS |
| [reference.py](../services/backend/app/reference.py) | Эталонное расписание, остановки, восстановление маршрутной сети (union-find по общим остановкам), геометрия перегонов по дорогам |
| [clock.py](../services/backend/app/clock.py) | Часы времени данных: синхронизация по пакетам, оценка темпа реплея, ход при обрыве |
| [engine.py](../services/backend/app/engine.py) | Оркестрация: событийный цикл прогноза, батч в ML, инциденты с гистерезисом, KPI, снимок для дашборда, онлайн-оценка точности, таймлайн, pub/sub WebSocket |
| [ml_client.py](../services/backend/app/ml_client.py) | Клиент ML с keep-alive, таймаутом 1.5 с и circuit breaker (3 ошибки → пауза 5 с) |
| [risk.py](../services/backend/app/risk.py) | P(late) по Лапласу, уровни зелёный / жёлтый / красный, 12 правил диагностики причины с рекомендациями |
| [eta.py](../services/backend/app/eta.py) | ETA на 8 остановок: проекция на геометрию, темп ТС, отстои, пробки, автобусные полосы, затухание отклонения; счисление пути без связи |
| [whatif.py](../services/backend/app/whatif.py) | 9 сценариев what-if и их комбинации, сравнение «до/после», каскад на следующие рейсы, рекомендация |
| [routes_registry.py](../services/backend/app/routes_registry.py) | Реестр реальных маршрутов: импорт OSM (Overpass, 3 зеркала), GTFS, ручной; автопривязка ТС |
| [routing.py](../services/backend/app/routing.py) | Клиент OSRM: маршрут по дорогам, map matching треков, лимит частоты |
| [export.py](../services/backend/app/export.py) | GTFS-Realtime (VehiclePositions, TripUpdates с ML, Alerts), статический GTFS, GeoJSON |
| [main.py](../services/backend/app/main.py), [api_ext.py](../services/backend/app/api_ext.py) | REST API, WebSocket, метрики Prometheus, жизненный цикл сервиса |

## 3. Конвейер обработки пакета

1. **Приём.** TCP-фрагменты → `StreamDecoder` → кадр → проверка CRC → `NPH_RESULT` терминалу.
2. **Состояние ТС.** `peerAddress` → ТС (`tr_id`); Nav00 → координаты, скорость, курс; обновление часов данных.
3. **Map matching.** Проверка прохождения геозон ближайших по графику остановок → факт прибытия и отклонение.
4. **Цикл прогноза** (не позже 150 мс после пакета, не реже 1 раза в секунду):
   - выбор целевой остановки в окне (T+10, T+15] мин;
   - текущее отклонение;
   - последние ~20 пакетов → батч в ML;
   - прогноз, SHAP, P(late), уровень риска, причина, инцидент;
   - ETA, риск участков сети, снимок, рассылка по WebSocket.
5. **Онлайн-оценка.** При фактическом прибытии на целевую остановку прогнозы, сделанные за 10–15 мин до него,
   сравниваются с фактом → KPI «Точность онлайн».

## 4. Дашборд: архитектура и функциональность

Код: [dashboard/](../dashboard/). Статический SPA (vanilla JS, Leaflet) за nginx; nginx проксирует `/api/*` и `/ws`
на backend, `/feeder/*` — на фидер ([nginx.conf](../dashboard/nginx.conf)). Данные приходят по WebSocket раз
в секунду; при разрыве дашборд переключается на опрос REST и переподключается.

| Модуль | Функциональность |
|---|---|
| [app.js](../dashboard/app.js) | Карта (ТС, сеть, риск участков, расчётное положение), KPI, инциденты, карточка ТС с ETA и SHAP, поиск и отслеживание, журнал, производительность, темы, подложки 2ГИС / Яндекс / OSM |
| [whatif.js](../dashboard/whatif.js) | Конструктор сценариев, комбинации, сравнение «до/после», отображение на карте, применение к живому прогнозу |
| [timeline.js](../dashboard/timeline.js) | Таймлайн: 6 ч истории, прогноз положения на 30 мин, просмотр карты на выбранный момент |
| [routes.js](../dashboard/routes.js) | Реестр реальных маршрутов: импорт OSM / GTFS / вручную, привязка ТС |
| [integrations/](../dashboard/integrations/) | Примеры подключения фидов к JS API Яндекс Карт и 2ГИС |

Интерфейс рассчитан на понимание за ~5 секунд: цвет риска на ТС, участках и маршрутах дублируется формой маркера,
самые серьёзные непринятые инциденты — вверху списка.

## 5. Фидер потока

Код: [services/feeder/feeder.py](../services/feeder/feeder.py). Воспроизводит `dataset/validate/traffic.csv` как живой
NDTP-поток и ведёт себя как штатный эмулятор: соединение на терминал, handshake, пауза 200 мс, realtime-пакеты,
reconnect с экспоненциальным backoff. Строки выдаются в порядке поступления на сервер (паритет признаков с обучением).

- **Управление:** скорость ×0.1…×200, пауза, перемотка, зацикливание.
- **Сценарии отказов:** обрыв на N секунд или до ручного восстановления, мусорные байты в канал.
- **Внештатные ситуации в потоке:** поломка (ТС стоит, шлёт «стоячие» пакеты), ДТП или засор (ТС едет медленнее).
  Их включает what-if backend при «Применить к живому прогнозу».

## 6. Swagger / OpenAPI и документация по коду

| Сервис | Swagger UI (система запущена) | Статическая спецификация (без запуска) |
|---|---|---|
| Backend | http://localhost:8000/docs · ReDoc `/redoc` · `/openapi.json` | [api/backend.html](api/backend.html) · [openapi-backend.json](api/openapi-backend.json) |
| ML-сервис | http://localhost:8001/docs | [api/ml-service.html](api/ml-service.html) · [openapi-ml-service.json](api/openapi-ml-service.json) |
| Фидер | http://localhost:8090/docs | [api/feeder.html](api/feeder.html) · [openapi-feeder.json](api/openapi-feeder.json) |

- Описания эндпоинтов в Swagger сгенерированы из docstrings кода.
- **Документация по коду (Sphinx):** [code/index.html](code/index.html) — модули Backend, NDTP, фидера, ML-сервиса и ML-ядра;
  контракты, параметры, возвращаемые значения, исключения, ссылки на исходники.
- **Пересборка из кода:** `docker run --rm -v "$PWD:/w" --entrypoint sh mos-transport/ml-service:1.0 /w/docs/build_docs.sh`
  (PowerShell: `${PWD}`). Скрипт [build_docs.sh](build_docs.sh) собирает Sphinx ([sphinx/](sphinx/)) и выгружает OpenAPI ([export_openapi.py](export_openapi.py)).

## 7. Backend API (порт 8000)

Время в ответах — МСК. Идентификатор ТС — `unit_id` терминала или `tr_id`.

| Группа | Метод | Путь | Назначение |
|---|---|---|---|
| service | GET | `/health` | Healthcheck, режим online/degraded |
| service | GET | `/api/status` | Режим работы и причины деградации |
| service | GET | `/api/metrics` | Метрики приёма, цикла прогноза, задержек p50/p95/p99, онлайн-точности |
| service | GET | `/metrics` | Метрики в формате Prometheus |
| ndtp | GET | `/api/connections` | Активные TCP-соединения терминалов |
| ndtp | POST | `/api/ndtp/parse` | Разбор NDTP-кадров из hex |
| dispatcher | GET | `/api/state` | Полный снимок: KPI, ТС, инциденты, риск маршрутов и участков, журнал |
| dispatcher | GET | `/api/vehicles` | Все ТС: положение, риск, прогноз T+10…15, P(late), причина |
| dispatcher | GET | `/api/vehicles/{vehicle_id}` | Карточка ТС: прогноз, SHAP, признаки, ETA, трейл, прибытия |
| dispatcher | GET | `/api/vehicles/{vehicle_id}/eta` | ETA на ближайшие остановки |
| dispatcher | GET | `/api/incidents` | Инциденты: `open` и `resolved` |
| dispatcher | POST | `/api/incidents/{incident_id}/ack` | Принять инцидент в работу |
| dispatcher | GET | `/api/search?q=` | Поиск ТС и маршрутов |
| dispatcher | GET | `/api/network` | Маршрутная сеть: остановки, сегменты по дорогам, маршруты |
| dispatcher | GET | `/api/model` | Метаданные модели (прокси ML) |
| map | GET | `/api/region` | Регион карты: границы, центр, провайдеры тайлов |
| map | GET / PUT | `/api/traffic` | Загруженность перегонов; внешний индекс пробок и автобусные полосы |
| timeline | GET | `/api/timeline` | История состояний и прогноз положения ТС |
| what-if | GET | `/api/whatif/types` | Сценарии и параметры |
| what-if | POST | `/api/whatif/simulate` | Моделирование «до/после» |
| what-if | POST | `/api/whatif/apply` | Применение к живому прогнозу и потоку |
| what-if | DELETE | `/api/whatif/live` | Снять применённые сценарии |
| routes | GET / POST | `/api/routes` | Реестр маршрутов; создание по остановкам |
| routes | GET / DELETE | `/api/routes/{route_id}` | Маршрут с геометрией; удаление |
| routes | PUT | `/api/routes/{route_id}/vehicles` | Привязка ТС |
| routes | POST | `/api/routes/import/osm` | Импорт из OpenStreetMap |
| routes | POST | `/api/routes/import/gtfs` | Импорт из GTFS (zip) |
| export | GET | `/api/export/gtfs-rt/vehicle-positions` | GTFS-RT VehiclePositions (`?format=json`) |
| export | GET | `/api/export/gtfs-rt/trip-updates` | GTFS-RT TripUpdates (ETA + ML) |
| export | GET | `/api/export/gtfs-rt/alerts` | GTFS-RT Alerts |
| export | GET | `/api/export/gtfs-static.zip` | Статический GTFS |
| export | GET | `/api/export/vehicles.geojson` | GeoJSON ТС |

```bash
curl http://localhost:8000/api/status
curl http://localhost:8000/api/incidents
curl -X POST http://localhost:8000/api/whatif/simulate -H "Content-Type: application/json" \
  -d '{"scenarios":[{"type":"reserve_bus","params":{"unit_id":1076894}}]}'
```

## 8. WebSocket `/ws`

`ws://localhost:8000/ws` (через дашборд — `ws://localhost:8080/ws`). Первое сообщение — текущий снимок, далее —
после каждого цикла прогноза (≈ 1 раз в секунду). Формат совпадает с `GET /api/state`: `status`, `kpi`, `vehicles`,
`incidents`, `resolved`, `routes`, `segments`, `events`, `perf`, `live_scenarios`. Очередь на клиента — 2 снимка
(всегда свежие данные, постоянная память).

## 9. Приём NDTP (TCP 9201)

- Кадр `NPL(15) + NPH(10) + тело`, little-endian, CRC-16/Modbus по NPH + телу.
- Handshake `NPH_SGC_CONN_REQUEST` и пакеты с флагом request подтверждаются `NPH_RESULT`.
- Навигация — `G6CellNav00`: знаки широты и долготы из битов 5/6, валидность из бита 7; `peerAddress` → ТС.
- Поток собирается из произвольных TCP-фрагментов; битые кадры пропускаются с пересинхронизацией по `0x7E7E`.
- Реализовано по `dataset/docs/Emulator-and-Telematic-Packets-Specification.md`, проверено на захватах живого
  эмулятора (`libs/ndtp/tests/fixtures`) и нагрузкой 1000 терминалов × 5 пакетов/с.

## 10. ML-сервис API (порт 8001)

| Метод | Путь | Назначение |
|---|---|---|
| GET | `/health` | `ready: true` после загрузки модели |
| GET | `/v1/model` | Модель: путь, деревья, признаки, важности, валидационная MAE |
| POST | `/v1/predict` | Батч-прогноз: `points` + `telemetry` → `predicted_delay_s`, `features`, `explanation` (SHAP), тайминги |
| POST | `/v1/train` | Переобучение по HTTP (включается `ML_ENABLE_TRAIN=true`; штатно — консольное дообучение) |
| GET | `/v1/stats` | Запросы и задержки p50/p95/p99 |
| GET | `/metrics` | Prometheus |

## 11. Фидер API (порт 8090)

| Метод | Путь | Назначение |
|---|---|---|
| GET | `/status` | Время данных, прогресс, соединения, отправлено / потеряно |
| POST | `/pause`, `/resume` | Пауза и продолжение |
| POST | `/speed/{value}` | Ускорение 0.1…200 |
| POST | `/seek?t=YYYY-MM-DD HH:MM:SS` | Перемотка (UTC) |
| POST | `/outage/{seconds}` | Обрыв связи на N секунд |
| POST | `/outage/start`, `/outage/stop` | Обрыв до ручного восстановления |
| POST | `/chaos/garbage` | Мусорные байты во все соединения |
| POST | `/incident/breakdown/{unit}?minutes=` | Поломка ТС в потоке |
| POST | `/incident/slowdown/{unit}?minutes=&factor=` | ДТП или засор: замедление ТС |
| POST | `/incident/clear` | Снять внештатные ситуации |

## 12. Коды ответов

| Код | Когда |
|---|---|
| 200 | Успех |
| 400 | Некорректный hex (`/api/ndtp/parse`), некорректный GTFS-архив, скорость реплея вне диапазона |
| 403 | `/v1/train` без `ML_ENABLE_TRAIN=true` |
| 404 | ТС, инцидент или маршрут не найден; терминала нет в реплее |
| 409 | `/v1/train`: обучение уже идёт |
| 422 | Тело запроса не прошло валидацию |
| 501 | Бинарный protobuf GTFS-RT недоступен — используйте `?format=json` |
| 502 | Внешний сервис Overpass не ответил (импорт OSM) |
| 503 | Сервис стартует или ML-сервис недоступен (`/api/model`) |
