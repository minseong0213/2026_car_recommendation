# -*- coding: utf-8 -*-
"""CSV의 위도/경도 원시 데이터를 Leaflet HTML 지도로 변환한다."""

from __future__ import annotations

import json
import math
import argparse
from pathlib import Path
from typing import Any

import pandas as pd

import recommendation_pipeline as rp


OUTPUT_DIR = Path("outputs")
MAP_HTML = OUTPUT_DIR / "drive_route_map.html"
GEOJSON_PATH = OUTPUT_DIR / "drive_route.geojson"
PREVIEW_PNG = OUTPUT_DIR / "drive_route_preview.png"


SPEED_COLORS = {
    "stop": "#6B7280",
    "low": "#16A34A",
    "mid": "#F59E0B",
    "high": "#DC2626",
    "unknown": "#2563EB",
}

SPEED_LABELS = {
    "stop": "정차/극저속",
    "low": "저속 2~40km/h",
    "mid": "중속 40~80km/h",
    "high": "고속 80km/h+",
    "unknown": "속도 미상",
}

GRADE_COLORS = {
    "strong_uphill_or_external_load": "#DC2626",
    "mild_uphill_or_external_load": "#F59E0B",
    "normal_load": "#16A34A",
    "downhill_or_coast_possible": "#2563EB",
    "transition_load": "#8B5CF6",
    "non_analyzed": "#9CA3AF",
    "unknown": "#6B7280",
}

GRADE_LABELS = {
    "strong_uphill_or_external_load": "강한 오르막/외부부하",
    "mild_uphill_or_external_load": "약한 오르막/외부부하",
    "normal_load": "평지 기대부하 근접",
    "downhill_or_coast_possible": "내리막/타행 가능",
    "transition_load": "전환 부하",
    "non_analyzed": "분석 제외",
    "unknown": "미상",
}


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """두 GPS 좌표 사이의 대권거리(km)를 계산한다."""

    radius_km = 6371.0088
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = (
        math.sin(dphi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    )
    return 2 * radius_km * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def speed_band(speed_kmh: float | None) -> str:
    """지도 색상용 속도 구간을 판정한다."""

    if speed_kmh is None or not math.isfinite(float(speed_kmh)):
        return "unknown"
    if speed_kmh < 2:
        return "stop"
    if speed_kmh < 40:
        return "low"
    if speed_kmh < 80:
        return "mid"
    return "high"


def js_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def load_route_points(csv_path: Path) -> pd.DataFrame:
    """원시 CSV에서 시간, 좌표, 차량 속도를 정리한다."""

    raw = pd.read_csv(csv_path, encoding="utf-8-sig")
    elapsed_s = rp.parse_time_seconds(raw[rp.CSV_COL["time"]])

    route = pd.DataFrame(
        {
            "elapsed_s": elapsed_s,
            "time": raw[rp.CSV_COL["time"]],
            "lat": pd.to_numeric(raw[rp.CSV_COL["lat"]], errors="coerce"),
            "lon": pd.to_numeric(raw[rp.CSV_COL["lon"]], errors="coerce"),
            "speed_kmh": pd.to_numeric(raw[rp.CSV_COL["obd_speed"]], errors="coerce"),
        }
    )
    route = route.dropna(subset=["elapsed_s", "lat", "lon"]).sort_values("elapsed_s")
    route = route[
        route["lat"].between(-90, 90)
        & route["lon"].between(-180, 180)
        & ~((route["lat"] == 0) & (route["lon"] == 0))
    ].copy()

    # 속도는 좌표보다 낮은 주기로 찍히므로 짧은 공백은 보간/전후값으로 메운다.
    route["speed_kmh"] = (
        route["speed_kmh"].interpolate(limit=10, limit_area="inside").ffill().bfill()
    )
    route["band"] = route["speed_kmh"].apply(speed_band)

    # 동일 좌표가 매우 촘촘하게 반복되면 HTML이 무거워지므로 시간 1초 단위로 대표점을 둔다.
    route["second"] = route["elapsed_s"].round().astype(int)
    route = (
        route.groupby("second", as_index=False)
        .agg(
            elapsed_s=("elapsed_s", "first"),
            time=("time", "first"),
            lat=("lat", "mean"),
            lon=("lon", "mean"),
            speed_kmh=("speed_kmh", "mean"),
            band=("band", lambda values: values.mode().iloc[0] if not values.mode().empty else "unknown"),
        )
        .sort_values("elapsed_s")
    )
    route["band"] = route["speed_kmh"].apply(speed_band)
    return route


def load_grade_timeseries(grade_path: Path) -> pd.DataFrame:
    """OBD-only 경사 분석 시계열을 1초 단위로 읽는다."""

    grade = pd.read_csv(grade_path, encoding="utf-8-sig")
    grade["second"] = pd.to_numeric(grade["time_s"], errors="coerce").round().astype("Int64")
    keep_cols = [
        "second",
        "obd_grade_class",
        "power_load_deviation_pp",
        "equivalent_grade_index_pct",
        "is_analyzable_quasi_cruise",
    ]
    grade = grade.dropna(subset=["second"])[keep_cols].copy()
    grade["second"] = grade["second"].astype(int)
    grade["obd_grade_class"] = grade["obd_grade_class"].fillna("non_analyzed")
    return grade


def apply_grade_overlay(route: pd.DataFrame, grade: pd.DataFrame) -> pd.DataFrame:
    """시간초(second)를 기준으로 GPS 경로 포인트에 OBD 경사 클래스를 붙인다."""

    merged = route.merge(grade, on="second", how="left")
    merged["obd_grade_class"] = merged["obd_grade_class"].fillna("non_analyzed")
    merged["map_band"] = merged["obd_grade_class"]
    return merged


def clean_route_spikes(route: pd.DataFrame, spike_m: float = 50.0) -> pd.DataFrame:
    """1초 단위 GPS 단발 튐을 제거한다.

    GPS는 지도 표시용일 뿐이지만, 단발 튐이 있으면 선이 대각선으로 크게 꺾여
    경사 오버레이 해석을 방해한다.
    """

    if len(route) < 3:
        return route

    keep = [True] * len(route)
    rows = list(route.itertuples(index=False))
    for idx in range(1, len(rows) - 1):
        prev, cur, nxt = rows[idx - 1], rows[idx], rows[idx + 1]
        prev_cur_m = haversine_km(float(prev.lat), float(prev.lon), float(cur.lat), float(cur.lon)) * 1000
        cur_next_m = haversine_km(float(cur.lat), float(cur.lon), float(nxt.lat), float(nxt.lon)) * 1000
        prev_next_m = haversine_km(float(prev.lat), float(prev.lon), float(nxt.lat), float(nxt.lon)) * 1000
        if prev_cur_m > spike_m and cur_next_m > spike_m and prev_next_m < spike_m:
            keep[idx] = False

    return route.loc[keep].reset_index(drop=True)


def build_segments(route: pd.DataFrame, band_col: str = "band") -> list[dict[str, Any]]:
    """같은 표시 구간이 연속된 좌표를 하나의 polyline segment로 묶는다."""

    segments: list[dict[str, Any]] = []
    if route.empty:
        return segments

    current_band = route.iloc[0][band_col]
    current_points: list[list[float]] = []

    previous = None
    for _, row in route.iterrows():
        point = [round(float(row["lat"]), 7), round(float(row["lon"]), 7)]
        if previous is not None:
            # GPS 튐을 방지한다. 이 데이터에서는 거의 발생하지 않지만 지도 품질 안전장치다.
            jump_km = haversine_km(
                float(previous["lat"]),
                float(previous["lon"]),
                float(row["lat"]),
                float(row["lon"]),
            )
            if jump_km > 0.05:
                if len(current_points) >= 2:
                    segments.append({"band": current_band, "points": current_points})
                current_points = [point]
                current_band = row[band_col]
                previous = row
                continue

        if row[band_col] != current_band and len(current_points) >= 2:
            segments.append({"band": current_band, "points": current_points})
            current_points = [current_points[-1], point]
            current_band = row[band_col]
        else:
            current_points.append(point)
        previous = row

    if len(current_points) >= 2:
        segments.append({"band": current_band, "points": current_points})
    return segments


def write_geojson(route: pd.DataFrame, output_path: Path) -> None:
    """경로를 다른 지도 도구에서도 열 수 있게 GeoJSON으로 저장한다."""

    coordinates = [[float(row.lon), float(row.lat)] for row in route.itertuples(index=False)]
    feature = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {"name": "2026-04-30 drive route"},
                "geometry": {"type": "LineString", "coordinates": coordinates},
            }
        ],
    }
    output_path.write_text(json.dumps(feature, ensure_ascii=False, indent=2), encoding="utf-8")


def write_static_preview(
    route: pd.DataFrame,
    output_path: Path,
    colors: dict[str, str],
    labels: dict[str, str],
    legend_order: list[str],
    title: str,
    subtitle: str,
) -> None:
    """외부 지도 타일 없이 경로 형태를 빠르게 볼 수 있는 PNG를 만든다."""

    from PIL import Image, ImageDraw, ImageFont

    width, height, pad = 1400, 1400, 100
    image = Image.new("RGB", (width, height), "#F8FAFC")
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default()

    mid_lat = math.radians(float(route["lat"].mean()))
    x_values = (route["lon"] - route["lon"].min()) * math.cos(mid_lat)
    y_values = route["lat"] - route["lat"].min()
    x_min, x_max = float(x_values.min()), float(x_values.max())
    y_min, y_max = float(y_values.min()), float(y_values.max())
    x_span = max(x_max - x_min, 1e-9)
    y_span = max(y_max - y_min, 1e-9)
    scale = min((width - 2 * pad) / x_span, (height - 2 * pad) / y_span)

    def xy(row: Any) -> tuple[int, int]:
        x = (float(row.lon) - float(route["lon"].min())) * math.cos(mid_lat)
        y = float(row.lat) - float(route["lat"].min())
        px = int((x - x_min) * scale + pad)
        py = int(height - ((y - y_min) * scale + pad))
        return px, py

    for i in range(6):
        x = pad + i * (width - 2 * pad) // 5
        y = pad + i * (height - 2 * pad) // 5
        draw.line([(x, pad), (x, height - pad)], fill="#E2E8F0", width=1)
        draw.line([(pad, y), (width - pad, y)], fill="#E2E8F0", width=1)

    previous = None
    for row in route.itertuples(index=False):
        if previous is not None:
            band = str(getattr(row, "map_band", getattr(row, "band", "unknown")))
            draw.line(
                [xy(previous), xy(row)],
                fill=colors.get(band, "#2563EB"),
                width=7 if band in ["high", "strong_uphill_or_external_load"] else 6,
            )
        previous = row

    start_xy = xy(route.iloc[0])
    end_xy = xy(route.iloc[-1])
    draw.ellipse(
        [start_xy[0] - 12, start_xy[1] - 12, start_xy[0] + 12, start_xy[1] + 12],
        fill="#22C55E",
        outline="#111827",
        width=3,
    )
    draw.ellipse(
        [end_xy[0] - 12, end_xy[1] - 12, end_xy[0] + 12, end_xy[1] + 12],
        fill="#EF4444",
        outline="#111827",
        width=3,
    )

    draw.text((40, 34), title, fill="#111827", font=font)
    static_labels = {
        "strong_uphill_or_external_load": "strong uphill/load",
        "mild_uphill_or_external_load": "mild uphill/load",
        "normal_load": "normal load",
        "downhill_or_coast_possible": "downhill/coast",
        "transition_load": "transition",
        "non_analyzed": "not analyzed",
        "stop": "stop",
        "low": "low 2-40km/h",
        "mid": "mid 40-80km/h",
        "high": "high 80km/h+",
    }
    static_title = (
        "OBD-only grade/load route map"
        if "strong_uphill_or_external_load" in colors
        else "Drive Route Preview"
    )
    static_subtitle = (
        "GPS draws position only; grade uses speed + engine load"
        if "strong_uphill_or_external_load" in colors
        else "Static preview without external map tiles"
    )
    draw.rectangle([36, 28, 520, 82], fill="#F8FAFC")
    draw.text((40, 34), static_title, fill="#111827", font=font)
    draw.text((40, 58), static_subtitle, fill="#475569", font=font)

    legend_x, legend_y = 40, height - 220
    for idx, band in enumerate(legend_order):
        y = legend_y + idx * 30
        draw.line([(legend_x, y + 8), (legend_x + 42, y + 8)], fill=colors.get(band, "#2563EB"), width=8)
        draw.text((legend_x + 54, y), static_labels.get(band, labels.get(band, band)), fill="#111827", font=font)

    image.save(output_path)


def write_map_html(
    route: pd.DataFrame,
    segments: list[dict[str, Any]],
    output_path: Path,
    colors: dict[str, str],
    labels: dict[str, str],
    legend_order: list[str],
    title: str,
    subtitle: str,
) -> None:
    """Leaflet 기반 인터랙티브 HTML 지도를 저장한다."""

    start = route.iloc[0]
    end = route.iloc[-1]
    total_gps_km = 0.0
    for prev, cur in zip(route.itertuples(index=False), route.iloc[1:].itertuples(index=False)):
        total_gps_km += haversine_km(float(prev.lat), float(prev.lon), float(cur.lat), float(cur.lon))

    summary = {
        "points": int(len(route)),
        "start_time": str(start.time),
        "end_time": str(end.time),
        "start": [round(float(start.lat), 7), round(float(start.lon), 7)],
        "end": [round(float(end.lat), 7), round(float(end.lon), 7)],
        "gps_distance_km": round(total_gps_km, 3),
        "avg_speed_kmh": round(float(route["speed_kmh"].mean()), 1),
    }

    band_col = "map_band" if "map_band" in route.columns else "band"
    band_counts = route[band_col].value_counts().to_dict()
    legend_html = "\n".join(
        [
            f'      <span><i class="swatch" style="background:{colors.get(band, "#2563EB")}"></i>{labels.get(band, band)}</span>'
            for band in legend_order
        ]
    )
    count_html = "\n".join(
        [
            f'      <div>{labels.get(str(band), str(band))}: <b>{count}초</b></div>'
            for band, count in band_counts.items()
        ]
    )

    html = f"""<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{title}</title>
  <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" />
  <style>
    html, body {{ height: 100%; margin: 0; font-family: Arial, sans-serif; }}
    #map {{ height: 100%; width: 100%; }}
    .panel {{
      position: absolute;
      z-index: 900;
      top: 16px;
      right: 16px;
      width: 340px;
      background: rgba(255,255,255,0.94);
      border: 1px solid #D1D5DB;
      border-radius: 8px;
      padding: 12px 14px;
      box-shadow: 0 8px 24px rgba(0,0,0,0.16);
      font-size: 13px;
      line-height: 1.45;
    }}
    .panel h1 {{ font-size: 16px; margin: 0 0 8px; }}
    .legend {{ display: grid; gap: 5px; margin-top: 8px; }}
    .legend span {{ display: inline-flex; align-items: center; gap: 7px; }}
    .swatch {{ width: 18px; height: 4px; display: inline-block; border-radius: 99px; }}
    @media (max-width: 720px) {{
      .panel {{ left: 12px; right: 12px; top: 12px; width: auto; }}
    }}
  </style>
</head>
<body>
  <div id="map"></div>
  <section class="panel">
    <h1>{title}</h1>
    <div>{subtitle}</div>
    <div>시작: <b>{summary["start_time"]}</b></div>
    <div>종료: <b>{summary["end_time"]}</b></div>
    <div>GPS 경로거리: <b>{summary["gps_distance_km"]} km</b></div>
    <div>지도 포인트: <b>{summary["points"]:,}개</b></div>
    <div class="legend">
{legend_html}
    </div>
    <div style="margin-top:10px">
{count_html}
    </div>
  </section>
  <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
  <script>
    const segments = {js_json(segments)};
    const colors = {js_json(colors)};
    const labels = {js_json(labels)};
    const summary = {js_json(summary)};
    const bandCounts = {js_json(band_counts)};

    const map = L.map('map', {{ preferCanvas: true }});
    L.tileLayer('https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png', {{
      maxZoom: 19,
      attribution: '&copy; OpenStreetMap contributors'
    }}).addTo(map);

    const allPoints = [];
    segments.forEach((segment) => {{
      if (segment.points.length < 2) return;
      segment.points.forEach((point) => allPoints.push(point));
      L.polyline(segment.points, {{
        color: colors[segment.band] || colors.unknown,
        weight: ['high', 'strong_uphill_or_external_load'].includes(segment.band) ? 6 : 5,
        opacity: 0.86,
        lineCap: 'round',
        lineJoin: 'round'
      }}).addTo(map).bindPopup(labels[segment.band] || segment.band);
    }});

    const bounds = L.latLngBounds(allPoints);
    map.fitBounds(bounds, {{ padding: [30, 30] }});

    L.marker(summary.start).addTo(map).bindPopup(`출발<br>${{summary.start_time}}`);
    L.marker(summary.end).addTo(map).bindPopup(`도착<br>${{summary.end_time}}`);
    L.circleMarker(summary.start, {{
      radius: 7,
      color: '#111827',
      fillColor: '#22C55E',
      fillOpacity: 1
    }}).addTo(map);
    L.circleMarker(summary.end, {{
      radius: 7,
      color: '#111827',
      fillColor: '#EF4444',
      fillOpacity: 1
    }}).addTo(map);
  </script>
</body>
</html>
"""
    output_path.write_text(html, encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="CSV 위도/경도를 지도 산출물로 변환합니다.")
    parser.add_argument("--drive-csv", type=Path, default=rp.DEFAULT_DRIVE_CSV)
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR)
    parser.add_argument(
        "--grade-timeseries",
        type=Path,
        default=None,
        help="OBD-only 경사 분석 시계열 CSV. 지정하면 경사/내리막 색상으로 지도 표시",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    output_dir = args.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    map_html = output_dir / "drive_route_map.html"
    geojson_path = output_dir / "drive_route.geojson"
    preview_png = output_dir / "drive_route_preview.png"

    route = load_route_points(args.drive_csv)
    if route.empty:
        raise RuntimeError("CSV에서 유효한 위도/경도 좌표를 찾지 못했습니다.")
    route = clean_route_spikes(route)
    if args.grade_timeseries is not None:
        grade = load_grade_timeseries(args.grade_timeseries)
        route = apply_grade_overlay(route, grade)
        colors = GRADE_COLORS
        labels = GRADE_LABELS
        legend_order = [
            "strong_uphill_or_external_load",
            "mild_uphill_or_external_load",
            "normal_load",
            "downhill_or_coast_possible",
            "transition_load",
            "non_analyzed",
        ]
        title = "OBD-only 경사/외부부하 지도"
        subtitle = "GPS는 위치 표시만 사용, 경사 판정은 차량속도+엔진부하 기반"
        band_col = "map_band"
    else:
        route["map_band"] = route["band"]
        colors = SPEED_COLORS
        labels = SPEED_LABELS
        legend_order = ["stop", "low", "mid", "high"]
        title = "실제 주행 경로"
        subtitle = "속도 구간별 경로 표시"
        band_col = "map_band"

    segments = build_segments(route, band_col=band_col)
    write_geojson(route, geojson_path)
    write_static_preview(route, preview_png, colors, labels, legend_order, title, subtitle)
    write_map_html(route, segments, map_html, colors, labels, legend_order, title, subtitle)
    print(f"route_points={len(route)}")
    print(f"segments={len(segments)}")
    print(f"html={map_html.resolve()}")
    print(f"geojson={geojson_path.resolve()}")
    print(f"preview={preview_png.resolve()}")


if __name__ == "__main__":
    main()
