###########################
# ИМПОРТЫ И ЗАВИСИМОСТИ
###########################
import io
from pathlib import Path
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import seaborn as sns
from catboost import CatBoostRegressor
from sklearn.metrics import mean_absolute_error


###########################
# КОНФИГУРАЦИЯ И ПУТИ К ДАННЫМ
###########################
PROJECT_ROOT = Path.cwd().parent

MODELS_DIR = PROJECT_ROOT / "models"
MODELS_DIR.mkdir(parents=True, exist_ok=True)

PATHS = {
    # Метки
    "train_labels": PROJECT_ROOT / "dataset" / "labels" / "labels_train.csv",
    "test_labels": PROJECT_ROOT / "dataset" / "labels" / "labels_test.csv",
    
    # Обучающая и тестовая телематика/расписание
    "train_traffic": PROJECT_ROOT / "dataset" / "train" / "traffic.csv",
    "train_schedule": PROJECT_ROOT / "dataset" / "train" / "schedule.csv",
    "test_traffic": PROJECT_ROOT / "dataset" / "test" / "traffic.csv",
    "test_schedule": PROJECT_ROOT / "dataset" / "test" / "schedule.csv",
    
    # Валидация (финальный инференс)
    "val_points": PROJECT_ROOT / "dataset" / "validate" / "points.csv",
    "val_traffic": PROJECT_ROOT / "dataset" / "validate" / "traffic.csv",
    "val_schedule_plan": PROJECT_ROOT / "dataset" / "validate" / "schedule_plan.csv",
    
    # Сабмит
    "sample_submission": PROJECT_ROOT / "dataset" / "sample_submission.csv",
    "output_submission": PROJECT_ROOT / "submission.csv",

    # Директории для моделей и кэша CatBoost
    "models_dir": MODELS_DIR,
    "catboost_info": MODELS_DIR / "catboost_info",
    "model_save_path": MODELS_DIR / "catboost_model.cbm",
}

# Кэш координат остановок: tt_action_item_id -> (lat, lon)
STOP_COORDS_CACHE: dict[int, tuple[float, float]] = {}


###########################
# ОПРЕДЕЛЕНИЕ ПРИЗНАКОВ (FEATURES)
###########################
FEATURE_COLS = [
    "tr_id",
    "cur_dev_s",
    "is_late_now",
    "time_to_target_s",             # Горизонт прогноза в секундах
    "distance_to_target_m",          # Дистанция до целевой остановки (м)
    "required_speed_kmh",            # Требуемая скорость до остановки (км/ч)
    "speed_deficit",                 # Дефицит скорости (required - speed_mean_5m)
    "hour",
    "minute_of_day",
    "sin_time",
    "cos_time",
    "speed",
    "speed_diff",
    "speed_mean_5m",
    "standing_ratio_5m",             # Доля простоя (< 3 км/ч) за 5 минут
    "speed_diff_std_5m",
    "speed_diff_from_hourly_mean",   # Отклонение 5-мин темпа от исторической нормы часа
    "gps_failure",
    "telemetry_age_s",
    "sched_mean_delay",              # Исторический априорный профиль рейса
    "sched_median_delay",
    "sched_std_delay",
    "sched_max_delay",
]

CAT_COLS = ["tr_id"]


###########################
# ВСПОМОГАТЕЛЬНЫЕ ГЕО-ФУНКЦИИ
###########################
def haversine_distance(lat1: np.ndarray, lon1: np.ndarray, lat2: np.ndarray, lon2: np.ndarray) -> np.ndarray:
    """Вычисляет расстояние в метрах между массивами координат на сфере."""
    R = 6371000.0  # Радиус Земли в метрах
    phi1 = np.radians(lat1)
    phi2 = np.radians(lat2)
    dphi = np.radians(lat2 - lat1)
    dlambda = np.radians(lon2 - lon1)
    
    a = np.sin(dphi / 2.0) ** 2 + np.cos(phi1) * np.cos(phi2) * np.sin(dlambda / 2.0) ** 2
    c = 2.0 * np.arctan2(np.sqrt(a), np.sqrt(1.0 - a))
    return R * c


def init_stop_coordinates() -> None:
    """Заполняет кэш координат остановок из доступных файлов расписания."""
    for path_key in ("train_schedule", "test_schedule", "val_schedule_plan"):
        path = PATHS.get(path_key)
        if path and Path(path).exists():
            load_and_preprocess_schedule(path)


###########################
# ЗАГРУЗКА И ПРЕДОБРАБОТКА ДАННЫХ
###########################
def load_and_preprocess_traffic(traffic_path: Path | io.StringIO | str) -> pd.DataFrame:
    """Загружает телематику, фильтрует GPS, восстанавливает скорость и
    рассчитывает 5-минутные оконные характеристики кинематики движения.
    """
    df = pd.read_csv(traffic_path, low_memory=False)
    
    if "device_event_id" in df.columns:
        df = df.drop(columns=["device_event_id"])
        
    df["event_time"] = pd.to_datetime(df["event_time"])
    df = df.sort_values(by=["unit_id", "event_time"]).reset_index(drop=True)

    # 1. Жесткая маска битого GPS: нули, NaN или явный флаг системы
    bad_gps = (
        (~df["location_valid"]) | 
        (df["lon"].isna()) | 
        (df["lat"].isna()) | 
        (df["lon"] < 30) | (df["lon"] > 40) |  # Выход за границы Московского региона
        (df["lat"] < 50) | (df["lat"] > 60)
    )
    df["gps_failure"] = bad_gps.astype(int)

    # 2. Невалидные координаты обнуляем/ставим NaN
    df.loc[bad_gps, ["lon", "lat"]] = np.nan

    # 3. Фильтруем физические выбросы скорости
    df.loc[(df["speed"] < 0) | (df["speed"] > 110), "speed"] = np.nan
    
    # 4. Восстанавливаем скорость строго из прошлого (без утечки будущего)
    df["speed"] = df.groupby("unit_id")["speed"].transform(
        lambda s: s.ffill(limit=6).fillna(0.0)
    )
    
    # 5. Мгновенные кинематические признаки
    df["is_standing"] = (df["speed"] < 3.0).astype(int)
    df["speed_diff"] = df.groupby("unit_id")["speed"].diff().fillna(0.0).clip(-20, 20)

    # 6. Оконные признаки за 5 минут до точки T (через .to_numpy() без ошибок MultiIndex)
    if not df.empty:
        roll_5m = df.groupby("unit_id", sort=False).rolling("5min", on="event_time")
        df["speed_mean_5m"] = np.nan_to_num(roll_5m["speed"].mean().to_numpy(), nan=0.0)
        df["standing_ratio_5m"] = np.nan_to_num(roll_5m["is_standing"].mean().to_numpy(), nan=1.0)
        df["speed_diff_std_5m"] = np.nan_to_num(roll_5m["speed_diff"].std().to_numpy(), nan=0.0)
    else:
        df["speed_mean_5m"] = 0.0
        df["standing_ratio_5m"] = 1.0
        df["speed_diff_std_5m"] = 0.0

    return df


def load_and_preprocess_labels(labels_path: Path | io.StringIO | str) -> pd.DataFrame:
    """Загружает метки и контрольные точки T, генерирует временные признаки
    и признаки текущего отклонения от графика."""
    df = pd.read_csv(labels_path)

    t_col = "T"
    df[t_col] = pd.to_datetime(df[t_col])

    # 1. Feature Engineering по отклонению
    if "cur_dev_s" in df.columns:
        df["is_late_now"] = (df["cur_dev_s"] > 0).astype(int)

    # 2. Расчет горизонта прибытия до целевой остановки (10-15 минут)
    if "target_time_begin" in df.columns:
        df["target_time_begin"] = pd.to_datetime(df["target_time_begin"])
        df["time_to_target_s"] = (df["target_time_begin"] - df[t_col]).dt.total_seconds()

    # 3. Feature Engineering по времени суток
    df["hour"] = df[t_col].dt.hour
    df["minute"] = df[t_col].dt.minute
    df["minute_of_day"] = df["hour"] * 60 + df["minute"]
    df["sin_time"] = np.sin(2 * np.pi * df["minute_of_day"] / 1440.0)
    df["cos_time"] = np.cos(2 * np.pi * df["minute_of_day"] / 1440.0)

    return df


def load_and_preprocess_schedule(schedule_path: Path | str) -> pd.DataFrame:
    """Загружает расписание, кэширует координаты остановок из geom и переводит даты в datetime."""
    df = pd.read_csv(schedule_path)

    # Извлечение координат остановок POINT (lon lat)
    if "geom" in df.columns and "tt_action_item_id" in df.columns:
        extracted = df["geom"].dropna().astype(str).str.extract(r"POINT\s*\(\s*([0-9.]+)\s+([0-9.]+)\s*\)")
        stop_lon = pd.to_numeric(extracted[0], errors="coerce")
        stop_lat = pd.to_numeric(extracted[1], errors="coerce")
        valid_mask = stop_lat.notna() & stop_lon.notna()
        
        stop_ids = df.loc[valid_mask.index[valid_mask], "tt_action_item_id"]
        lats = stop_lat[valid_mask]
        lons = stop_lon[valid_mask]
        for sid, lat, lon in zip(stop_ids, lats, lons):
            STOP_COORDS_CACHE[int(sid)] = (float(lat), float(lon))

    drop_cols = ["order_date", "building_address", "geom"]
    df = df.drop(columns=[c for c in drop_cols if c in df.columns])

    for col in df.columns:
        if "time" in col.lower():
            df[col] = pd.to_datetime(df[col])

    return df


###########################
# ГЕНЕРАЦИЯ ПРИЗНАКОВ И СБОРКА ДАТАСЕТА
###########################
def compute_schedule_features(
    df_schedule: pd.DataFrame, 
    traffic_path: Path | str | None = None
) -> pd.DataFrame:
    """Считает историческую статистику опозданий по рейсам (tr_id) на основе расписания
    и формирует исторический профиль почасовой скорости движения.
    """
    sched = df_schedule.copy()
    if "time_fact_begin" not in sched.columns or "time_begin" not in sched.columns:
        return pd.DataFrame(columns=[
            "tr_id", 
            "sched_mean_delay", 
            "sched_median_delay", 
            "sched_std_delay", 
            "sched_max_delay"
        ])

    sched["sched_delay_s"] = (sched["time_fact_begin"] - sched["time_begin"]).dt.total_seconds()
    
    tr_stats = sched.groupby("tr_id")["sched_delay_s"].agg(
        sched_mean_delay="mean",
        sched_median_delay="median",
        sched_std_delay="std",
        sched_max_delay="max"
    ).reset_index()
    tr_stats["sched_std_delay"] = tr_stats["sched_std_delay"].fillna(0.0)

    # Расчет исторического профиля скорости по часам суток (speed_h0 ... speed_h23)
    if traffic_path is None:
        traffic_path = PATHS.get("train_traffic")

    hour_cols = [f"speed_h{h}" for h in range(24)]
    if traffic_path is not None and Path(traffic_path).exists():
        try:
            trf = pd.read_csv(traffic_path, usecols=["tr_id", "event_time", "speed"], low_memory=False)
            trf["event_time"] = pd.to_datetime(trf["event_time"])
            trf = trf[(trf["speed"] >= 0) & (trf["speed"] <= 110)]
            trf["hour"] = trf["event_time"].dt.hour

            tr_mean = trf.groupby("tr_id")["speed"].mean().rename("tr_mean_speed").reset_index()
            hourly_pivot = trf.pivot_table(index="tr_id", columns="hour", values="speed", aggfunc="mean")
            hourly_pivot.columns = [f"speed_h{int(c)}" for c in hourly_pivot.columns]
            hourly_pivot = hourly_pivot.reset_index()

            tr_stats = tr_stats.merge(tr_mean, on="tr_id", how="left")
            tr_stats = tr_stats.merge(hourly_pivot, on="tr_id", how="left")
        except Exception:
            tr_stats["tr_mean_speed"] = 20.0
            for c in hour_cols:
                tr_stats[c] = 20.0
    else:
        tr_stats["tr_mean_speed"] = 20.0
        for c in hour_cols:
            tr_stats[c] = 20.0

    tr_stats["tr_mean_speed"] = tr_stats["tr_mean_speed"].fillna(20.0)
    for c in hour_cols:
        if c in tr_stats.columns:
            tr_stats[c] = tr_stats[c].fillna(tr_stats["tr_mean_speed"])
        else:
            tr_stats[c] = tr_stats["tr_mean_speed"]

    return tr_stats


def build_dataset(
    labels_or_points_df: pd.DataFrame,
    traffic_df: pd.DataFrame,
    schedule_features: pd.DataFrame,
    time_col: str = "T",
    tolerance_minutes: int = 30,
) -> pd.DataFrame:
    """Объединяет признаки меток, расписания и телематики в единый датафрейм
    строго назад во времени (merge_asof).
    """
    if not STOP_COORDS_CACHE:
        init_stop_coordinates()

    # 1. Присоединяем статистики расписания по tr_id
    df = labels_or_points_df.merge(
        schedule_features, on="tr_id", how="left"
    ).fillna(0.0)

    # 2. Сортировка по времени перед merge_asof
    df = df.sort_values(time_col).reset_index(drop=True)
    traffic_sorted = traffic_df.sort_values("event_time").reset_index(drop=True)

    # 3. Сопоставление с последней известной телеметрией строго назад во времени
    traffic_cols = [
        "tr_id",
        "event_time",
        "lon",
        "lat",
        "speed",
        "speed_diff",
        "heading",
        "alt",
        "gps_failure",
        "speed_mean_5m",
        "standing_ratio_5m",
        "speed_diff_std_5m",
    ]

    for col in traffic_cols:
        if col not in traffic_sorted.columns:
            traffic_sorted[col] = np.nan

    merged = pd.merge_asof(
        df,
        traffic_sorted[traffic_cols],
        left_on=time_col,
        right_on="event_time",
        by="tr_id",
        direction="backward",
        tolerance=pd.Timedelta(minutes=tolerance_minutes),
    )

    # 4. Расчет давности пакета телеметрии к моменту T
    merged["telemetry_age_s"] = (
        (merged[time_col] - merged["event_time"]).dt.total_seconds().fillna(999.0)
    )

    fill_defaults = {
        "lon": np.nan,
        "lat": np.nan,
        "speed": 0.0,
        "speed_diff": 0.0,
        "heading": 0.0,
        "alt": 150.0,
        "gps_failure": 1,
        "speed_mean_5m": 0.0,
        "standing_ratio_5m": 1.0,
        "speed_diff_std_5m": 0.0,
    }
    merged = merged.fillna(fill_defaults)

    # 5. Геометрия: расстояние, требуемая скорость и дефицит скорости
    stop_ids = merged["target_stop_id"].values
    stop_lats = np.array([STOP_COORDS_CACHE.get(int(sid), (np.nan, np.nan))[0] for sid in stop_ids])
    stop_lons = np.array([STOP_COORDS_CACHE.get(int(sid), (np.nan, np.nan))[1] for sid in stop_ids])

    raw_dist = haversine_distance(merged["lat"].values, merged["lon"].values, stop_lats, stop_lons)
    merged["distance_to_target_m"] = np.nan_to_num(raw_dist, nan=3500.0).clip(50.0, 20000.0)

    target_time_s = merged["time_to_target_s"].clip(60.0, 1800.0).values
    req_speed = (merged["distance_to_target_m"].values / target_time_s) * 3.6
    merged["required_speed_kmh"] = np.nan_to_num(req_speed, nan=20.0).clip(0.0, 100.0)

    merged["speed_deficit"] = merged["required_speed_kmh"] - merged["speed_mean_5m"]

    # Защита от ложного дефицита: при сбое GPS или устаревшей телеметрии обнуляем дефицит
    bad_telemetry_mask = (merged["gps_failure"] == 1) | (merged["telemetry_age_s"] > 600.0) | (merged["event_time"].isna())
    merged.loc[bad_telemetry_mask, "speed_deficit"] = 0.0

    # 6. Разница темпа с исторической нормой в этот час
    hour_cols = [f"speed_h{h}" for h in range(24)]
    if set(hour_cols).issubset(merged.columns):
        hour_indices = merged["hour"].clip(0, 23).astype(int).values
        speed_matrix = merged[hour_cols].values
        expected_speed = speed_matrix[np.arange(len(merged)), hour_indices]
        fallback = merged["tr_mean_speed"].replace(0.0, 20.0).values if "tr_mean_speed" in merged.columns else 20.0
        expected_speed = np.where(expected_speed > 0, expected_speed, fallback)
    elif "tr_mean_speed" in merged.columns:
        expected_speed = merged["tr_mean_speed"].replace(0.0, 20.0).values
    else:
        expected_speed = 20.0

    merged["speed_diff_from_hourly_mean"] = merged["speed_mean_5m"] - expected_speed
    merged.loc[merged["event_time"].isna(), "speed_diff_from_hourly_mean"] = 0.0

    # 7. Дропаем служебные столбцы
    drop_cols = ["event_time", "lon", "lat", "tr_mean_speed"] + hour_cols
    merged = merged.drop(columns=[c for c in drop_cols if c in merged.columns], errors="ignore")

    return merged


###########################
# ОБУЧЕНИЕ И ИНФЕРЕНС МОДЕЛИ
###########################
def train(
    train_df: pd.DataFrame, 
    val_df: pd.DataFrame | None = None, 
    target_col: str = "target_delay_s", 
    val_ratio: float = 0.2,
    fit_on_all: bool = False
) -> CatBoostRegressor:
    """Обучает CatBoostRegressor предсказывать остаток (дельту) отклонения:
    target_delta = target_delay_s - cur_dev_s.
    Если fit_on_all=True, после валидации модель дообучается на объединении train+test.
    """
    df = train_df.sort_values("T").reset_index(drop=True)
    
    y_tr_full = df[target_col]
    y_tr_delta = df[target_col] - df["cur_dev_s"]
    X_tr = df[FEATURE_COLS]

    if val_df is not None:
        # Честная валидация на отложенном test (реальные ТС)
        X_val = val_df[FEATURE_COLS]
        y_val_full = val_df[target_col]
        y_val_delta = val_df[target_col] - val_df["cur_dev_s"]
    else:
        split_idx = int(len(df) * (1 - val_ratio))
        X_tr = df.iloc[:split_idx][FEATURE_COLS]
        y_tr_delta = y_tr_delta.iloc[:split_idx]
        
        X_val = df.iloc[split_idx:][FEATURE_COLS]
        y_val_full = y_tr_full.iloc[split_idx:]
        y_val_delta = y_val_delta.iloc[split_idx:]

    model = CatBoostRegressor(
        iterations=1000,
        learning_rate=0.05,
        depth=6,
        loss_function="MAE",
        eval_metric="MAE",
        cat_features=CAT_COLS,
        random_seed=42,
        verbose=100,
        train_dir=str(PATHS["catboost_info"])
    )

    model.fit(
        X_tr, y_tr_delta,
        eval_set=(X_val, y_val_delta),
        early_stopping_rounds=50,
        use_best_model=True
    )

    predicted_delta = model.predict(X_val)
    final_val_preds = X_val["cur_dev_s"] + predicted_delta
    
    model_mae = mean_absolute_error(y_val_full, final_val_preds)
    baseline_mae = mean_absolute_error(y_val_full, X_val["cur_dev_s"])
    
    print(f"\n--- Результаты валидации (Holdout Test) ---")
    print(f"MAE Baseline (cur_dev_s): {baseline_mae:.2f} s")
    print(f"MAE Model:               {model_mae:.2f} s")
    print(f"Улучшение:               {baseline_mae - model_mae:.2f} s\n")

    # Дообучение на объединении train + test для финального инференса
    if fit_on_all and val_df is not None:
        print("Дообучение финальной модели на объединении train + test...")
        best_iter = model.get_best_iteration() or 300
        full_df = pd.concat([train_df, val_df], ignore_index=True).sort_values("T").reset_index(drop=True)
        X_all = full_df[FEATURE_COLS]
        y_all_delta = full_df[target_col] - full_df["cur_dev_s"]

        model = CatBoostRegressor(
            iterations=max(100, best_iter),
            learning_rate=0.05,
            depth=6,
            loss_function="MAE",
            eval_metric="MAE",
            cat_features=CAT_COLS,
            random_seed=42,
            verbose=100,
            train_dir=str(PATHS["catboost_info"])
        )
        model.fit(X_all, y_all_delta)

    model.save_model(str(PATHS["model_save_path"]))
    print(f"Модель сохранена в: {PATHS['model_save_path']}")

    return model


def predict(model: CatBoostRegressor, test_df: pd.DataFrame) -> np.ndarray:
    """Выполняет инференс: возвращает cur_dev_s + предсказанная дельта."""
    predicted_delta = model.predict(test_df[FEATURE_COLS])
    return test_df["cur_dev_s"].values + predicted_delta


###########################
# ЭКСПОРТ САБМИТА
###########################
def make_submission(
    model: CatBoostRegressor, 
    test_df: pd.DataFrame, 
    sample_sub_path: Path, 
    output_path: Path
) -> None:
    """Формирует и сохраняет submission.csv строго по спецификации организаторов:
    разделитель ';', колонки sample_id;prediction. Порядок строк строго по sample_sub_path.
    """
    sub = pd.read_csv(sample_sub_path, sep=";")
    test_df_copy = test_df.copy()
    test_df_copy["prediction"] = predict(model, test_df)

    # Сохраняем исходный порядок sample_submission по sample_id
    sub = sub[["sample_id"]].merge(
        test_df_copy[["sample_id", "prediction"]], 
        on="sample_id", 
        how="left"
    )
    sub["prediction"] = sub["prediction"].fillna(0.0)

    sub.to_csv(output_path, sep=";", index=False)
    print(f"Сабмит успешно сохранен в {output_path}.")
    print(f"Колонки: {list(sub.columns)}, Разделитель: ';', Строк: {len(sub)}")