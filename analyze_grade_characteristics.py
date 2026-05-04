# -*- coding: utf-8 -*-
"""V5 설계서 기반 OBD-only 경사/외부부하 분석.

중요:
- GPS 위도/경도/고도는 경사 계산에 사용하지 않는다.
- V5 문서의 S4 방식처럼 차량속도/가속도/엔진부하만으로 평지 기대부하 대비
  잔차(power_load_deviation)를 계산한다.
- +15pp 이상 잔차는 "오르막 또는 외부부하 플래그"로 본다.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

import recommendation_pipeline as rp


def json_ready(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): json_ready(v) for k, v in value.items()}
    if isinstance(value, list):
        return [json_ready(v) for v in value]
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating,)):
        as_float = float(value)
        return as_float if math.isfinite(as_float) else None
    if pd.isna(value):
        return None
    return value


def load_obd_timeseries(csv_path: Path) -> tuple[pd.DataFrame, pd.Timestamp]:
    """GPS 관련 컬럼을 제외하고 OBD 센서만 1Hz로 정규화한다."""

    raw = pd.read_csv(csv_path, encoding="utf-8-sig")
    elapsed = rp.parse_time_seconds(raw.iloc[:, 0])
    clock_start = pd.to_datetime(raw.iloc[0, 0], format="%H:%M:%S.%f", errors="coerce")

    signals = pd.DataFrame(
        {
            "speed_kmh": pd.to_numeric(raw.iloc[:, 26], errors="coerce").to_numpy(),
            "rpm": pd.to_numeric(raw.iloc[:, 19], errors="coerce").to_numpy(),
            "engine_load_pct": pd.to_numeric(raw.iloc[:, 4], errors="coerce").to_numpy(),
            "maf_gps": pd.to_numeric(raw.iloc[:, 6], errors="coerce").to_numpy(),
            "fuel_rate_lph": pd.to_numeric(raw.iloc[:, 2], errors="coerce").to_numpy(),
            "distance_km": pd.to_numeric(raw.iloc[:, 21], errors="coerce").to_numpy(),
        },
        index=pd.to_timedelta(elapsed, unit="s"),
    )
    ts = signals.resample("1s").mean()
    for column in ["speed_kmh", "rpm", "engine_load_pct", "maf_gps", "fuel_rate_lph"]:
        ts[column] = (
            ts[column]
            .interpolate(limit=5, limit_area="inside")
            .ffill(limit=2)
            .bfill(limit=2)
        )
    ts["distance_km"] = ts["distance_km"].interpolate(limit_area="inside").ffill().bfill()
    ts["speed_kmh"] = ts["speed_kmh"].clip(lower=0, upper=250)
    ts["speed_mps"] = ts["speed_kmh"] / 3.6
    ts["time_s"] = ts.index.total_seconds().astype(float)
    return ts, clock_start


def clock_label(clock_start: pd.Timestamp, seconds: float) -> str:
    if pd.isna(clock_start):
        return f"+{seconds:.0f}s"
    return (clock_start + pd.to_timedelta(float(seconds), unit="s")).strftime("%H:%M:%S")


def sustained_mask(condition: pd.Series, min_samples: int) -> pd.Series:
    """True 상태가 지정 샘플 수 이상 이어진 구간만 True로 남긴다."""

    run_id = condition.ne(condition.shift(fill_value=False)).cumsum()
    run_len = condition.groupby(run_id).transform("size")
    return condition & (run_len >= min_samples)


def most_common(series: pd.Series) -> Any:
    values = series.dropna()
    if values.empty:
        return None
    mode = values.mode()
    return mode.iloc[0] if not mode.empty else values.iloc[0]


def analyze_obd_grade(ts: pd.DataFrame) -> pd.DataFrame:
    """V5 1/2/3차 구조로 주행상태와 경사/외부부하 클래스를 계산한다."""

    dt = ts["time_s"].diff().fillna(1.0).clip(lower=0.1, upper=3.0)
    smooth_speed = ts["speed_mps"].rolling(3, center=True, min_periods=1).mean()
    accel = (smooth_speed.diff() / dt).fillna(0.0).clip(-8, 8)

    result = ts.copy()
    result["dt_s"] = dt
    result["accel_mps2"] = accel
    result["distance_step_m"] = result["speed_mps"] * dt

    # V5 S4: required_load ≈ 8 + speed × 0.18 + (speed/100)^2 × 15
    result["required_load_pct"] = (
        8.0 + result["speed_kmh"] * 0.18 + (result["speed_kmh"] / 100.0) ** 2 * 15.0
    )
    result["power_load_deviation_pp"] = (
        result["engine_load_pct"] - result["required_load_pct"]
    )

    has_motion_signal = result["speed_kmh"].notna()
    stop_candidate = has_motion_signal & (result["speed_kmh"] < 2.0)
    accel_candidate = has_motion_signal & (result["accel_mps2"] > 0.3)
    decel_candidate = has_motion_signal & (result["accel_mps2"] < -0.3)
    cruise_candidate = (
        has_motion_signal
        & (result["speed_kmh"] >= 2.0)
        & (result["accel_mps2"].abs() <= 0.3)
    )

    stop_state = sustained_mask(stop_candidate, 5)
    accel_state = sustained_mask(accel_candidate, 2)
    decel_state = sustained_mask(decel_candidate, 2)

    result["primary_motion_state"] = "transition"
    result.loc[~has_motion_signal, "primary_motion_state"] = "sensor_unavailable"
    result.loc[cruise_candidate, "primary_motion_state"] = "cruise"
    result.loc[accel_state, "primary_motion_state"] = "acceleration"
    result.loc[decel_state, "primary_motion_state"] = "deceleration"
    result.loc[stop_state, "primary_motion_state"] = "stop"

    result["cruise_speed_band"] = ""
    result.loc[cruise_candidate & (result["speed_kmh"] < 40), "cruise_speed_band"] = (
        "low_cruise"
    )
    result.loc[
        cruise_candidate & (result["speed_kmh"] >= 40) & (result["speed_kmh"] < 80),
        "cruise_speed_band",
    ] = "mid_cruise"
    result.loc[cruise_candidate & (result["speed_kmh"] >= 80), "cruise_speed_band"] = (
        "high_cruise"
    )

    result["v5_segment"] = result["primary_motion_state"]
    cruise_segment = result["cruise_speed_band"] != ""
    result.loc[cruise_segment, "v5_segment"] = result.loc[cruise_segment, "cruise_speed_band"]

    stable_cruise_for_grade = (
        (result["speed_kmh"] >= 2.0)
        & (result["accel_mps2"].abs() <= 0.3)
        & result["engine_load_pct"].notna()
    )
    result["is_analyzable_quasi_cruise"] = sustained_mask(stable_cruise_for_grade, 3)

    dev = result["power_load_deviation_pp"]
    has_load_deviation = (
        (result["speed_kmh"] >= 2.0)
        & result["engine_load_pct"].notna()
        & dev.notna()
    )

    result["load_deviation_class"] = "load_not_available"
    result.loc[has_load_deviation, "load_deviation_class"] = "transition_load"
    result.loc[has_load_deviation & (dev >= 15), "load_deviation_class"] = (
        "strong_uphill_or_external_load"
    )
    result.loc[
        has_load_deviation & (dev >= 10) & (dev < 15),
        "load_deviation_class",
    ] = "mild_uphill_or_external_load"
    result.loc[
        has_load_deviation & (dev <= -8),
        "load_deviation_class",
    ] = "downhill_or_coast_possible"
    result.loc[
        has_load_deviation & (dev.abs() <= 5),
        "load_deviation_class",
    ] = "normal_load"

    result["grade_confidence"] = "not_applicable"
    result.loc[has_load_deviation, "grade_confidence"] = "low"
    result.loc[result["is_analyzable_quasi_cruise"], "grade_confidence"] = "high"

    result["obd_grade_class"] = result["load_deviation_class"]
    result.loc[stop_candidate, "obd_grade_class"] = "stop_or_idle"
    result.loc[~has_motion_signal | result["engine_load_pct"].isna(), "obd_grade_class"] = (
        "sensor_unavailable"
    )

    # V5 근거: 5% 경사에서 평지 대비 약 +15~20pp 부하 증가.
    # 정확한 경사도라기보다 equivalent grade index로만 사용한다.
    result["equivalent_grade_index_pct"] = (
        result["power_load_deviation_pp"] / 3.5
    ).clip(lower=-12, upper=12)
    return result


def summarize(analyzed: pd.DataFrame) -> dict[str, Any]:
    total_seconds = int(len(analyzed))
    moving = analyzed["speed_kmh"] >= 2
    moving_seconds = int(moving.sum())
    moving_distance_m = float(analyzed.loc[moving, "distance_step_m"].sum())
    analyzable = analyzed[analyzed["is_analyzable_quasi_cruise"]].copy()
    mapped = analyzed[analyzed["obd_grade_class"] != "sensor_unavailable"].copy()
    low_confidence = analyzed[analyzed["grade_confidence"] == "low"].copy()

    def build_class_summary(
        frame: pd.DataFrame,
        ratio_suffix: str,
    ) -> dict[str, Any]:
        class_summary: dict[str, Any] = {}
        if frame.empty:
            return class_summary
        grouped = (
            frame.groupby("obd_grade_class")
            .agg(
                seconds=("obd_grade_class", "size"),
                distance_m=("distance_step_m", "sum"),
                speed_avg_kmh=("speed_kmh", "mean"),
                rpm_avg=("rpm", "mean"),
                engine_load_avg_pct=("engine_load_pct", "mean"),
                required_load_avg_pct=("required_load_pct", "mean"),
                deviation_mean_pp=("power_load_deviation_pp", "mean"),
                deviation_median_pp=("power_load_deviation_pp", "median"),
                deviation_p90_pp=("power_load_deviation_pp", lambda s: s.quantile(0.90)),
                equivalent_grade_index_mean_pct=("equivalent_grade_index_pct", "mean"),
                high_confidence_seconds=(
                    "grade_confidence",
                    lambda s: int((s == "high").sum()),
                ),
                low_confidence_seconds=(
                    "grade_confidence",
                    lambda s: int((s == "low").sum()),
                ),
            )
            .reset_index()
        )
        for row in grouped.to_dict(orient="records"):
            row[f"time_ratio_of_{ratio_suffix}"] = row["seconds"] / max(len(frame), 1)
            row[f"distance_ratio_of_{ratio_suffix}"] = row["distance_m"] / max(
                float(frame["distance_step_m"].sum()), 1e-9
            )
            class_summary[row["obd_grade_class"]] = row
        return class_summary

    return {
        "method": "OBD-only V5 3-stage motion + S4 power_load_deviation",
        "gps_used_for_grade": False,
        "total_seconds": total_seconds,
        "moving_seconds": moving_seconds,
        "moving_distance_m_by_obd_speed": moving_distance_m,
        "mapped_grade_seconds": int(len(mapped)),
        "mapped_grade_distance_m": float(mapped["distance_step_m"].sum())
        if not mapped.empty
        else 0.0,
        "analyzable_quasi_cruise_seconds": int(len(analyzable)),
        "analyzable_quasi_cruise_distance_m": float(analyzable["distance_step_m"].sum())
        if not analyzable.empty
        else 0.0,
        "low_confidence_grade_seconds": int(len(low_confidence)),
        "low_confidence_grade_distance_m": float(low_confidence["distance_step_m"].sum())
        if not low_confidence.empty
        else 0.0,
        "mean_power_load_deviation_pp": float(analyzable["power_load_deviation_pp"].mean())
        if not analyzable.empty
        else None,
        "median_power_load_deviation_pp": float(analyzable["power_load_deviation_pp"].median())
        if not analyzable.empty
        else None,
        "p90_power_load_deviation_pp": float(analyzable["power_load_deviation_pp"].quantile(0.90))
        if not analyzable.empty
        else None,
        "class_summary": build_class_summary(analyzable, "analyzable"),
        "map_class_summary": build_class_summary(mapped, "mapped"),
    }


def build_runs(analyzed: pd.DataFrame, clock_start: pd.Timestamp) -> pd.DataFrame:
    """연속된 OBD 부하 클래스를 구간 테이블로 묶는다."""

    rows: list[dict[str, Any]] = []
    if analyzed.empty:
        return pd.DataFrame(rows)

    values = analyzed["obd_grade_class"].tolist()
    start_idx = 0
    for idx in range(1, len(analyzed)):
        if values[idx] != values[idx - 1]:
            rows.append(run_summary(analyzed.iloc[start_idx:idx], values[idx - 1], clock_start))
            start_idx = idx
    rows.append(run_summary(analyzed.iloc[start_idx:], values[-1], clock_start))
    return pd.DataFrame(rows)


def run_summary(part: pd.DataFrame, grade_class: str, clock_start: pd.Timestamp) -> dict[str, Any]:
    start_s = float(part["time_s"].iloc[0])
    end_s = float(part["time_s"].iloc[-1])
    return {
        "class": grade_class,
        "start_clock": clock_label(clock_start, start_s),
        "end_clock": clock_label(clock_start, end_s),
        "start_s": start_s,
        "end_s": end_s,
        "seconds": int(len(part)),
        "distance_m": float(part["distance_step_m"].sum()),
        "speed_avg_kmh": float(part["speed_kmh"].mean()),
        "engine_load_avg_pct": float(part["engine_load_pct"].mean()),
        "required_load_avg_pct": float(part["required_load_pct"].mean()),
        "deviation_mean_pp": float(part["power_load_deviation_pp"].mean()),
        "equivalent_grade_index_mean_pct": float(part["equivalent_grade_index_pct"].mean()),
        "grade_confidence": most_common(part["grade_confidence"])
        if "grade_confidence" in part
        else None,
        "v5_segment": most_common(part["v5_segment"]) if "v5_segment" in part else None,
    }


def write_report(
    output_dir: Path,
    summary: dict[str, Any],
    runs: pd.DataFrame,
) -> Path:
    report_path = output_dir / "grade_analysis_report.md"
    class_summary = summary["class_summary"]
    map_class_summary = summary.get("map_class_summary", {})

    def fmt_pp(value: Any) -> str:
        if value is None or pd.isna(value):
            return "n/a"
        return f"{float(value):.1f}pp"

    def fmt_pct(value: Any) -> str:
        if value is None or pd.isna(value):
            return "n/a"
        return f"{float(value):.1%}"

    def line(table: dict[str, Any], key: str, label: str, ratio_key: str) -> str:
        row = table.get(key)
        if row is None:
            return f"- {label}: 0초, 0m"
        ratio = row.get(ratio_key)
        confidence = ""
        if "high_confidence_seconds" in row or "low_confidence_seconds" in row:
            confidence = (
                f", 고신뢰 {row.get('high_confidence_seconds', 0):.0f}초"
                f"/저신뢰 {row.get('low_confidence_seconds', 0):.0f}초"
            )
        return (
            f"- {label}: {row['seconds']:.0f}초, {row['distance_m']:.0f}m "
            f"({fmt_pct(ratio)}), 평균 편차 {fmt_pp(row['deviation_mean_pp'])}"
            f"{confidence}"
        )

    significant = runs[
        (runs["class"] != "non_analyzed")
        & (runs["seconds"] >= 3)
        & (
            runs["class"].isin(
                ["strong_uphill_or_external_load", "mild_uphill_or_external_load"]
            )
        )
    ].copy()
    if not significant.empty:
        significant = significant.sort_values("deviation_mean_pp", ascending=False).head(8)

    lines = [
        "# V5 3단계 OBD-only 경사/외부부하 분석",
        "",
        "- 사용 데이터: 차량속도, 엔진부하, RPM, MAF, 시간",
        "- 미사용 데이터: GPS 위도, GPS 경도, GPS 고도",
        "- 기준: V5 1차 운동상태 → 2차 순항 세분화 → 3차 S4 부하편차",
        "- 지도 표시는 1차에서 탈락시킨 결과가 아니라, 이동 중 부하편차를 함께 오버레이한 결과이다.",
        "- 고신뢰 경사 판정: 속도 2km/h 이상, abs(a) ≤ 0.3m/s², 3초 이상 준정속",
        "- 저신뢰 부하 참고: 가속/감속/전환 중 부하편차가 큰 구간",
        "",
        f"- 전체 시간: {summary['total_seconds']}초",
        f"- 이동 시간: {summary['moving_seconds']}초",
        f"- 지도 표시 가능 구간: {summary['mapped_grade_seconds']}초, "
        f"{summary['mapped_grade_distance_m']:.0f}m",
        f"- 고신뢰 준정속 구간: {summary['analyzable_quasi_cruise_seconds']}초, "
        f"{summary['analyzable_quasi_cruise_distance_m']:.0f}m",
        f"- 저신뢰 부하 참고 구간: {summary['low_confidence_grade_seconds']}초, "
        f"{summary['low_confidence_grade_distance_m']:.0f}m",
        f"- 고신뢰 준정속 평균 부하 편차: {fmt_pp(summary['mean_power_load_deviation_pp'])}",
        f"- 고신뢰 준정속 중앙 부하 편차: {fmt_pp(summary['median_power_load_deviation_pp'])}",
        f"- 고신뢰 준정속 P90 부하 편차: {fmt_pp(summary['p90_power_load_deviation_pp'])}",
        "",
        "## 지도 표시 부하 클래스 비중",
        line(map_class_summary, "strong_uphill_or_external_load", "강한 오르막/외부부하 플래그", "distance_ratio_of_mapped"),
        line(map_class_summary, "mild_uphill_or_external_load", "약한 오르막/외부부하 플래그", "distance_ratio_of_mapped"),
        line(map_class_summary, "normal_load", "평지 기대부하 근접", "distance_ratio_of_mapped"),
        line(map_class_summary, "downhill_or_coast_possible", "내리막/타행 가능", "distance_ratio_of_mapped"),
        line(map_class_summary, "transition_load", "부하 전환/중간 편차", "distance_ratio_of_mapped"),
        line(map_class_summary, "stop_or_idle", "정차/초저속", "distance_ratio_of_mapped"),
        "",
        "## 고신뢰 준정속 부하 클래스 비중",
        line(class_summary, "strong_uphill_or_external_load", "강한 오르막/외부부하 플래그", "distance_ratio_of_analyzable"),
        line(class_summary, "mild_uphill_or_external_load", "약한 오르막/외부부하 플래그", "distance_ratio_of_analyzable"),
        line(class_summary, "normal_load", "평지 기대부하 근접", "distance_ratio_of_analyzable"),
        line(class_summary, "downhill_or_coast_possible", "내리막/타행 가능", "distance_ratio_of_analyzable"),
        line(class_summary, "transition_load", "부하 전환/중간 편차", "distance_ratio_of_analyzable"),
        "",
        "## 주요 오르막/외부부하 의심 구간",
    ]
    if significant.empty:
        lines.append("- 3초 이상 지속된 오르막/외부부하 플래그 구간 없음")
    else:
        for row in significant.itertuples(index=False):
            lines.append(
                f"- {row.start_clock}~{row.end_clock}: {row.seconds}초, "
                f"{row.distance_m:.0f}m, 평균속도 {row.speed_avg_kmh:.1f}km/h, "
                f"부하편차 {row.deviation_mean_pp:.1f}pp, "
                f"등가 경사 지수 {row.equivalent_grade_index_mean_pct:.1f}%, "
                f"신뢰도 {row.grade_confidence}, V5구간 {row.v5_segment}"
            )

    lines.extend(
        [
            "",
            "## 해석 주의",
            "- 등가 경사 지수는 V5의 `5% 경사 ≈ +15~20pp 부하 증가` 근거를 이용한 부하지수이며, 실제 도로 경사도 실측값이 아니다.",
            "- 저속/저단/냉간/에어컨/노면/적재/차량별 엔진 특성도 엔진부하 편차를 올릴 수 있으므로, 결과는 `오르막 또는 외부부하`로 표현한다.",
            "- 내리막은 엔진부하만으로는 오르막보다 약하게 추정된다. 낮은 부하 편차는 타행/감속/내리막 가능성으로만 해석한다.",
            "",
        ]
    )
    report_path.write_text("\n".join(lines), encoding="utf-8")
    return report_path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="V5 OBD-only 경사/외부부하 분석을 실행합니다.")
    parser.add_argument("--drive-csv", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)

    ts, clock_start = load_obd_timeseries(args.drive_csv)
    analyzed = analyze_obd_grade(ts)
    summary = summarize(analyzed)
    runs = build_runs(analyzed, clock_start)

    analyzed.to_csv(args.output_dir / "obd_grade_timeseries.csv", index=False, encoding="utf-8-sig")
    runs.to_csv(args.output_dir / "obd_grade_segments.csv", index=False, encoding="utf-8-sig")
    (args.output_dir / "grade_analysis.json").write_text(
        json.dumps(json_ready(summary), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    report_path = write_report(args.output_dir, summary, runs)
    print(json.dumps(json_ready(summary), ensure_ascii=False, indent=2))
    print(f"report={report_path.resolve()}")


if __name__ == "__main__":
    main()
