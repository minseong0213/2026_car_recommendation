# -*- coding: utf-8 -*-
"""V5 설계서 기반 파워트레인 효율 시뮬레이션 차량 추천 파이프라인.

기본 입력은 사용자가 제공한 3개 파일이다.
- V5 기술설계서: 수식/파이프라인 기준
- 차량 DB xlsx: 현대/기아/제네시스 차종 후보
- 실제 주행 CSV: 추천 테스트용 OBD 로그
"""

from __future__ import annotations

import argparse
import json
import math
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd


# =============================================================================
# 0. 기본 경로/상수 설정
# =============================================================================

DEFAULT_VEHICLE_DB = Path(
    r"C:\Users\minseong\Downloads\현대기아_제네시스_파워트레인별_차량DB_v2.xlsx"
)
DEFAULT_DRIVE_CSV = Path(
    r"C:\Users\minseong\Documents\카카오톡 받은 파일\2026-04-30 23-44-13.csv"
)
DEFAULT_OUTPUT_DIR = Path("outputs")


# V5 6.5절의 연료 단가를 기본값으로 둔다.
# 경유/LPG/수소는 V5 표에 없으므로 DB 전체 커버리지를 위한 보수적 기본값이다.
FUEL_PRICE_KRW = {
    "휘발유": 1650.0,
    "경유": 1550.0,
    "LPG": 1050.0,
    "전기": 324.0,  # 원/kWh
    "휘발유+전기": 1650.0,
    "수소": 9900.0,
}


# CSV 컬럼명은 로거 앱 출력 그대로 사용한다.
CSV_COL = {
    "time": "time",
    "obd_speed": "차량 속도 (km/h)",
    "gps_speed": "속도 (GPS) (km/h)",
    "rpm": "엔진 RPM (rpm)",
    "throttle": "스로틀 위치 (%)",
    "engine_load": "계산된 엔진 부하 (%)",
    "maf": "공기 질량 유량(MAF) (g/sec)",
    "fuel_rate": "계산된 순간 연료 소비율 (L/h)",
    "fuel_used": "사용 연료 (L)",
    "distance": "주행 거리 (km)",
    "lat": "Latitude",
    "lon": "Longtitude",
}

CSV_ALIASES = {
    "time": ["time", "timestamp", "Timestamp", "시간"],
    "obd_speed": ["차량 속도 (km/h)", "obd_speed_kph", "obd_speed_kmh"],
    "gps_speed": ["속도 (GPS) (km/h)", "gps_speed_kph", "gps_speed_kmh"],
    "rpm": ["엔진 RPM (rpm)", "rpm", "RPM"],
    "throttle": ["스로틀 위치 (%)", "throttle_pct", "Throttle Position (%)"],
    "engine_load": ["계산된 엔진 부하 (%)", "engine_load_pct", "Engine Load (%)"],
    "maf": ["공기 질량 유량(MAF) (g/sec)", "maf_gps", "MAF (g/sec)"],
    "fuel_rate": ["계산된 순간 연료 소비율 (L/h)", "fuel_rate_lph"],
    "fuel_used": ["사용 연료 (L)", "사용 연료 (오늘) (L)", "fuel_used_l"],
    "distance": ["주행 거리 (km)", "주행 거리 (오늘) (km)", "distance_km"],
    "lat": ["Latitude", "lat"],
    "lon": ["Longtitude", "Longitude", "lon"],
}


VEHICLE_COL = {
    "brand": "브랜드",
    "model": "모델명",
    "segment": "세그먼트",
    "body": "차체유형",
    "year": "연식",
    "powertrain": "파워트레인유형",
    "hp": "최대출력(hp)",
    "torque": "최대토크(Nm)",
    "combined_eff": "복합연비(km/L)",
    "city_eff": "도심연비(km/L)",
    "highway_eff": "고속연비(km/L)",
    "ev_eff": "전비(km/kWh)",
    "ev_range": "1회충전거리(km)",
    "weight": "공차중량(kg)",
    "fuel": "연료유종",
    "price_min": "최저가(만원)",
    "price_max": "최고가(만원)",
    "passengers": "최대탑승인원",
    "cargo_l": "트렁크용량(L)",
    "acc": "ACC탑재",
    "regen_control": "회생제동조절",
}


# =============================================================================
# 1. V5 Rule-Based 파워트레인 효율/성향 계수
# =============================================================================

# V5 Table 11, Table 15를 코드화한다.
# V5에 없는 LPG/디젤/PHEV/FCEV는 후보 DB를 누락시키지 않기 위한 근사 fallback이다.
POWERTRAIN_RULES: dict[str, dict[str, float | str]] = {
    "가솔린NA": {
        "display": "가솔린 NA",
        "stop_go": 0.55,
        "low": 0.70,
        "mid": 0.95,
        "high": 1.00,
        "extra": 0.85,
        "regen": 0.00,
        "accel_penalty_coeff": 0.88,
        "sport_fit": 0.30,
        "comfort_fit": 0.80,
    },
    "가솔린터보": {
        "display": "가솔린 터보",
        "stop_go": 0.50,
        "low": 0.65,
        "mid": 1.00,
        "high": 1.05,
        "extra": 0.80,
        "regen": 0.00,
        "accel_penalty_coeff": 0.75,
        "sport_fit": 0.75,
        "comfort_fit": 0.50,
    },
    "병렬HEV": {
        "display": "병렬 HEV",
        "stop_go": 0.95,
        "low": 1.10,
        "mid": 1.15,
        "high": 1.10,
        "extra": 0.90,
        "regen": 0.35,
        "accel_penalty_coeff": 0.85,
        "sport_fit": 0.60,
        "comfort_fit": 0.90,
    },
    "BEV": {
        "display": "BEV",
        "stop_go": 1.20,
        "low": 1.15,
        "mid": 1.00,
        "high": 0.75,
        "extra": 0.55,
        "regen": 0.45,
        "accel_penalty_coeff": 0.92,
        "sport_fit": 0.85,
        "comfort_fit": 0.70,
    },
    "LPG": {
        "display": "LPG",
        "stop_go": 0.52,
        "low": 0.68,
        "mid": 0.92,
        "high": 0.96,
        "extra": 0.82,
        "regen": 0.00,
        "accel_penalty_coeff": 0.86,
        "sport_fit": 0.25,
        "comfort_fit": 0.78,
    },
    "디젤": {
        "display": "디젤",
        "stop_go": 0.60,
        "low": 0.75,
        "mid": 1.05,
        "high": 1.12,
        "extra": 1.00,
        "regen": 0.00,
        "accel_penalty_coeff": 0.82,
        "sport_fit": 0.55,
        "comfort_fit": 0.65,
    },
    "PHEV": {
        "display": "PHEV",
        "stop_go": 1.05,
        "low": 1.12,
        "mid": 1.12,
        "high": 1.05,
        "extra": 0.85,
        "regen": 0.40,
        "accel_penalty_coeff": 0.88,
        "sport_fit": 0.65,
        "comfort_fit": 0.85,
    },
    "FCEV": {
        "display": "FCEV",
        "stop_go": 1.05,
        "low": 1.05,
        "mid": 1.00,
        "high": 0.90,
        "extra": 0.75,
        "regen": 0.35,
        "accel_penalty_coeff": 0.92,
        "sport_fit": 0.75,
        "comfort_fit": 0.75,
    },
}


HIGH_CRUISE_FIT = {
    "가솔린NA": 6.0,
    "가솔린터보": 7.0,
    "병렬HEV": 8.0,
    "BEV": 3.0,
    "LPG": 5.0,
    "디젤": 8.0,
    "PHEV": 7.0,
    "FCEV": 5.0,
}

LOW_SPEED_FIT = {
    "가솔린NA": 6.0,
    "가솔린터보": 5.0,
    "병렬HEV": 8.0,
    "BEV": 9.0,
    "LPG": 6.0,
    "디젤": 5.0,
    "PHEV": 8.0,
    "FCEV": 7.0,
}


# =============================================================================
# 2. 사용자 입력/필터 조건
# =============================================================================


@dataclass
class UserConstraints:
    """추천 파이프라인 Step 1~2에 쓰는 사용자 조건.

    사용자가 값을 주지 않으면 테스트에서는 차량 DB 전체를 대상으로 점수화한다.
    """

    passengers: int | None = None
    cargo_need: str | None = None
    budget_min_10k_krw: float | None = None
    budget_max_10k_krw: float | None = None
    monthly_distance_km: float | None = None
    apply_body_filter: bool = False


# =============================================================================
# 3. 공통 유틸리티
# =============================================================================


def finite_float(value: Any, default: float = 0.0) -> float:
    """NaN/inf를 안전한 float로 바꾼다."""

    try:
        value = float(value)
    except (TypeError, ValueError):
        return default
    return value if math.isfinite(value) else default


def clip(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def json_ready(obj: Any) -> Any:
    """numpy/pandas 타입을 JSON 직렬화 가능한 기본 타입으로 변환한다."""

    if isinstance(obj, dict):
        return {str(k): json_ready(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [json_ready(v) for v in obj]
    if isinstance(obj, tuple):
        return [json_ready(v) for v in obj]
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (np.floating,)):
        value = float(obj)
        return value if math.isfinite(value) else None
    if isinstance(obj, (pd.Timestamp, pd.Timedelta)):
        return str(obj)
    if pd.isna(obj):
        return None
    return obj


def numeric_series(df: pd.DataFrame, column: str) -> pd.Series:
    """컬럼이 없을 때도 동일 인덱스의 NaN Series를 돌려준다."""

    if column not in df.columns:
        return pd.Series(np.nan, index=df.index, dtype="float64")
    return pd.to_numeric(df[column], errors="coerce")


def resolve_csv_columns(df: pd.DataFrame) -> dict[str, str | None]:
    """지원하는 로거 CSV 포맷에서 표준 의미별 실제 컬럼명을 찾는다."""

    resolved: dict[str, str | None] = {}
    for key, default_name in CSV_COL.items():
        candidates = [default_name, *CSV_ALIASES.get(key, [])]
        resolved[key] = next((name for name in candidates if name in df.columns), None)
    return resolved


def numeric_signal(df: pd.DataFrame, columns: dict[str, str | None], key: str) -> pd.Series:
    column = columns.get(key)
    if column is None:
        return pd.Series(np.nan, index=df.index, dtype="float64")
    return numeric_series(df, column)


def mark_runs(mask: pd.Series, min_len: int) -> pd.Series:
    """True가 min_len 샘플 이상 연속된 구간만 True로 인정한다."""

    clean = mask.fillna(False).astype(bool)
    group_id = clean.ne(clean.shift(fill_value=False)).cumsum()
    run_len = clean.groupby(group_id).transform("size")
    return clean & (run_len >= min_len)


def rising_edge_count(mask: pd.Series) -> int:
    """False→True 전환 횟수를 이벤트 수로 계산한다."""

    clean = mask.fillna(False).astype(bool)
    return int((clean & ~clean.shift(fill_value=False)).sum())


def positive_range(series: pd.Series) -> float:
    """누적형 센서의 max-min 범위를 반환한다."""

    clean = pd.to_numeric(series, errors="coerce").dropna()
    if clean.empty:
        return 0.0
    return finite_float(clean.max() - clean.min())


# =============================================================================
# 4. 실제 주행 CSV 로딩/1Hz 정규화
# =============================================================================


def parse_time_seconds(time_col: pd.Series) -> pd.Series:
    """로거 시간을 경과초로 변환한다.

    기존 HH:MM:SS.sss 포맷과 신규 ISO timestamp 포맷을 모두 처리한다.
    """

    text = time_col.astype(str)
    parsed = pd.to_datetime(text, format="%H:%M:%S.%f", errors="coerce")
    fallback = pd.to_datetime(text, format="%H:%M:%S", errors="coerce")
    generic = pd.to_datetime(text, errors="coerce")
    parsed = parsed.fillna(fallback).fillna(generic)
    has_date = text.str.contains(r"\d{4}-\d{2}-\d{2}|T", regex=True, na=False)
    if has_date.any():
        first_valid = parsed.dropna().iloc[0]
        return (parsed - first_valid).dt.total_seconds()

    seconds = (
        parsed.dt.hour * 3600
        + parsed.dt.minute * 60
        + parsed.dt.second
        + parsed.dt.microsecond / 1_000_000
    )
    day_offset = (seconds.diff() < -12 * 3600).cumsum() * 86400
    elapsed = seconds + day_offset
    first_valid = elapsed.dropna().iloc[0]
    return elapsed - first_valid


def load_drive_timeseries(csv_path: Path) -> tuple[pd.DataFrame, dict[str, Any]]:
    """원시 CSV를 센서 융합 후 1Hz 시계열로 만든다."""

    raw = pd.read_csv(csv_path, encoding="utf-8-sig")
    csv_columns = resolve_csv_columns(raw)
    if csv_columns["time"] is None:
        raise KeyError(
            "시간 컬럼을 찾지 못했습니다. 지원 컬럼: "
            + ", ".join(CSV_ALIASES["time"])
        )

    raw["elapsed_s"] = parse_time_seconds(raw[csv_columns["time"]])
    raw = raw.dropna(subset=["elapsed_s"]).sort_values("elapsed_s")
    csv_columns = resolve_csv_columns(raw)

    speed_obd = numeric_signal(raw, csv_columns, "obd_speed")
    speed_gps = numeric_signal(raw, csv_columns, "gps_speed")
    speed = speed_obd.combine_first(speed_gps)

    signals = pd.DataFrame(
        {
            "speed_kmh": speed.to_numpy(),
            "rpm": numeric_signal(raw, csv_columns, "rpm").to_numpy(),
            "throttle_pct": numeric_signal(raw, csv_columns, "throttle").to_numpy(),
            "engine_load_pct": numeric_signal(raw, csv_columns, "engine_load").to_numpy(),
            "maf_gps": numeric_signal(raw, csv_columns, "maf").to_numpy(),
            "fuel_rate_lph": numeric_signal(raw, csv_columns, "fuel_rate").to_numpy(),
            "fuel_used_l": numeric_signal(raw, csv_columns, "fuel_used").to_numpy(),
            "distance_km": numeric_signal(raw, csv_columns, "distance").to_numpy(),
            "lat": numeric_signal(raw, csv_columns, "lat").to_numpy(),
            "lon": numeric_signal(raw, csv_columns, "lon").to_numpy(),
        },
        index=pd.to_timedelta(raw["elapsed_s"], unit="s"),
    )

    # 센서별 샘플링 주기가 달라 NaN이 교차하므로 1초 평균 후 짧은 공백만 보간한다.
    ts = signals.resample("1s").mean()
    for column in [
        "speed_kmh",
        "rpm",
        "throttle_pct",
        "engine_load_pct",
        "maf_gps",
        "fuel_rate_lph",
        "lat",
        "lon",
    ]:
        ts[column] = (
            ts[column]
            .interpolate(limit=5, limit_area="inside")
            .ffill(limit=2)
            .bfill(limit=2)
        )

    for column in ["fuel_used_l", "distance_km"]:
        ts[column] = ts[column].interpolate(limit_area="inside").ffill().bfill()

    ts["speed_kmh"] = ts["speed_kmh"].clip(lower=0, upper=250)
    ts["speed_mps"] = ts["speed_kmh"] / 3.6
    ts["time_s"] = ts.index.total_seconds().astype(float)

    metadata = {
        "raw_rows": int(len(raw)),
        "normalized_rows_1hz": int(len(ts)),
        "raw_duration_s": finite_float(raw["elapsed_s"].max() - raw["elapsed_s"].min()),
        "csv_path": str(csv_path),
        "csv_columns": {key: value for key, value in csv_columns.items() if value is not None},
    }
    return ts, metadata


# =============================================================================
# 5. V5 주행구간 판별 및 Feature 추출
# =============================================================================


def extract_drive_features(ts: pd.DataFrame) -> dict[str, Any]:
    """V5 Table 9의 F01~F13, S1~S5를 실제 주행 시계열에서 산출한다."""

    if ts.empty:
        raise ValueError("정규화된 주행 시계열이 비어 있습니다.")

    dt = ts["time_s"].diff().fillna(1.0).clip(lower=0.1, upper=3.0)
    duration_s = finite_float(ts["time_s"].iloc[-1] - ts["time_s"].iloc[0] + 1.0)

    smooth_speed_mps = ts["speed_mps"].rolling(3, min_periods=1, center=True).mean()
    accel_mps2 = (smooth_speed_mps.diff() / dt).fillna(0.0).clip(-8, 8)
    ts = ts.copy()
    ts["accel_mps2"] = accel_mps2

    distance_by_sensor = positive_range(ts["distance_km"])
    distance_by_speed = finite_float((ts["speed_mps"] * dt).sum() / 1000)
    distance_km = distance_by_sensor if distance_by_sensor > 0.05 else distance_by_speed

    fuel_by_sensor = positive_range(ts["fuel_used_l"])
    fuel_by_rate = finite_float((ts["fuel_rate_lph"].fillna(0.0) * dt / 3600).sum())
    fuel_used_l = fuel_by_sensor if fuel_by_sensor > 0 else fuel_by_rate

    moving = ts["speed_kmh"] >= 2.0
    moving_time_s = finite_float((moving * dt).sum())
    idle_raw = (ts["speed_kmh"] < 2.0) & (ts["rpm"] > 500)

    # 1차 판별: 정차/가속/감속/순항
    stopped_5s = mark_runs(ts["speed_kmh"] < 2.0, 5)
    accel_2s = mark_runs(ts["accel_mps2"] > 0.3, 2)
    decel_2s = mark_runs(ts["accel_mps2"] < -0.3, 2)
    cruise = (ts["speed_kmh"] >= 2.0) & (ts["accel_mps2"].abs() <= 0.3)

    # 2차 판별: 순항 속도대 세분화
    low_cruise = cruise & (ts["speed_kmh"] < 40)
    mid_cruise = cruise & (ts["speed_kmh"] >= 40) & (ts["speed_kmh"] < 80)
    high_cruise = cruise & (ts["speed_kmh"] >= 80)
    extra_high = ts["speed_kmh"] >= 120

    # 정체는 저속 이동이 정차와 60초 이내로 붙어 있는 경우로 근사한다.
    recent_stop = stopped_5s.rolling(61, min_periods=1).max().astype(bool)
    future_stop = stopped_5s.iloc[::-1].rolling(61, min_periods=1).max().iloc[::-1].astype(bool)
    congestion = (ts["speed_kmh"] >= 2) & (ts["speed_kmh"] < 15) & (recent_stop | future_stop)

    stop_go_count = int(
        ((~stopped_5s) & stopped_5s.shift(fill_value=False) & (ts["speed_kmh"] >= 2)).sum()
    )
    stop_go_per_min = stop_go_count / max(duration_s / 60, 1e-9)

    harsh_accel_events = rising_edge_count(ts["accel_mps2"] >= 2.5)
    harsh_brake_events = rising_edge_count(ts["accel_mps2"] <= -3.0)
    decel_events = rising_edge_count(decel_2s)

    harsh_accel_per_100km = harsh_accel_events / max(distance_km, 1e-9) * 100
    harsh_brake_per_100km = harsh_brake_events / max(distance_km, 1e-9) * 100
    decel_events_per_100km = decel_events / max(distance_km, 1e-9) * 100

    # 효율 계산용 속도대 체류 비율은 순항뿐 아니라 전체 이동 시간을 기준으로 잡는다.
    low_speed_ratio = finite_float((((ts["speed_kmh"] >= 2) & (ts["speed_kmh"] < 40)) * dt).sum()) / max(
        moving_time_s, 1e-9
    )
    mid_speed_ratio = finite_float(
        (((ts["speed_kmh"] >= 40) & (ts["speed_kmh"] < 80)) * dt).sum()
    ) / max(moving_time_s, 1e-9)
    high_speed_ratio = finite_float(
        (((ts["speed_kmh"] >= 80) & (ts["speed_kmh"] < 120)) * dt).sum()
    ) / max(moving_time_s, 1e-9)
    extra_speed_ratio = finite_float(((ts["speed_kmh"] >= 120) * dt).sum()) / max(
        moving_time_s, 1e-9
    )

    stop_go_time_s = min(stop_go_count * 10.0, moving_time_s)
    stop_go_ratio_for_eff = max(
        finite_float((congestion * dt).sum()) / max(moving_time_s, 1e-9),
        stop_go_time_s / max(moving_time_s, 1e-9),
    )

    # S1. 스로틀 공격성: 증가 구간의 평균/P95 변화율을 0~1로 정규화한다.
    throttle = ts["throttle_pct"].rolling(3, min_periods=1, center=True).median()
    throttle_rate = (throttle.diff() / dt).replace([np.inf, -np.inf], np.nan)
    positive_throttle_rate = throttle_rate[throttle_rate > 0].dropna()
    throttle_rate_avg = finite_float(positive_throttle_rate.mean())
    throttle_rate_p95 = finite_float(positive_throttle_rate.quantile(0.95))
    throttle_aggressiveness = clip(
        0.45 * (throttle_rate_avg / 12.0) + 0.55 * (throttle_rate_p95 / 30.0),
        0.0,
        1.0,
    )

    # S2. 제동 공격성: 감속 구간의 저크 절대값을 사용한다.
    jerk_mps3 = (ts["accel_mps2"].diff() / dt).replace([np.inf, -np.inf], np.nan)
    brake_jerk = jerk_mps3[(decel_2s | (ts["accel_mps2"] < -0.3)) & (jerk_mps3 < 0)].abs()
    braking_jerk_avg = finite_float(brake_jerk.mean())
    braking_jerk_p95 = finite_float(brake_jerk.quantile(0.95))

    # S3. 가속 강도 분포와 필요 최소 출력 추정.
    positive_accel = ts.loc[ts["accel_mps2"] > 0, "accel_mps2"]
    pos_count = max(len(positive_accel), 1)
    accel_profile = {
        "mild_0_1_5": finite_float(((positive_accel > 0) & (positive_accel < 1.5)).sum() / pos_count),
        "normal_1_5_2_5": finite_float(
            ((positive_accel >= 1.5) & (positive_accel < 2.5)).sum() / pos_count
        ),
        "strong_2_5_4_0": finite_float(
            ((positive_accel >= 2.5) & (positive_accel < 4.0)).sum() / pos_count
        ),
        "aggressive_4_0_plus": finite_float((positive_accel >= 4.0).sum() / pos_count),
    }
    if accel_profile["aggressive_4_0_plus"] >= 0.10 or throttle_rate_p95 > 40:
        min_recommended_hp = 250
    elif accel_profile["strong_2_5_4_0"] >= 0.20 or throttle_rate_p95 > 25:
        min_recommended_hp = 200
    elif throttle_rate_avg > 8:
        min_recommended_hp = 160
    else:
        min_recommended_hp = 120

    # S4. 출력 요구·적재 부하 편차: V5의 grade-free baseline 수식 적용.
    quasi_cruise = (ts["speed_kmh"] >= 2) & (ts["accel_mps2"].abs() <= 0.3)
    required_load = 8 + ts["speed_kmh"] * 0.18 + (ts["speed_kmh"] / 100.0) ** 2 * 15
    load_deviation = (ts["engine_load_pct"] - required_load)[quasi_cruise].dropna()
    if load_deviation.empty:
        power_load_deviation_mean = np.nan
        power_load_deviation_p95 = np.nan
        load_status = "부하 데이터 부족"
    else:
        power_load_deviation_mean = finite_float(load_deviation.mean())
        power_load_deviation_p95 = finite_float(load_deviation.quantile(0.95))

    if load_deviation.empty:
        pass
    elif abs(power_load_deviation_mean) <= 5:
        load_status = "정상 부하"
    elif power_load_deviation_mean >= 10:
        load_status = "적재량 증가/차량 상태 점검 필요"
    elif power_load_deviation_p95 >= 15:
        load_status = "일시 외부 부하 가능"
    else:
        load_status = "관찰 필요"

    # S5. 운전 일관성: 10km/h 속도 bin별 스로틀 CV의 역수.
    consistency_samples: list[float] = []
    consistency_frame = pd.DataFrame(
        {
            "speed_bin": (ts["speed_kmh"] // 10) * 10,
            "throttle": ts["throttle_pct"],
        }
    ).dropna()
    for _, group in consistency_frame.groupby("speed_bin"):
        if len(group) < 5 or group["throttle"].mean() <= 0:
            continue
        cv = group["throttle"].std(ddof=0) / group["throttle"].mean()
        if math.isfinite(cv) and cv > 0:
            consistency_samples.append(cv)
    driving_consistency_index = finite_float(1 / np.mean(consistency_samples)) if consistency_samples else 0.0

    observed_km_per_l = distance_km / fuel_used_l if fuel_used_l > 0 else np.nan
    observed_l_per_100km = fuel_used_l / distance_km * 100 if distance_km > 0 else np.nan

    pke = 0.0
    speed_sq = ts["speed_mps"] ** 2
    positive_delta_v2 = speed_sq.diff().where(speed_sq.diff() > 0, 0.0)
    if distance_km > 0:
        pke = finite_float(positive_delta_v2.sum() / (distance_km * 1000))

    features: dict[str, Any] = {
        # 포괄적 활용 Feature
        "daily_dist_km": distance_km,
        "avg_trip_dist_km": distance_km,
        "monthly_dist_km_est": distance_km * 30.0,
        # 주행구간 Feature
        "duration_s": duration_s,
        "moving_time_s": moving_time_s,
        "moving_ratio": moving_time_s / max(duration_s, 1e-9),
        "avg_speed_kmh": distance_km / max(duration_s / 3600, 1e-9),
        "moving_avg_speed_kmh": distance_km / max(moving_time_s / 3600, 1e-9),
        "low_cruise_ratio": finite_float((low_cruise * dt).sum()) / max(moving_time_s, 1e-9),
        "mid_cruise_ratio": finite_float((mid_cruise * dt).sum()) / max(moving_time_s, 1e-9),
        "high_cruise_ratio": finite_float((high_cruise * dt).sum()) / max(moving_time_s, 1e-9),
        "idle_ratio": finite_float((idle_raw * dt).sum()) / max(duration_s, 1e-9),
        "congestion_ratio": finite_float((congestion * dt).sum()) / max(moving_time_s, 1e-9),
        "stop_go_count": stop_go_count,
        "stop_go_per_min": stop_go_per_min,
        "urban_pattern_ratio": clip(
            (
                finite_float((low_cruise * dt).sum())
                + finite_float((congestion * dt).sum())
                + stop_go_time_s
            )
            / max(moving_time_s, 1e-9),
            0.0,
            1.0,
        ),
        # 운전자 성향 Feature
        "harsh_accel_events": harsh_accel_events,
        "harsh_brake_events": harsh_brake_events,
        "decel_events": decel_events,
        "harsh_accel_per_100km": harsh_accel_per_100km,
        "harsh_brake_per_100km": harsh_brake_per_100km,
        "decel_events_per_100km": decel_events_per_100km,
        "throttle_rate_avg": throttle_rate_avg,
        "throttle_rate_p95": throttle_rate_p95,
        "throttle_aggressiveness": throttle_aggressiveness,
        "braking_jerk_avg": braking_jerk_avg,
        "braking_jerk_p95": braking_jerk_p95,
        "accel_intensity_profile": accel_profile,
        "min_recommended_hp": min_recommended_hp,
        "driving_consistency_index": driving_consistency_index,
        # 적재량/부하 판별
        "power_load_deviation_mean": power_load_deviation_mean,
        "power_load_deviation_p95": power_load_deviation_p95,
        "load_status": load_status,
        # 효율 시뮬레이션 입력용 보조값
        "eff_weight_stop_go": stop_go_ratio_for_eff,
        "eff_weight_low": low_speed_ratio,
        "eff_weight_mid": mid_speed_ratio,
        "eff_weight_high": high_speed_ratio,
        "eff_weight_extra": extra_speed_ratio,
        "pke": pke,
        "fuel_used_l": fuel_used_l,
        "observed_km_per_l": observed_km_per_l,
        "observed_l_per_100km": observed_l_per_100km,
    }
    return features


# =============================================================================
# 6. 차량 DB 로딩 및 Step 1~2 후보 필터
# =============================================================================


def load_vehicle_db(vehicle_db_path: Path) -> pd.DataFrame:
    """첫 번째 시트를 차량 DB로 읽고 문자열 컬럼 공백을 정리한다."""

    xlsx = pd.ExcelFile(vehicle_db_path)
    df = pd.read_excel(vehicle_db_path, sheet_name=xlsx.sheet_names[0])
    df.columns = [str(col).strip() for col in df.columns]
    for column in [VEHICLE_COL["brand"], VEHICLE_COL["model"], VEHICLE_COL["powertrain"], VEHICLE_COL["fuel"]]:
        df[column] = df[column].astype(str).str.strip()
    return df


def body_filter_mask(df: pd.DataFrame, constraints: UserConstraints) -> pd.Series:
    """V5 Step 1 차체 유형 필터를 사용자 입력이 있을 때만 적용한다."""

    mask = pd.Series(True, index=df.index)
    if constraints.passengers is not None:
        mask &= pd.to_numeric(df[VEHICLE_COL["passengers"]], errors="coerce").fillna(0) >= constraints.passengers

    if not constraints.apply_body_filter:
        return mask

    passengers = constraints.passengers or 1
    cargo_need = constraints.cargo_need or "보통"
    segment = df[VEHICLE_COL["segment"]].astype(str)
    body = df[VEHICLE_COL["body"]].astype(str)

    if passengers <= 2 and cargo_need == "낮음":
        allowed = segment.isin(["경차", "소형", "준중형"]) & body.isin(["세단", "해치백", "SUV"])
    elif passengers <= 5 and cargo_need in ["낮음", "보통"]:
        allowed = segment.isin(["소형", "준중형", "중형", "대형"]) & body.isin(["세단", "SUV", "해치백"])
    else:
        allowed = body.isin(["SUV", "MPV", "픽업"]) | segment.isin(["중형", "대형"])
    return mask & allowed


def filter_candidates(df: pd.DataFrame, constraints: UserConstraints) -> pd.DataFrame:
    """V5 Step 1 차체 필터, Step 2 가격 필터, 필수 효율값 존재 여부를 적용한다."""

    mask = body_filter_mask(df, constraints)

    min_price = pd.to_numeric(df[VEHICLE_COL["price_min"]], errors="coerce")
    max_price = pd.to_numeric(df[VEHICLE_COL["price_max"]], errors="coerce")
    if constraints.budget_min_10k_krw is not None:
        mask &= max_price >= constraints.budget_min_10k_krw
    if constraints.budget_max_10k_krw is not None:
        mask &= min_price <= constraints.budget_max_10k_krw

    combined_eff = pd.to_numeric(df[VEHICLE_COL["combined_eff"]], errors="coerce")
    ev_eff = pd.to_numeric(df[VEHICLE_COL["ev_eff"]], errors="coerce")
    mask &= combined_eff.notna() | ev_eff.notna()

    return df.loc[mask].copy()


# =============================================================================
# 7. V5 Step 3 효율 시뮬레이션
# =============================================================================


def weighted_base_efficiency(features: dict[str, Any], rule: dict[str, float | str]) -> float:
    """V5 6.1 baseEffCoeff = Σ(구간비율×효율계수)/Σ(구간비율)."""

    weights = {
        "stop_go": finite_float(features["eff_weight_stop_go"]),
        "low": finite_float(features["eff_weight_low"]),
        "mid": finite_float(features["eff_weight_mid"]),
        "high": finite_float(features["eff_weight_high"]),
        "extra": finite_float(features["eff_weight_extra"]),
    }
    numerator = 0.0
    denominator = 0.0
    for key, weight in weights.items():
        coeff = finite_float(rule.get(key), 0.0)
        if weight <= 0 or coeff <= 0:
            continue
        numerator += weight * coeff
        denominator += weight
    return numerator / denominator if denominator > 0 else 1.0


def final_efficiency_coeff(features: dict[str, Any], rule: dict[str, float | str]) -> dict[str, float]:
    """V5 6.2~6.4 회생 보너스, 급가속 페널티, 최종 효율 계수를 계산한다."""

    base = weighted_base_efficiency(features, rule)
    regen_eff = finite_float(rule.get("regen"), 0.0)
    accel_penalty_coeff = finite_float(rule.get("accel_penalty_coeff"), 1.0)

    brake_frequency = finite_float(features["decel_events_per_100km"]) + finite_float(
        features["stop_go_per_min"]
    ) * 10.0
    regen_bonus = regen_eff * brake_frequency * 0.0005

    accel_penalty = (1.0 - accel_penalty_coeff) * min(
        finite_float(features["harsh_accel_per_100km"]) * 0.001,
        0.15,
    )
    final = max(0.30, base + regen_bonus - accel_penalty)
    return {
        "base_eff_coeff": base,
        "regen_bonus": regen_bonus,
        "accel_penalty": accel_penalty,
        "final_eff_coeff": final,
    }


def rated_efficiency(row: pd.Series) -> tuple[float, str, str]:
    """차량별 공인 효율값과 단위를 반환한다."""

    ev_eff = finite_float(row.get(VEHICLE_COL["ev_eff"]), np.nan)
    combined_eff = finite_float(row.get(VEHICLE_COL["combined_eff"]), np.nan)
    fuel = str(row.get(VEHICLE_COL["fuel"], ""))

    if math.isfinite(ev_eff) and ev_eff > 0 and fuel == "전기":
        return ev_eff, "km/kWh", "electric"
    if math.isfinite(combined_eff) and combined_eff > 0:
        return combined_eff, "km/L", "fuel"
    if math.isfinite(ev_eff) and ev_eff > 0:
        return ev_eff, "km/kWh", "electric"
    return np.nan, "", "missing"


def monthly_cost(
    row: pd.Series,
    simulated_eff: float,
    eff_kind: str,
    monthly_distance_km: float,
) -> float:
    """V5 6.5 monthlyCost 계산. 연료별 단가는 설정값에서 가져온다."""

    if not math.isfinite(simulated_eff) or simulated_eff <= 0:
        return np.nan
    fuel = str(row.get(VEHICLE_COL["fuel"], ""))
    unit_price = FUEL_PRICE_KRW.get(fuel, FUEL_PRICE_KRW["휘발유"])
    if eff_kind == "electric":
        return monthly_distance_km / simulated_eff * FUEL_PRICE_KRW["전기"]
    return monthly_distance_km / simulated_eff * unit_price


# =============================================================================
# 8. V5 Step 4 성향 매칭 및 종합 점수
# =============================================================================


def high_cruise_score(powertrain: str, high_cruise_ratio: float) -> float:
    """V5 Table 14의 고속순항 적합도 점수."""

    if high_cruise_ratio > 0.20:
        return HIGH_CRUISE_FIT.get(powertrain, 5.0)
    return LOW_SPEED_FIT.get(powertrain, 6.0)


def score_vehicle(
    row: pd.Series,
    features: dict[str, Any],
    constraints: UserConstraints,
) -> dict[str, Any]:
    """후보 차량 1대에 대해 효율/비용/성향/고속 적합 점수를 계산한다."""

    powertrain = str(row[VEHICLE_COL["powertrain"]])
    rule = POWERTRAIN_RULES.get(powertrain)
    if rule is None:
        raise ValueError(f"지원하지 않는 파워트레인: {powertrain}")

    monthly_distance_km = (
        constraints.monthly_distance_km
        if constraints.monthly_distance_km is not None
        else finite_float(features["monthly_dist_km_est"])
    )

    coeff = final_efficiency_coeff(features, rule)
    rated_eff, eff_unit, eff_kind = rated_efficiency(row)
    simulated_eff = rated_eff * coeff["final_eff_coeff"] if math.isfinite(rated_eff) else np.nan
    cost = monthly_cost(row, simulated_eff, eff_kind, monthly_distance_km)

    throttle_aggr = finite_float(features["throttle_aggressiveness"])
    regen_eff = finite_float(rule.get("regen"), 0.0)
    regen_frequency = finite_float(features["decel_events_per_100km"]) + finite_float(
        features["stop_go_per_min"]
    ) * 10.0

    efficiency_score = min(35.0, coeff["final_eff_coeff"] * 35.0)
    cost_score = clip((1.0 - finite_float(cost, 300000.0) / 300000.0) * 20.0, 0.0, 20.0)
    regen_score = min(10.0, regen_frequency * regen_eff * 0.5)
    sport_score = finite_float(rule.get("sport_fit"), 0.5) * throttle_aggr * 15.0
    comfort_score = finite_float(rule.get("comfort_fit"), 0.5) * (1.0 - throttle_aggr) * 10.0
    highway_score = high_cruise_score(powertrain, finite_float(features["high_cruise_ratio"]))

    total_score = efficiency_score + cost_score + regen_score + sport_score + comfort_score + highway_score

    max_hp = finite_float(row.get(VEHICLE_COL["hp"]), 0.0)
    power_margin_hp = max_hp - finite_float(features["min_recommended_hp"])
    if power_margin_hp < -40:
        power_note = "출력 여유 부족 가능"
    elif power_margin_hp < 0:
        power_note = "출력 하한 근접"
    else:
        power_note = "출력 여유 충분"

    return {
        "브랜드": row.get(VEHICLE_COL["brand"]),
        "모델명": row.get(VEHICLE_COL["model"]),
        "세그먼트": row.get(VEHICLE_COL["segment"]),
        "차체유형": row.get(VEHICLE_COL["body"]),
        "파워트레인유형": powertrain,
        "연료유종": row.get(VEHICLE_COL["fuel"]),
        "최저가(만원)": row.get(VEHICLE_COL["price_min"]),
        "최고가(만원)": row.get(VEHICLE_COL["price_max"]),
        "정격효율": rated_eff,
        "효율단위": eff_unit,
        "baseEffCoeff": coeff["base_eff_coeff"],
        "regenBonus": coeff["regen_bonus"],
        "accelPenalty": coeff["accel_penalty"],
        "finalEffCoeff": coeff["final_eff_coeff"],
        "예상실효율": simulated_eff,
        "월주행거리(km)": monthly_distance_km,
        "예상월비용(원)": cost,
        "효율점수": efficiency_score,
        "월비용점수": cost_score,
        "회생점수": regen_score,
        "토크응답점수": sport_score,
        "컴포트점수": comfort_score,
        "고속순항점수": highway_score,
        "총점": total_score,
        "최대출력(hp)": row.get(VEHICLE_COL["hp"]),
        "출력판정": power_note,
        "ACC탑재": row.get(VEHICLE_COL["acc"]),
        "회생제동조절": row.get(VEHICLE_COL["regen_control"]),
    }


def recommend_vehicles(
    vehicle_df: pd.DataFrame,
    features: dict[str, Any],
    constraints: UserConstraints,
) -> pd.DataFrame:
    """후보 전체에 점수를 매기고 총점순으로 정렬한다."""

    candidates = filter_candidates(vehicle_df, constraints)
    scored_rows: list[dict[str, Any]] = []
    for _, row in candidates.iterrows():
        powertrain = str(row.get(VEHICLE_COL["powertrain"], ""))
        if powertrain not in POWERTRAIN_RULES:
            continue
        scored = score_vehicle(row, features, constraints)
        if math.isfinite(finite_float(scored["총점"], np.nan)):
            scored_rows.append(scored)

    result = pd.DataFrame(scored_rows)
    if result.empty:
        return result

    result = result.sort_values(["총점", "예상월비용(원)"], ascending=[False, True]).reset_index(drop=True)
    result.insert(0, "순위", np.arange(1, len(result) + 1))
    return result


# =============================================================================
# 9. 테스트 리포트 생성
# =============================================================================


def recommendation_reason(row: pd.Series, features: dict[str, Any]) -> str:
    """상위 추천 차량 카드에 들어갈 짧은 이유를 만든다."""

    ptype = str(row["파워트레인유형"])
    reasons: list[str] = []
    if ptype in ["병렬HEV", "PHEV"] and features["urban_pattern_ratio"] >= 0.35:
        reasons.append("저속/정체 비율에서 하이브리드 효율 계수가 높음")
    if ptype == "BEV":
        if features["high_cruise_ratio"] > 0.20:
            reasons.append("전기요금 이점은 크지만 고속 비율 때문에 효율계수는 보수 적용")
        else:
            reasons.append("정차-출발/저속 패턴에서 BEV 효율 계수가 유리")
    if ptype in ["가솔린터보", "디젤"] and features["high_cruise_ratio"] > 0.20:
        reasons.append("고속 순항 점수에서 내연기관 계열이 유리")
    if row.get("회생제동조절") == "O" and features["decel_events_per_100km"] > 10:
        reasons.append("감속 이벤트가 있어 회생제동 활용 여지가 있음")
    if not reasons:
        reasons.append("효율/월비용/성향 점수의 균형이 좋음")
    return "; ".join(reasons)


def dataframe_to_markdown(df: pd.DataFrame, columns: list[str]) -> str:
    """tabulate 의존성 없이 작은 Markdown 표를 만든다."""

    if df.empty:
        return "추천 후보가 없습니다."

    def fmt(value: Any) -> str:
        if isinstance(value, (float, np.floating)):
            if not math.isfinite(float(value)):
                return ""
            return f"{float(value):.2f}"
        if isinstance(value, (int, np.integer)):
            return str(int(value))
        if pd.isna(value):
            return ""
        return str(value).replace("\n", " ")

    rows = [[fmt(value) for value in row] for row in df[columns].to_numpy()]
    header = [str(column) for column in columns]
    widths = [
        max(len(header[i]), *(len(row[i]) for row in rows))
        for i in range(len(header))
    ]
    header_line = "| " + " | ".join(header[i].ljust(widths[i]) for i in range(len(header))) + " |"
    sep_line = "| " + " | ".join("-" * widths[i] for i in range(len(header))) + " |"
    body_lines = [
        "| " + " | ".join(row[i].ljust(widths[i]) for i in range(len(header))) + " |"
        for row in rows
    ]
    return "\n".join([header_line, sep_line, *body_lines])


def write_report(
    output_dir: Path,
    metadata: dict[str, Any],
    constraints: UserConstraints,
    features: dict[str, Any],
    recommendations: pd.DataFrame,
) -> Path:
    """실제 CSV를 테스트셋으로 돌린 추천 검사 리포트를 Markdown으로 저장한다."""

    output_dir.mkdir(parents=True, exist_ok=True)
    report_path = output_dir / "recommendation_test_report.md"

    top = recommendations.head(10).copy()
    if not top.empty:
        top["추천이유"] = top.apply(lambda row: recommendation_reason(row, features), axis=1)
        top_table = top[
            [
                "순위",
                "브랜드",
                "모델명",
                "파워트레인유형",
                "총점",
                "예상실효율",
                "효율단위",
                "예상월비용(원)",
                "finalEffCoeff",
                "추천이유",
            ]
        ]
        top_table = dataframe_to_markdown(
            top_table,
            [
                "순위",
                "브랜드",
                "모델명",
                "파워트레인유형",
                "총점",
                "예상실효율",
                "효율단위",
                "예상월비용(원)",
                "finalEffCoeff",
                "추천이유",
            ],
        )
    else:
        top_table = "추천 후보가 없습니다."

    if not recommendations.empty:
        top_by_powertrain = (
            recommendations.sort_values("총점", ascending=False)
            .groupby("파워트레인유형", as_index=False)
            .head(1)
            .sort_values("총점", ascending=False)
        )
        top_by_powertrain_table = dataframe_to_markdown(
            top_by_powertrain,
            [
                "순위",
                "브랜드",
                "모델명",
                "파워트레인유형",
                "총점",
                "예상실효율",
                "효율단위",
                "예상월비용(원)",
                "finalEffCoeff",
            ],
        )
    else:
        top_by_powertrain_table = "추천 후보가 없습니다."

    def fmt_num(value: Any, suffix: str = "", precision: int = 1) -> str:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return "n/a"
        if not math.isfinite(number):
            return "n/a"
        return f"{number:.{precision}f}{suffix}"

    feature_lines = [
        f"- 주행거리: {features['daily_dist_km']:.3f} km",
        f"- 주행시간: {features['duration_s'] / 60:.1f} 분, 이동시간 비율: {features['moving_ratio']:.1%}",
        f"- 평균속도: {features['avg_speed_kmh']:.1f} km/h, 이동 평균속도: {features['moving_avg_speed_kmh']:.1f} km/h",
        f"- 저속/중속/고속 순항비율: {features['low_cruise_ratio']:.1%} / {features['mid_cruise_ratio']:.1%} / {features['high_cruise_ratio']:.1%}",
        f"- 정체비율: {features['congestion_ratio']:.1%}, 정차-출발: {features['stop_go_count']}회 ({features['stop_go_per_min']:.2f}회/분)",
        f"- 급가속/급제동: {features['harsh_accel_events']}회 / {features['harsh_brake_events']}회",
        f"- 스로틀 공격성: {features['throttle_aggressiveness']:.2f} (avg {features['throttle_rate_avg']:.2f}%/s, p95 {features['throttle_rate_p95']:.2f}%/s)",
        f"- 부하 판정: {features['load_status']} (평균 편차 {fmt_num(features['power_load_deviation_mean'], 'pp')}, P95 {fmt_num(features['power_load_deviation_p95'], 'pp')})",
        f"- 관측 연비: {features['observed_km_per_l']:.2f} km/L ({features['observed_l_per_100km']:.2f} L/100km)",
    ]

    assumptions = [
        "사용자 예산/탑승/적재 조건을 입력하지 않은 테스트이므로 차량 DB 전체를 후보로 점수화했다.",
        f"월 주행거리는 미입력 상태라 실제 1회 주행거리 × 30일 = {features['monthly_dist_km_est']:.1f} km로 추정했다.",
        "PHEV/LPG/디젤/FCEV 계수는 V5 표에 직접 없으므로 스크립트 내부 fallback 계수를 사용했다.",
        "FCEV처럼 공인 효율값이 없는 레코드는 월비용/효율 산출이 불가능해 추천 후보에서 제외된다.",
        "CSV의 스로틀 위치는 APP 페달값이 아니라 스로틀 밸브 위치일 수 있어 S1 스로틀 공격성 해석에는 주의가 필요하다.",
    ]

    text = "\n".join(
        [
            "# V5 기반 차량 추천 테스트 리포트",
            "",
            "## 입력",
            f"- 실제 주행 CSV: `{metadata['csv_path']}`",
            f"- 원시 행 수: {metadata['raw_rows']:,}행",
            f"- 1Hz 정규화 행 수: {metadata['normalized_rows_1hz']:,}행",
            f"- 사용자 조건: `{json.dumps(json_ready(asdict(constraints)), ensure_ascii=False)}`",
            "",
            "## 실제 주행 Feature 요약",
            *feature_lines,
            "",
            "## 추천 Top 10",
            top_table,
            "",
            "## 파워트레인별 최고 후보",
            top_by_powertrain_table,
            "",
            "## 해석상 가정",
            *[f"- {item}" for item in assumptions],
            "",
        ]
    )
    report_path.write_text(text, encoding="utf-8")
    return report_path


def save_outputs(
    output_dir: Path,
    metadata: dict[str, Any],
    constraints: UserConstraints,
    features: dict[str, Any],
    recommendations: pd.DataFrame,
) -> dict[str, Path]:
    """JSON/CSV/Markdown 결과물을 저장한다."""

    output_dir.mkdir(parents=True, exist_ok=True)
    summary_path = output_dir / "trip_summary.json"
    result_path = output_dir / "vehicle_recommendation_results.csv"

    summary_payload = {
        "metadata": metadata,
        "constraints": asdict(constraints),
        "features": features,
    }
    summary_path.write_text(
        json.dumps(json_ready(summary_payload), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    recommendations.to_csv(result_path, index=False, encoding="utf-8-sig")
    report_path = write_report(output_dir, metadata, constraints, features, recommendations)
    return {
        "summary": summary_path,
        "results": result_path,
        "report": report_path,
    }


def run_recommendation_pipeline(
    drive_csv_path: Path,
    vehicle_db_path: Path = DEFAULT_VEHICLE_DB,
    constraints: UserConstraints | None = None,
    top_n: int = 10,
    output_dir: Path | None = None,
    save_artifacts: bool = False,
) -> dict[str, Any]:
    """API/CLI 공용 추천 실행 함수.

    CSV를 읽어 주행 Feature를 추출하고 차량 DB 전체에 점수를 매긴 뒤,
    JSON 직렬화 가능한 결과를 반환한다. ``save_artifacts``가 True이면 기존 CLI와
    같은 JSON/CSV/Markdown 산출물도 저장한다.
    """

    active_constraints = constraints or UserConstraints()
    ts, metadata = load_drive_timeseries(drive_csv_path)
    features = extract_drive_features(ts)
    vehicle_df = load_vehicle_db(vehicle_db_path)
    recommendations = recommend_vehicles(vehicle_df, features, active_constraints)

    paths: dict[str, Path] = {}
    if save_artifacts:
        artifact_dir = output_dir or DEFAULT_OUTPUT_DIR
        paths = save_outputs(
            artifact_dir,
            metadata,
            active_constraints,
            features,
            recommendations,
        )

    top = recommendations.head(top_n).replace({np.nan: None})
    return {
        "metadata": metadata,
        "constraints": asdict(active_constraints),
        "features": features,
        "total_recommendations": int(len(recommendations)),
        "top_recommendations": top.to_dict(orient="records"),
        "artifacts": {name: str(path.resolve()) for name, path in paths.items()},
    }


# =============================================================================
# 10. CLI 엔트리포인트
# =============================================================================


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="V5 설계서 기반 차량 추천 파이프라인을 실제 주행 CSV로 테스트합니다."
    )
    parser.add_argument("--vehicle-db", type=Path, default=DEFAULT_VEHICLE_DB)
    parser.add_argument("--drive-csv", type=Path, default=DEFAULT_DRIVE_CSV)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--monthly-distance-km", type=float, default=None)
    parser.add_argument("--budget-min", type=float, default=None, help="최소 예산, 만원")
    parser.add_argument("--budget-max", type=float, default=None, help="최대 예산, 만원")
    parser.add_argument("--passengers", type=int, default=None)
    parser.add_argument("--cargo-need", type=str, default=None, help="낮음/보통/높음")
    parser.add_argument("--apply-body-filter", action="store_true")
    parser.add_argument("--top-n", type=int, default=10)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    constraints = UserConstraints(
        passengers=args.passengers,
        cargo_need=args.cargo_need,
        budget_min_10k_krw=args.budget_min,
        budget_max_10k_krw=args.budget_max,
        monthly_distance_km=args.monthly_distance_km,
        apply_body_filter=args.apply_body_filter,
    )

    ts, metadata = load_drive_timeseries(args.drive_csv)
    features = extract_drive_features(ts)
    vehicle_df = load_vehicle_db(args.vehicle_db)
    recommendations = recommend_vehicles(vehicle_df, features, constraints)
    paths = save_outputs(args.output_dir, metadata, constraints, features, recommendations)

    print("\n=== Trip Feature Summary ===")
    compact_features = {
        key: features[key]
        for key in [
            "daily_dist_km",
            "duration_s",
            "avg_speed_kmh",
            "moving_avg_speed_kmh",
            "low_cruise_ratio",
            "mid_cruise_ratio",
            "high_cruise_ratio",
            "urban_pattern_ratio",
            "stop_go_count",
            "stop_go_per_min",
            "throttle_aggressiveness",
            "power_load_deviation_mean",
            "load_status",
            "observed_km_per_l",
        ]
    }
    print(json.dumps(json_ready(compact_features), ensure_ascii=False, indent=2))

    print(f"\n=== Recommendation Top {args.top_n} ===")
    display_cols = [
        "순위",
        "브랜드",
        "모델명",
        "파워트레인유형",
        "총점",
        "예상실효율",
        "효율단위",
        "예상월비용(원)",
        "finalEffCoeff",
    ]
    if recommendations.empty:
        print("추천 후보가 없습니다.")
    else:
        print(
            recommendations.head(args.top_n)[display_cols].to_string(
                index=False,
                formatters={
                    "총점": "{:.2f}".format,
                    "예상실효율": "{:.2f}".format,
                    "예상월비용(원)": "{:,.0f}".format,
                    "finalEffCoeff": "{:.3f}".format,
                },
            )
        )

    print("\n=== Output Files ===")
    for name, path in paths.items():
        print(f"{name}: {path.resolve()}")


if __name__ == "__main__":
    main()
