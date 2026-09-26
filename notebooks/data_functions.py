###########################
# ИМПОРТЫ И ЗАВИСИМОСТИ
###########################
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

# Папка models и подпапка для кэша
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

###########################
# ОПРЕДЕЛЕНИЕ ПРИЗНАКОВ (FEATURES)
###########################
FEATURE_COLS = [
    "tr_id",
    "cur_dev_s",
    "abs_cur_dev_s",
    "is_late_now",
    "is_ontime_now",
    "time_to_target_s",  # Расчет горизонта прогноза в секундах
    "hour",
    "minute_of_day",
    "sin_time",
    "cos_time",
    "is_morning_peak",
    "is_evening_peak",
    "speed",
    "is_standing",
    "speed_diff",
    "gps_failure",
    "telemetry_age_s",
    "telemetry_missing",
    "sched_mean_delay",
    "sched_median_delay",
    "sched_std_delay",
    "sched_max_delay",
]

CAT_COLS = ["tr_id"]


###########################
# ЗАГРУЗКА И ПРЕДОБРАБОТКА ДАННЫХ
###########################
def load_and_preprocess_traffic(traffic_path: Path) -> pd.DataFrame:
    """Загружает телематику, фильтрует сбои GPS и восстанавливает кинематику."""
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
    
    # 4. Восстанавливаем скорость только из прошлого (без утечки будущего)
    df["speed"] = df.groupby("unit_id")["speed"].transform(
        lambda s: s.ffill(limit=6).fillna(0.0)
    )
    
    # 5. Признаки кинематики
    df["is_standing"] = (df["speed"] < 3.0).astype(int)
    df["speed_diff"] = df.groupby("unit_id")["speed"].diff().fillna(0.0).clip(-20, 20)

    return df


def load_and_preprocess_labels(labels_path: Path) -> pd.DataFrame:
    """Загружает метки и контрольные точки T, генерирует временные признаки
    и признаки текущего отклонения от графика."""
    df = pd.read_csv(labels_path)

    t_col = "T"
    df[t_col] = pd.to_datetime(df[t_col])

    # 1. Feature Engineering по отклонению от графика
    if "cur_dev_s" in df.columns:
        df["is_late_now"] = (df["cur_dev_s"] > 0).astype(int)
        df["abs_cur_dev_s"] = df["cur_dev_s"].abs()
        df["is_ontime_now"] = (df["cur_dev_s"].abs() <= 60).astype(int)

    # 2. Расчет времени до целевой остановки (горизонт 10-15 минут)
    if "target_time_begin" in df.columns:
        df["target_time_begin"] = pd.to_datetime(df["target_time_begin"])
        df["time_to_target_s"] = (df["target_time_begin"] - df[t_col]).dt.total_seconds()

    # 3. Feature Engineering по времени суток
    df["hour"] = df[t_col].dt.hour
    df["minute"] = df[t_col].dt.minute
    df["minute_of_day"] = df["hour"] * 60 + df["minute"]
    df["sin_time"] = np.sin(2 * np.pi * df["minute_of_day"] / 1440.0)
    df["cos_time"] = np.cos(2 * np.pi * df["minute_of_day"] / 1440.0)

    # Флаги пиковых часов для наземного транспорта Москвы
    df["is_morning_peak"] = (
        (df["hour"] >= 7) & (df["hour"] <= 10)
    ).astype(int)
    df["is_evening_peak"] = (
        (df["hour"] >= 17) & (df["hour"] <= 20)
    ).astype(int)

    return df


def load_and_preprocess_schedule(schedule_path: Path) -> pd.DataFrame:
    """Загружает расписание, чистит адреса и переводит даты в datetime."""
    df = pd.read_csv(schedule_path)

    drop_cols = ["order_date", "building_address"]
    df = df.drop(columns=[c for c in drop_cols if c in df.columns])

    for col in df.columns:
        if "time" in col.lower():
            df[col] = pd.to_datetime(df[col])

    return df


###########################
# ГЕНЕРАЦИЯ ПРИЗНАКОВ И СБОРКА ДАТАСЕТА
###########################
def compute_schedule_features(df_schedule: pd.DataFrame) -> pd.DataFrame:
    """Считает историческую статистику опозданий по рейсам (tr_id) на основе расписания."""
    sched = df_schedule.copy()
    if "time_fact_begin" not in sched.columns or "time_begin" not in sched.columns:
        return pd.DataFrame(columns=[
            "tr_id", 
            "sched_mean_delay", 
            "sched_median_delay", 
            "sched_std_delay", 
            "sched_max_delay"
        ])

    # Фактическое отклонение на остановке по расписанию в секундах
    sched["sched_delay_s"] = (sched["time_fact_begin"] - sched["time_begin"]).dt.total_seconds()
    
    # Агрегация по рейсу (tr_id)
    tr_stats = sched.groupby("tr_id")["sched_delay_s"].agg(
        sched_mean_delay="mean",
        sched_median_delay="median",
        sched_std_delay="std",
        sched_max_delay="max"
    ).reset_index()
    
    tr_stats["sched_std_delay"] = tr_stats["sched_std_delay"].fillna(0.0)
    return tr_stats


def build_dataset(
    labels_or_points_df: pd.DataFrame,
    traffic_df: pd.DataFrame,
    schedule_features: pd.DataFrame,
    time_col: str = "T",
    tolerance_minutes: int = 30,
) -> pd.DataFrame:
    """Объединяет признаки меток, статистики расписания и телематики в единый

    датафрейм строго назад во времени (merge_asof).
    """
    # 1. Присоединяем статистики расписания по tr_id
    df = labels_or_points_df.merge(
        schedule_features, on="tr_id", how="left"
    ).fillna(0.0)

    # 2. Сортировка по времени перед merge_asof
    df = df.sort_values(time_col).reset_index(drop=True)
    traffic_sorted = traffic_df.sort_values("event_time").reset_index(drop=True)

    # 3. Сопоставление с последней известной телеметрией строго назад по времени
    traffic_cols = [
        "tr_id",
        "event_time",
        "speed",
        "is_standing",
        "speed_diff",
        "heading",
        "alt",
        "gps_failure",
    ]

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
    merged["telemetry_missing"] = merged["event_time"].isna().astype(int)

    # Заполняем пропуски дефолтными значениями, если пакетов для ТС не нашлось
    fill_defaults = {
        "speed": 0.0,
        "is_standing": 1,
        "speed_diff": 0.0,
        "heading": 0.0,
        "alt": 150.0,
        "gps_failure": 1,
    }
    merged = merged.fillna(fill_defaults)

    # 5. Дропаем служебную колонку event_time
    merged = merged.drop(columns=["event_time"], errors="ignore")

    return merged


###########################
# ОБУЧЕНИЕ И ИНФЕРЕНС МОДЕЛИ
###########################
def train(
    train_df: pd.DataFrame, 
    val_df: pd.DataFrame = None, 
    target_col: str = "target_delay_s", 
    val_ratio: float = 0.2
) -> CatBoostRegressor:
    """Обучает CatBoostRegressor предсказывать остаток (дельту) отклонения:
    target_delta = target_delay_s - cur_dev_s.
    Все логи, графики и кэш обучения пишутся в models/catboost_info/.
    """
    df = train_df.sort_values("T").reset_index(drop=True)
    
    # Целевая переменная — прирост задержки относительно текущей
    y_tr_full = df[target_col]
    y_tr_delta = df[target_col] - df["cur_dev_s"]
    X_tr = df[FEATURE_COLS]

    if val_df is not None:
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

    # Перенаправляем кэш и логи в папку models/catboost_info
    model = CatBoostRegressor(
        iterations=1000,
        learning_rate=0.05,
        depth=6,
        loss_function="MAE",
        eval_metric="MAE",
        cat_features=CAT_COLS,
        random_seed=42,
        verbose=100,
        train_dir=str(PATHS["catboost_info"])  # <--- КЭШ И ЛОГИ В models/
    )

    model.fit(
        X_tr, y_tr_delta,
        eval_set=(X_val, y_val_delta),
        early_stopping_rounds=50,
        use_best_model=True
    )

    # Сохраняем готовую модель в папку models
    model.save_model(str(PATHS["model_save_path"]))
    print(f"Модель сохранена в: {PATHS['model_save_path']}")

    # Итоговый прогноз = текущее отклонение + спрогнозированная дельта
    predicted_delta = model.predict(X_val)
    final_val_preds = X_val["cur_dev_s"] + predicted_delta
    
    model_mae = mean_absolute_error(y_val_full, final_val_preds)
    baseline_mae = mean_absolute_error(y_val_full, X_val["cur_dev_s"])
    
    print(f"\n--- Результаты валидации ---")
    print(f"MAE Baseline (cur_dev_s): {baseline_mae:.2f} s")
    print(f"MAE Model:               {model_mae:.2f} s")
    print(f"Улучшение:               {baseline_mae - model_mae:.2f} s\n")

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

    разделитель ';', колонки sample_id;prediction.
    """
    sub = pd.read_csv(sample_sub_path, sep=";")
    preds = predict(model, test_df)

    sub["prediction"] = preds
    sub = sub[["sample_id", "prediction"]]

    # Строго разделитель ';' согласно README.md
    sub.to_csv(output_path, sep=";", index=False)
    print(f"Сабмит успешно сохранен в {output_path}.")
    print(f"Колонки: {list(sub.columns)}, Разделитель: ';', Строк: {len(sub)}")