# -*- coding: utf-8 -*-
"""FastAPI wrapper for the rule-based vehicle recommendation pipeline."""

from __future__ import annotations

import os
import shutil
import uuid
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field

import recommendation_pipeline as pipeline


APP_VERSION = "0.1.0"
DEFAULT_UPLOAD_DIR = Path(os.getenv("RECOMMENDATION_UPLOAD_DIR", "api_uploads"))
DEFAULT_API_OUTPUT_DIR = Path(os.getenv("RECOMMENDATION_OUTPUT_DIR", "outputs/api"))
DEFAULT_VEHICLE_DB = Path(
    os.getenv("VEHICLE_DB_PATH", str(pipeline.DEFAULT_VEHICLE_DB))
)


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


def _constraints(payload: ConstraintPayload) -> pipeline.UserConstraints:
    return pipeline.UserConstraints(
        passengers=payload.passengers,
        cargo_need=payload.cargo_need,
        budget_min_10k_krw=payload.budget_min_10k_krw,
        budget_max_10k_krw=payload.budget_max_10k_krw,
        monthly_distance_km=payload.monthly_distance_km,
        apply_body_filter=payload.apply_body_filter,
    )


def _vehicle_db_path(path: str | None) -> Path:
    resolved = Path(path) if path else DEFAULT_VEHICLE_DB
    if not resolved.exists():
        raise HTTPException(status_code=400, detail=f"Vehicle DB not found: {resolved}")
    return resolved


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

    result = pipeline.run_recommendation_pipeline(
        drive_csv_path=drive_csv_path,
        vehicle_db_path=vehicle_db_path,
        constraints=constraints,
        top_n=top_n,
        output_dir=output_dir,
        save_artifacts=save_artifacts,
    )
    return pipeline.json_ready(result)


@app.get("/health")
def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "version": APP_VERSION,
        "vehicle_db_path": str(DEFAULT_VEHICLE_DB),
        "vehicle_db_exists": DEFAULT_VEHICLE_DB.exists(),
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
