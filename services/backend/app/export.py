"""Экспорт для внешних картографических сервисов (Яндекс Карты, 2ГИС, Google, Transit и др.).

* GTFS-Realtime — стандарт, в котором перевозчики отдают данные картографическим сервисам:
  VehiclePositions (положения ТС), TripUpdates (ETA-движок + ML-прогноз на целевой остановке),
  Alerts (инциденты диспетчерской). Формат protobuf или JSON (``?format=json``).
* Статический GTFS (zip) с теми же stop_id/route_id/trip_id: партнёр связывает realtime с расписанием.
* GeoJSON FeatureCollection ТС для быстрого встраивания в JS API карт.
"""
from __future__ import annotations

import csv
import hashlib
import io
import time
import zipfile
from datetime import datetime, timezone

from .reference import VehicleSchedule

try:
    from google.transit import gtfs_realtime_pb2  # gtfs-realtime-bindings
except Exception:  # noqa: BLE001 — без пакета работает JSON-вариант фидов
    gtfs_realtime_pb2 = None

LAYOVER = 360  # пауза ≥ 6 мин между остановками = отстой на конечной, граница рейса


def stop_id(stop_key: str) -> str:
    """Стабильный GTFS stop_id по физической остановке.

    Args:
        stop_key: Ключ остановки ``"lon,lat"``.

    Returns:
        str: ``"s" + 10 hex`` (md5 — только для компактного детерминированного ID, не для безопасности).
    """
    return "s" + hashlib.md5(stop_key.encode()).hexdigest()[:10]


def trips_of(sch: VehicleSchedule) -> list[tuple[int, int]]:
    """Разбивает нитку графика ТС на рейсы по межрейсовым отстоям.

    Args:
        sch: Нитка графика.

    Returns:
        list[tuple[int, int]]: Индексы ``(i_start, i_end)`` включительно для каждого рейса.
    """
    bounds, start = [], 0
    for i in range(1, len(sch.stops)):
        if sch.stops[i].plan_ts - sch.stops[i - 1].plan_ts >= LAYOVER:
            bounds.append((start, i - 1))
            start = i
    bounds.append((start, len(sch.stops) - 1))
    return bounds


def trip_id(tr: int, n: int) -> str:
    """GTFS trip_id: ``<tr_id>_<номер рейса с 1>``."""
    return f"{tr}_{n + 1}"


def route_ref(engine, tr: int) -> tuple[str, str]:
    """Маршрут ТС для экспорта.

    Args:
        engine: Движок backend.
        tr: tr_id.

    Returns:
        tuple[str, str]: ``(route_id, номер для пассажира)``; реальный номер OSM, если ТС привязано,
        иначе внутренний ``M<n>``.
    """
    rn = engine.route_names.get(tr)
    sch = engine.ref.schedules[tr]
    return (rn["id"], rn["ref"]) if rn else (sch.route_id, sch.route_id)


# ----------------------------------------------------------------------------- realtime
def _trip_for(engine, st, idx: int) -> tuple[str, int] | None:
    for n, (a, b) in enumerate(engine.export_trips(st.tr_id)):
        if a <= idx <= b:
            return trip_id(st.tr_id, n), idx - a + 1
    return None


def feed(engine, kind: str) -> dict:
    """Собирает фид GTFS-Realtime.

    Args:
        engine: Движок backend (текущее состояние ТС и инцидентов).
        kind: ``"vehicle_positions"``, ``"trip_updates"`` или ``"alerts"``.

    Returns:
        dict: FeedMessage в JSON-представлении protobuf (``header`` + ``entity``).
    """
    T = engine.clock.now()
    ents = []
    for st in engine.vehicles.values():
        if st.lat is None:
            continue
        vid = str(st.tr_id or st.unit_id)
        eta = st.eta
        if kind == "vehicle_positions":
            # без связи отдаём расчётное положение (счисление пути), а не устаревшую точку
            lat, lon = (eta.est_lat, eta.est_lon) if eta is not None and eta.mode == "dead_reckoning" and eta.est_lat else (st.lat, st.lon)
            v = {"vehicle": {"id": vid, "label": vid},
                 "position": {"latitude": lat, "longitude": lon, "bearing": st.heading, "speed": st.speed / 3.6},  # GTFS: м/с
                 "timestamp": int(st.last_ts)}
            if st.schedule is not None and eta is not None and eta.stops:
                t = _trip_for(engine, st, eta.stops[0].idx)
                if t:
                    rid, _ = route_ref(engine, st.tr_id)
                    v["trip"] = {"trip_id": t[0], "route_id": rid}
                    v["current_stop_sequence"] = t[1]
                    v["stop_id"] = stop_id(eta.stops[0].stop_key)
                v["current_status"] = "IN_TRANSIT_TO"
            ents.append({"id": f"vp-{vid}", "vehicle": v})
        elif kind == "trip_updates" and st.schedule is not None and eta is not None and eta.stops:
            ml = st.prediction if st.prediction and st.prediction.get("status") == "ok" else None
            by_trip: dict[str, list] = {}
            for e in eta.stops:
                t = _trip_for(engine, st, e.idx)
                if not t:
                    continue
                arrival = e.eta_ts
                # на целевой остановке горизонта 10–15 мин ML точнее кинематического ETA
                if ml and ml.get("target_idx") == e.idx:
                    arrival = e.plan_ts + ml["predicted_delay_s"]
                by_trip.setdefault(t[0], []).append({
                    "stop_sequence": t[1], "stop_id": stop_id(e.stop_key),
                    "schedule_relationship": "SKIPPED" if e.skipped else "SCHEDULED",
                    "arrival": {"time": int(arrival), "delay": int(arrival - e.plan_ts), "uncertainty": int(e.sigma_s)},
                })
            rid, _ = route_ref(engine, st.tr_id)
            for tid, upd in by_trip.items():
                ents.append({"id": f"tu-{tid}", "trip_update": {
                    "trip": {"trip_id": tid, "route_id": rid}, "vehicle": {"id": vid},
                    "stop_time_update": upd, "timestamp": int(T)}})
    if kind == "alerts":
        # коды причин → перечисление Alert.Cause стандарта GTFS-RT
        cause_map = {"STANDING_OFF_STOP": "ACCIDENT", "NO_SIGNAL": "TECHNICAL_PROBLEM", "GPS_FAILURE": "TECHNICAL_PROBLEM",
                     "SLOW_SEGMENT": "OTHER_CAUSE", "PEAK": "OTHER_CAUSE"}
        for inc in engine.snapshot.get("incidents", []):
            c = (inc.get("causes") or [{}])[0]
            rid, ref = route_ref(engine, inc["tr_id"]) if inc["tr_id"] in engine.ref.schedules else ("", "")
            ents.append({"id": f"al-{inc['id']}", "alert": {
                "active_period": [{"start": int(inc["opened_T"])}],
                "informed_entity": [{"route_id": rid}],
                "cause": cause_map.get(c.get("code"), "UNKNOWN_CAUSE"),
                "effect": "SIGNIFICANT_DELAYS",
                "header_text": {"translation": [{"text": f"Маршрут {ref}: задержка до {max(0, int((inc.get('predicted_delay_s') or 0) // 60))} мин", "language": "ru"}]},
                "description_text": {"translation": [{"text": f"{c.get('title', '')}. {c.get('detail', '')}", "language": "ru"}]},
            }})
    return {"header": {"gtfs_realtime_version": "2.0", "incrementality": "FULL_DATASET", "timestamp": int(T)},
            "entity": ents}


def to_protobuf(d: dict) -> bytes:
    """Сериализует фид в бинарный protobuf GTFS-RT.

    Args:
        d: Фид из :func:`feed`.

    Returns:
        bytes: FeedMessage.

    Raises:
        RuntimeError: Не установлен ``gtfs-realtime-bindings``.
        google.protobuf.json_format.ParseError: Структура фида не соответствует схеме.
    """
    if gtfs_realtime_pb2 is None:
        raise RuntimeError("gtfs-realtime-bindings не установлен")
    from google.protobuf import json_format
    msg = gtfs_realtime_pb2.FeedMessage()
    json_format.ParseDict(d, msg)
    return msg.SerializeToString()


# ----------------------------------------------------------------------------- static GTFS
def gtfs_static(engine, agency_name: str = "Мосгортранс (демо)", tz: str = "Europe/Moscow") -> bytes:
    """Статический GTFS (zip) по эталонному расписанию и геометрии по дорогам.

    Args:
        engine: Движок backend.
        agency_name: Название перевозчика в ``agency.txt``.
        tz: Часовой пояс перевозчика.

    Returns:
        bytes: Zip-архив: agency, calendar, stops, routes, trips, stop_times, shapes.
    """
    ref = engine.ref
    buf = io.BytesIO()
    z = zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED)

    def write(name: str, header: list[str], rows: list[list]) -> None:
        s = io.StringIO()
        w = csv.writer(s, lineterminator="\n")
        w.writerow(header)
        w.writerows(rows)
        z.writestr(name, s.getvalue())

    first_ts = min((sch.stops[0].plan_ts for sch in ref.schedules.values() if sch.stops), default=time.time())
    day = datetime.fromtimestamp(first_ts, tz=timezone.utc).strftime("%Y%m%d")
    # в датасете один день — календарь на одну дату
    write("agency.txt", ["agency_id", "agency_name", "agency_url", "agency_timezone", "agency_lang"],
          [["mos", agency_name, "https://transport.mos.ru", tz, "ru"]])
    write("calendar.txt", ["service_id", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday",
                           "sunday", "start_date", "end_date"], [["day", 1, 1, 1, 1, 1, 1, 1, day, day]])
    write("stops.txt", ["stop_id", "stop_name", "stop_lat", "stop_lon"],
          [[stop_id(k), s["address"], s["lat"], s["lon"]] for k, s in ref.stops.items()])
    routes, trips, times, shapes = {}, [], [], []
    for tr, sch in ref.schedules.items():
        rid, rref = route_ref(engine, tr)
        routes[rid] = [rid, "mos", rref, "", 3]  # route_type 3 = автобус
        for n, (a, b) in enumerate(trips_of(sch)):
            tid = trip_id(tr, n)
            trips.append([rid, "day", tid, sch.stops[b].address, f"sh_{tid}"])
            seq = 0
            for i in range(a, b + 1):
                s = sch.stops[i]
                hhmmss = _gtfs_time(s.plan_ts, first_ts)
                times.append([tid, hhmmss, hhmmss, stop_id(s.stop_key), i - a + 1])
                if i > a:
                    # [1:] — первая точка перегона совпадает с последней предыдущего
                    for c in ref.polyline(sch.stops[i - 1], s).coords[1:]:
                        seq += 1
                        shapes.append([f"sh_{tid}", c[0], c[1], seq])
                else:
                    seq += 1
                    shapes.append([f"sh_{tid}", s.lat, s.lon, seq])
    write("routes.txt", ["route_id", "agency_id", "route_short_name", "route_long_name", "route_type"], list(routes.values()))
    write("trips.txt", ["route_id", "service_id", "trip_id", "trip_headsign", "shape_id"], trips)
    write("stop_times.txt", ["trip_id", "arrival_time", "departure_time", "stop_id", "stop_sequence"], times)
    write("shapes.txt", ["shape_id", "shape_pt_lat", "shape_pt_lon", "shape_pt_sequence"], shapes)
    z.close()
    return buf.getvalue()


def _gtfs_time(ts: float, day_start_ts: float) -> str:
    """Время GTFS в МСК относительно полуночи сервисного дня.

    Может быть > 24:00 для рейсов после полуночи: так требует спецификация GTFS.
    """
    local0 = datetime.fromtimestamp(day_start_ts, tz=timezone.utc)
    midnight = datetime(local0.year, local0.month, local0.day, tzinfo=timezone.utc).timestamp() - 3 * 3600  # полночь МСК (UTC+3, без перехода на летнее время)
    sec = int(ts - midnight)
    return f"{sec // 3600:02d}:{sec % 3600 // 60:02d}:{sec % 60:02d}"


def geojson(engine) -> dict:
    """GeoJSON-слой ТС для JS API карт.

    Args:
        engine: Движок backend.

    Returns:
        dict: FeatureCollection точек с уровнем риска, прогнозом и ближайшими остановками;
        при потере связи — расчётное положение.
    """
    feats = []
    for v in engine.snapshot.get("vehicles", []):
        lat = v.get("est_lat") or v.get("lat")
        lon = v.get("est_lon") or v.get("lon")
        if lat is None:
            continue
        feats.append({"type": "Feature", "geometry": {"type": "Point", "coordinates": [lon, lat]},  # GeoJSON: lon, lat
                      "properties": {k: v.get(k) for k in ("tr_id", "unit_id", "route_ref", "route_id", "level",
                                                           "predicted_delay_s", "p_late", "heading", "speed",
                                                           "stale", "eta_mode", "cause", "next_stops")}})
    return {"type": "FeatureCollection", "features": feats, "data_time": engine.snapshot.get("data_time_local")}
