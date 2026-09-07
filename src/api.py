"""FastAPI serving layer.

Probe split matters:
  GET /live    process only, no I/O          -> livenessProbe
  GET /health  artifact loaded + manifest ok -> readinessProbe
A liveness probe that touches S3 restarts the pod during an S3 blip; a
readiness probe that does not validate the manifest lets a corrupt artifact
take traffic.

The model is loaded in the startup hook, not lazily on first request. A lazy
global races across uvicorn workers and lets readiness pass before the model
exists, so traffic arrives at a pod that cannot serve it.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import uuid
from datetime import datetime, timezone

import pandas as pd
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from src import registry, s3_io
from src.config import (API_V, ENV, LOG_PREDICTIONS, MIN_HISTORY, PREFIX_PREDS,
                        REQUIRED_METRICS, TARGET, ZONES_PANEL)
from src.predict import (ContractViolation, forecast, reconcile_all_india,
                         rule_based_fallback, validate_payload)

logging.basicConfig(
    level=logging.INFO, stream=sys.stdout,
    format='{"ts":"%(asctime)s","level":"%(levelname)s","msg":%(message)s}')
log = logging.getLogger("api")

STATE: dict = {"model": None, "load_error": None, "loaded_at": None}

@asynccontextmanager
async def lifespan(_app: FastAPI):
    _load_model()
    yield


app = FastAPI(title="Chocolate Sales Forecast API", version=API_V, lifespan=lifespan)

# Scoped to known callers. "*" on a prediction API lets any origin drive it.
ALLOWED_ORIGINS = [o for o in os.getenv("ALLOWED_ORIGINS", "http://localhost:8501").split(",") if o]
app.add_middleware(CORSMiddleware, allow_origins=ALLOWED_ORIGINS,
                   allow_credentials=False, allow_methods=["GET", "POST"],
                   allow_headers=["*"])


# ── schema. No defaults on required fields: a caller who omits twenty inputs
#    must get a 422, not a confident 200 computed from placeholders. ─────────
class HistoryRecord(BaseModel):
    brand: str
    zone: str
    date: str
    value_lakhs: float
    numeric_dist_pct: float
    wtd_dist_val: float
    num_stores: float
    ms_value: float
    volume_tonnes: float
    sah: float
    str_days: float
    pdo_rs: float


class PredictRequest(BaseModel):
    history: list[HistoryRecord] = Field(
        ..., min_length=1,
        description=(f"Zone-complete panel, >= {MIN_HISTORY} months per brand x zone. "
                     "Every brand must be present in every zone requested."))
    include_all_india: bool = True


class PredictionRow(BaseModel):
    brand: str
    zone: str
    target_month: str
    last_actual: float
    prediction: float
    pct_change: float


class PredictResponse(BaseModel):
    predictions: list[PredictionRow]
    target_month: str
    model_version: str
    contract_version: int
    source: str                      # "model" | "fallback"
    request_id: str
    n_series: int


def _load_model():
    strict = os.getenv("STRICT_LIB_CHECK", "true").lower() == "true"
    try:
        STATE["model"] = registry.load_current(strict_libs=strict)
        STATE["loaded_at"] = datetime.now(timezone.utc).isoformat()
        STATE["load_error"] = None
        log.info(json.dumps({"event": "model_loaded",
                             "version": STATE["model"].version_id,
                             "features": len(STATE["model"].feature_order),
                             "estimator": type(STATE["model"].estimator).__name__}))
    except Exception as exc:                      # noqa: BLE001 - recorded, not swallowed
        # Clear the model. A failed reload must NOT leave a stale artifact in
        # place serving traffic while /health reports the new version.
        STATE["model"] = None
        STATE["loaded_at"] = None
        STATE["load_error"] = f"{type(exc).__name__}: {exc}"
        log.error(json.dumps({"event": "model_load_failed", "error": STATE["load_error"]}))


@app.get("/live")
def live():
    """Process liveness. No I/O — deliberately cannot fail on an S3 blip."""
    return {"status": "alive", "env": ENV}


@app.get("/health")
def health():
    """Readiness. Fails while the artifact is missing or manifest-incompatible,
    so a bad promotion never takes traffic."""
    if STATE["model"] is None:
        raise HTTPException(503, detail={"status": "not_ready",
                                         "reason": STATE["load_error"] or "model not loaded"})
    m = STATE["model"]
    return {"status": "ready", "model_version": m.version_id,
            "estimator": type(m.estimator).__name__,
            "contract_version": m.manifest.get("contract_version"),
            "feature_hash": m.manifest.get("feature_hash"),
            "loaded_at": STATE["loaded_at"], "env": ENV}


@app.get(f"/{API_V}/model")
def model_info():
    if STATE["model"] is None:
        raise HTTPException(503, detail="model not loaded")
    m = STATE["model"]
    return {"version_id": m.version_id, "manifest": m.manifest, "metrics": m.metrics}


@app.get(f"/{API_V}/schema")
def schema():
    return {"required_metrics": REQUIRED_METRICS,
            "min_history_months": MIN_HISTORY,
            "zones": ZONES_PANEL,
            "note": ("Payload must be zone-complete: zt_d1 and share_l1 are derived from the "
                     "zone's total across all brands, so a partial brand set is rejected.")}


@app.post(f"/{API_V}/predict", response_model=PredictResponse)
def predict(req: PredictRequest, request: Request,
            allow_fallback: bool = Query(False,
                description="Serve naive persistence, tagged source=fallback, if the model is down")):
    request_id = str(uuid.uuid4())
    panel = pd.DataFrame([r.model_dump() for r in req.history])

    m = STATE["model"]
    if m is None:
        if not allow_fallback:
            raise HTTPException(503, detail={"error": "model not loaded",
                                             "reason": STATE["load_error"],
                                             "hint": "retry with ?allow_fallback=true for naive persistence"})
        preds = rule_based_fallback(panel)
        source, version = "fallback", "none"
    else:
        try:
            bmap, zmap = _brand_map(m), _zone_map(m)
            validate_payload(panel, bmap, zmap)
            preds = forecast(panel, m.estimator, m.feature_order, bmap, zmap)
            source, version = "model", m.version_id
        except ContractViolation as exc:
            raise HTTPException(422, detail={"error": "contract_violation", "detail": str(exc)})

    if req.include_all_india:
        preds = pd.concat([preds, reconcile_all_india(preds)], ignore_index=True)

    target_month = pd.to_datetime(preds["target_month"]).max()
    preds["target_month"] = pd.to_datetime(preds["target_month"]).dt.strftime("%Y-%m-%d")

    response = PredictResponse(
        predictions=[PredictionRow(**r) for r in preds.to_dict("records")],
        target_month=target_month.strftime("%Y-%m-%d"),
        model_version=version, contract_version=m.manifest.get("contract_version", 0) if m else 0,
        source=source, request_id=request_id, n_series=len(preds))

    log.info(json.dumps({"event": "predict", "request_id": request_id, "source": source,
                         "model_version": version, "n_series": len(preds),
                         "target_month": response.target_month}))
    if LOG_PREDICTIONS:
        _log_prediction(request_id, panel, preds, version, source)
    return response


def _brand_map(m) -> dict:
    return m.manifest.get("brand_map") or {}


def _zone_map(m) -> dict:
    return m.manifest.get("zone_map") or {}


def _log_prediction(request_id, panel, preds, version, source):
    """Write inputs+outputs so the Phase-3 drift job has a source. Disabled by
    default: the serving IRSA role is read-only until Phase 3 grants a scoped
    PutObject to predictions/."""
    try:
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        body = "\n".join(json.dumps({"request_id": request_id, "model_version": version,
                                     "source": source, **r})
                         for r in preds.to_dict("records"))
        s3_io.write_text(body, f"{PREFIX_PREDS}dt={day}/{request_id}.jsonl")
    except Exception as exc:                      # noqa: BLE001
        log.error(json.dumps({"event": "prediction_log_failed", "error": str(exc)}))
