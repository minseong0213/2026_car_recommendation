# -*- coding: utf-8 -*-
"""FastAPI wrapper for the rule-based vehicle recommendation pipeline."""

from __future__ import annotations

import os
import shutil
import uuid
import logging
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field

import recommendation_pipeline as pipeline
from warning_diagnostics import diagnose_warning


APP_VERSION = "0.2.0"
DEFAULT_UPLOAD_DIR = Path(os.getenv("RECOMMENDATION_UPLOAD_DIR", "api_uploads"))
DEFAULT_API_OUTPUT_DIR = Path(os.getenv("RECOMMENDATION_OUTPUT_DIR", "outputs/api"))
DEFAULT_VEHICLE_DB = Path(
    os.getenv("VEHICLE_DB_PATH", str(pipeline.DEFAULT_VEHICLE_DB))
)
DEFAULT_S3_BUCKET = os.getenv("DRIVE_DATA_S3_BUCKET", "")
DEFAULT_S3_PREFIX = os.getenv("DRIVE_DATA_S3_PREFIX", "drive-logs").strip("/")
DEFAULT_S3_ENDPOINT_URL = os.getenv("S3_ENDPOINT_URL")
DEFAULT_AWS_REGION = os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION")
DEFAULT_PRESIGN_EXPIRES_S = int(os.getenv("DRIVE_DATA_PRESIGN_EXPIRES_S", "900"))
UPLOAD_LOG_LIMIT = int(os.getenv("DRIVE_UPLOAD_LOG_LIMIT", "200"))


logger = logging.getLogger("uvicorn.error")
UPLOAD_EVENTS: deque[dict[str, Any]] = deque(maxlen=max(10, UPLOAD_LOG_LIMIT))


app = FastAPI(
    title="Vehicle Recommendation API",
    version=APP_VERSION,
    description="API server for OBD CSV analysis and rule-based vehicle recommendations.",
)


class ConstraintPayload(BaseModel):
    passengers: int | None = None
    cargo_need: str | None = None
    budget_min_10k_krw: float | None = None
    budget_max_10k_krw: float | None = None
    monthly_distance_km: float | None = None
    apply_body_filter: bool = False


class RecommendationRequest(BaseModel):
    drive_csv_path: str = Field(..., description="Server-local path to an OBD CSV file.")
    vehicle_db_path: str | None = Field(
        default=None,
        description="Server-local path to the vehicle DB xlsx. Defaults to VEHICLE_DB_PATH.",
    )
    output_dir: str | None = Field(
        default=None,
        description="Artifact output directory when save_artifacts is true.",
    )
    save_artifacts: bool = False
    top_n: int = Field(default=10, ge=1, le=100)
    constraints: ConstraintPayload = Field(default_factory=ConstraintPayload)


class PresignUploadRequest(BaseModel):
    filename: str = "drive.csv"
    content_type: str = "text/csv"
    trip_id: str | None = None
    expires_in_s: int = Field(default=DEFAULT_PRESIGN_EXPIRES_S, ge=60, le=3600)


class S3RecommendationRequest(BaseModel):
    s3_bucket: str | None = Field(
        default=None,
        description="S3 bucket. Defaults to DRIVE_DATA_S3_BUCKET.",
    )
    s3_key: str = Field(..., description="S3 object key for the uploaded OBD CSV.")
    vehicle_db_path: str | None = Field(
        default=None,
        description="Server-local path to the vehicle DB xlsx. Defaults to VEHICLE_DB_PATH.",
    )
    output_dir: str | None = Field(
        default=None,
        description="Artifact output directory when save_artifacts is true.",
    )
    save_artifacts: bool = False
    top_n: int = Field(default=10, ge=1, le=100)
    constraints: ConstraintPayload = Field(default_factory=ConstraintPayload)


class WarningDiagnosisRequest(BaseModel):
    dtc_code: str = Field(..., min_length=4, max_length=12)
    pid_values: dict[str, float | int | str | None] = Field(default_factory=dict)


def _constraints(payload: ConstraintPayload) -> pipeline.UserConstraints:
    return pipeline.UserConstraints(
        passengers=payload.passengers,
        cargo_need=payload.cargo_need,
        budget_min_10k_krw=payload.budget_min_10k_krw,
        budget_max_10k_krw=payload.budget_max_10k_krw,
        monthly_distance_km=payload.monthly_distance_km,
        apply_body_filter=payload.apply_body_filter,
    )


def _format_log_fields(fields: dict[str, Any]) -> str:
    parts: list[str] = []
    for key, value in fields.items():
        if value is None:
            continue
        if isinstance(value, float):
            value = round(value, 4)
        text = str(value).replace("\n", " ")
        if len(text) > 160:
            text = f"{text[:157]}..."
        parts.append(f"{key}={text}")
    return " ".join(parts)


def _record_upload_event(event: str, **fields: Any) -> None:
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "event": event,
        **pipeline.json_ready(fields),
    }
    UPLOAD_EVENTS.append(entry)
    logger.info("[drive-upload] %s %s", event, _format_log_fields(fields))


def _last_value(df: pd.DataFrame, column: str | None) -> Any:
    if not column or column not in df.columns:
        return None
    values = df[column].dropna()
    if values.empty:
        return None
    value = values.iloc[-1]
    if pd.isna(value):
        return None
    if isinstance(value, (int, float)):
        return round(float(value), 4)
    parsed = pd.to_numeric(pd.Series([value]), errors="coerce").iloc[0]
    if pd.notna(parsed):
        return round(float(parsed), 4)
    return str(value)


def _first_existing_column(df: pd.DataFrame, candidates: list[str]) -> str | None:
    for column in candidates:
        if column in df.columns:
            return column
    return None


def _csv_debug_summary(csv_path: Path) -> dict[str, Any]:
    try:
        raw = pd.read_csv(csv_path, encoding="utf-8-sig")
        csv_columns = pipeline.resolve_csv_columns(raw)
        time_col = csv_columns.get("time")
        avg_l100_col = _first_existing_column(
            raw,
            [
                "평균 연비 (합계) (L/100km)",
                "평균 연비 (오늘) (L/100km)",
                "avg_l_per_100km",
            ],
        )
        fuel_source_col = _first_existing_column(
            raw,
            ["fuel_rate_source", "연료소비율 출처"],
        )

        return {
            "stored_csv_path": str(csv_path.resolve()),
            "file_size_bytes": csv_path.stat().st_size,
            "raw_rows": int(len(raw)),
            "columns": int(len(raw.columns)),
            "first_timestamp": str(raw[time_col].dropna().iloc[0])
            if time_col and not raw[time_col].dropna().empty
            else None,
            "last_timestamp": _last_value(raw, time_col),
            "distance_km": _last_value(raw, csv_columns.get("distance")),
            "fuel_used_l": _last_value(raw, csv_columns.get("fuel_used")),
            "avg_l_per_100km": _last_value(raw, avg_l100_col),
            "fuel_rate_lph": _last_value(raw, csv_columns.get("fuel_rate")),
            "fuel_rate_source": _last_value(raw, fuel_source_col),
            "rpm": _last_value(raw, csv_columns.get("rpm")),
            "speed_kph": _last_value(raw, csv_columns.get("obd_speed")),
        }
    except Exception as exc:
        return {
            "stored_csv_path": str(csv_path.resolve()),
            "file_size_bytes": csv_path.stat().st_size if csv_path.exists() else None,
            "csv_summary_error": f"{type(exc).__name__}: {exc}",
        }


def _recommendation_debug_summary(result: dict[str, Any]) -> dict[str, Any]:
    metadata = result.get("metadata") or {}
    features = result.get("features") or {}
    top = result.get("top_recommendations") or []
    top_one = top[0] if top else {}
    model_name = " ".join(
        str(top_one.get(key, "")).strip()
        for key in ["브랜드", "모델명"]
        if str(top_one.get(key, "")).strip()
    )

    return {
        "raw_rows": metadata.get("raw_rows"),
        "normalized_rows_1hz": metadata.get("normalized_rows_1hz"),
        "duration_s": features.get("duration_s"),
        "distance_km": features.get("daily_dist_km"),
        "avg_speed_kmh": features.get("avg_speed_kmh"),
        "fuel_used_l": features.get("fuel_used_l"),
        "observed_l_per_100km": features.get("observed_l_per_100km"),
        "load_status": features.get("load_status"),
        "top_model": model_name or None,
        "top_score": top_one.get("총점"),
        "total_recommendations": result.get("total_recommendations"),
    }


def _vehicle_db_path(path: str | None) -> Path:
    resolved = Path(path) if path else DEFAULT_VEHICLE_DB
    if not resolved.exists():
        raise HTTPException(status_code=400, detail=f"Vehicle DB not found: {resolved}")
    return resolved


def _s3_bucket(bucket: str | None = None) -> str:
    resolved = (bucket or DEFAULT_S3_BUCKET).strip()
    if not resolved:
        raise HTTPException(
            status_code=400,
            detail="S3 bucket is not configured. Set DRIVE_DATA_S3_BUCKET or pass s3_bucket.",
        )
    return resolved


def _s3_client():
    try:
        import boto3
        from botocore.config import Config
    except ImportError as exc:
        raise HTTPException(
            status_code=500,
            detail='S3 support needs boto3. Run: pip install boto3',
        ) from exc

    kwargs: dict[str, Any] = {}
    if DEFAULT_AWS_REGION:
        kwargs["region_name"] = DEFAULT_AWS_REGION
    if DEFAULT_S3_ENDPOINT_URL:
        kwargs["endpoint_url"] = DEFAULT_S3_ENDPOINT_URL
    elif DEFAULT_AWS_REGION:
        kwargs["endpoint_url"] = f"https://s3.{DEFAULT_AWS_REGION}.amazonaws.com"
    kwargs["config"] = Config(signature_version="s3v4")
    return boto3.client("s3", **kwargs)


def _safe_filename(filename: str) -> str:
    name = Path(filename or "drive.csv").name
    return name or "drive.csv"


def _make_drive_s3_key(filename: str, trip_id: str | None = None) -> tuple[str, str]:
    safe_name = _safe_filename(filename)
    resolved_trip_id = (trip_id or uuid.uuid4().hex).strip()
    date_path = datetime.now(timezone.utc).strftime("%Y/%m/%d")
    object_name = f"{resolved_trip_id}_{safe_name}"
    parts = [part for part in (DEFAULT_S3_PREFIX, date_path, object_name) if part]
    return "/".join(parts), resolved_trip_id


def _download_s3_csv(bucket: str, key: str) -> Path:
    if not key.strip():
        raise HTTPException(status_code=400, detail="s3_key is required")

    DEFAULT_UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    suffix = Path(key).suffix or ".csv"
    local_path = DEFAULT_UPLOAD_DIR / f"{uuid.uuid4().hex}{suffix}"
    try:
        _s3_client().download_file(bucket, key, str(local_path))
    except Exception as exc:
        _record_upload_event(
            "s3.download.failed",
            s3_uri=f"s3://{bucket}/{key}",
            error=f"{type(exc).__name__}: {exc}",
        )
        raise HTTPException(
            status_code=502,
            detail=f"Failed to download S3 object s3://{bucket}/{key}: {exc}",
        ) from exc
    _record_upload_event(
        "s3.downloaded",
        s3_uri=f"s3://{bucket}/{key}",
        **_csv_debug_summary(local_path),
    )
    return local_path


def _run_recommendation(
    drive_csv_path: Path,
    vehicle_db_path: Path,
    constraints: pipeline.UserConstraints,
    top_n: int,
    output_dir: Path | None,
    save_artifacts: bool,
) -> dict[str, Any]:
    if not drive_csv_path.exists():
        raise HTTPException(status_code=400, detail=f"Drive CSV not found: {drive_csv_path}")

    _record_upload_event(
        "recommendation.started",
        drive_csv_path=str(drive_csv_path.resolve()),
        vehicle_db_path=str(vehicle_db_path.resolve()),
        top_n=top_n,
        save_artifacts=save_artifacts,
    )
    try:
        result = pipeline.run_recommendation_pipeline(
            drive_csv_path=drive_csv_path,
            vehicle_db_path=vehicle_db_path,
            constraints=constraints,
            top_n=top_n,
            output_dir=output_dir,
            save_artifacts=save_artifacts,
        )
    except Exception as exc:
        _record_upload_event(
            "recommendation.failed",
            drive_csv_path=str(drive_csv_path.resolve()),
            error=f"{type(exc).__name__}: {exc}",
        )
        raise

    ready = pipeline.json_ready(result)
    _record_upload_event(
        "recommendation.done",
        drive_csv_path=str(drive_csv_path.resolve()),
        **_recommendation_debug_summary(ready),
    )
    return ready


@app.get("/health")
def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "version": APP_VERSION,
        "vehicle_db_path": str(DEFAULT_VEHICLE_DB),
        "vehicle_db_exists": DEFAULT_VEHICLE_DB.exists(),
        "drive_data_s3_bucket": DEFAULT_S3_BUCKET or None,
        "drive_data_s3_prefix": DEFAULT_S3_PREFIX,
        "aws_region": DEFAULT_AWS_REGION,
        "s3_endpoint_url": DEFAULT_S3_ENDPOINT_URL
        or (f"https://s3.{DEFAULT_AWS_REGION}.amazonaws.com" if DEFAULT_AWS_REGION else None),
    }


@app.get("/drive-uploads/logs")
def drive_upload_logs(limit: int = 50) -> dict[str, Any]:
    resolved_limit = max(1, min(limit, UPLOAD_EVENTS.maxlen or UPLOAD_LOG_LIMIT))
    return {
        "count": min(resolved_limit, len(UPLOAD_EVENTS)),
        "events": list(UPLOAD_EVENTS)[-resolved_limit:],
    }


@app.get("/vehicles/summary")
def vehicle_summary(vehicle_db_path: str | None = None) -> dict[str, Any]:
    db_path = _vehicle_db_path(vehicle_db_path)
    df = pipeline.load_vehicle_db(db_path)
    powertrain_col = pipeline.VEHICLE_COL["powertrain"]
    brand_col = pipeline.VEHICLE_COL["brand"]
    return pipeline.json_ready(
        {
            "vehicle_db_path": str(db_path),
            "vehicle_count": int(len(df)),
            "powertrain_counts": df[powertrain_col].value_counts().to_dict(),
            "brand_counts": df[brand_col].value_counts().to_dict(),
        }
    )


@app.post("/warning-lights/diagnose")
def warning_light_diagnosis(request: WarningDiagnosisRequest) -> dict[str, Any]:
    """Rank five likely causes using a DTC and its live PID snapshot."""

    return diagnose_warning(request.dtc_code, request.pid_values)


@app.post("/recommendations")
async def recommend_from_server_path(request: RecommendationRequest) -> dict[str, Any]:
    db_path = _vehicle_db_path(request.vehicle_db_path)
    output_dir = Path(request.output_dir) if request.output_dir else None
    return await run_in_threadpool(
        _run_recommendation,
        Path(request.drive_csv_path),
        db_path,
        _constraints(request.constraints),
        request.top_n,
        output_dir,
        request.save_artifacts,
    )


@app.post("/drive-uploads/presign")
def create_drive_upload_url(request: PresignUploadRequest) -> dict[str, Any]:
    bucket = _s3_bucket()
    key, trip_id = _make_drive_s3_key(request.filename, request.trip_id)
    content_type = request.content_type or "text/csv"

    try:
        upload_url = _s3_client().generate_presigned_url(
            ClientMethod="put_object",
            Params={
                "Bucket": bucket,
                "Key": key,
                "ContentType": content_type,
            },
            ExpiresIn=request.expires_in_s,
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Failed to create S3 upload URL: {exc}") from exc

    _record_upload_event(
        "presign.created",
        trip_id=trip_id,
        s3_uri=f"s3://{bucket}/{key}",
        content_type=content_type,
        expires_in_s=request.expires_in_s,
    )

    return {
        "trip_id": trip_id,
        "bucket": bucket,
        "key": key,
        "s3_uri": f"s3://{bucket}/{key}",
        "method": "PUT",
        "upload_url": upload_url,
        "headers": {"Content-Type": content_type},
        "expires_in_s": request.expires_in_s,
    }


async def _recommend_from_s3_request(request: S3RecommendationRequest) -> dict[str, Any]:
    bucket = _s3_bucket(request.s3_bucket)
    db_path = _vehicle_db_path(request.vehicle_db_path)
    output_dir = Path(request.output_dir) if request.output_dir else None

    _record_upload_event(
        "complete.received",
        s3_uri=f"s3://{bucket}/{request.s3_key}",
        top_n=request.top_n,
        save_artifacts=request.save_artifacts,
    )

    csv_path = await run_in_threadpool(_download_s3_csv, bucket, request.s3_key)
    result = await run_in_threadpool(
        _run_recommendation,
        csv_path,
        db_path,
        _constraints(request.constraints),
        request.top_n,
        output_dir,
        request.save_artifacts,
    )
    result["upload"] = {
        "s3_bucket": bucket,
        "s3_key": request.s3_key,
        "s3_uri": f"s3://{bucket}/{request.s3_key}",
        "stored_csv_path": str(csv_path.resolve()),
    }
    return result


@app.post("/drive-uploads/complete")
async def complete_drive_upload(request: S3RecommendationRequest) -> dict[str, Any]:
    return await _recommend_from_s3_request(request)


@app.post("/recommendations/s3")
async def recommend_from_s3(request: S3RecommendationRequest) -> dict[str, Any]:
    return await _recommend_from_s3_request(request)


@app.post("/recommendations/upload")
async def recommend_from_upload(
    file: UploadFile = File(...),
    top_n: int = Form(10),
    save_artifacts: bool = Form(False),
    vehicle_db_path: str | None = Form(None),
    passengers: int | None = Form(None),
    cargo_need: str | None = Form(None),
    budget_min_10k_krw: float | None = Form(None),
    budget_max_10k_krw: float | None = Form(None),
    monthly_distance_km: float | None = Form(None),
    apply_body_filter: bool = Form(False),
) -> dict[str, Any]:
    if top_n < 1 or top_n > 100:
        raise HTTPException(status_code=400, detail="top_n must be between 1 and 100")

    db_path = _vehicle_db_path(vehicle_db_path)
    DEFAULT_UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

    suffix = Path(file.filename or "drive.csv").suffix or ".csv"
    trip_id = uuid.uuid4().hex
    csv_path = DEFAULT_UPLOAD_DIR / f"{trip_id}{suffix}"
    with csv_path.open("wb") as buffer:
        shutil.copyfileobj(file.file, buffer)
    _record_upload_event(
        "upload.saved",
        trip_id=trip_id,
        filename=file.filename,
        **_csv_debug_summary(csv_path),
    )

    output_dir = DEFAULT_API_OUTPUT_DIR / trip_id if save_artifacts else None
    constraints = pipeline.UserConstraints(
        passengers=passengers,
        cargo_need=cargo_need,
        budget_min_10k_krw=budget_min_10k_krw,
        budget_max_10k_krw=budget_max_10k_krw,
        monthly_distance_km=monthly_distance_km,
        apply_body_filter=apply_body_filter,
    )
    result = await run_in_threadpool(
        _run_recommendation,
        csv_path,
        db_path,
        constraints,
        top_n,
        output_dir,
        save_artifacts,
    )
    result["upload"] = {
        "trip_id": trip_id,
        "stored_csv_path": str(csv_path.resolve()),
    }
    return result
