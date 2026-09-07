"""Prediction. Uses the same `transform()` as training — no second implementation.

The forecast is next-month (h=1) per brand x zone. Given history through month
T, it returns month T+1. Because `zt_d1` and `share_l1` are computed from the
zone's whole-category total, the input must be zone-complete: every brand that
belongs in a zone must be present, or the denominator is wrong and the
prediction is confidently incorrect. `validate_payload` rejects that case with
a 4xx rather than serving it.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from src.config import MIN_HISTORY, REQUIRED_METRICS, TARGET
from src.features import align, make_series_id, reconstruct, transform


class ContractViolation(ValueError):
    """Payload does not satisfy the data contract. Maps to HTTP 422."""


def validate_payload(panel: pd.DataFrame, brand_map: dict, zone_map: dict) -> None:
    missing_cols = [c for c in ["brand", "zone", "date"] + REQUIRED_METRICS
                    if c not in panel.columns]
    if missing_cols:
        raise ContractViolation(f"missing required columns: {missing_cols}")

    unknown_brands = sorted(set(panel["brand"]) - set(brand_map))
    unknown_zones = sorted(set(panel["zone"]) - set(zone_map))
    if unknown_brands:
        raise ContractViolation(f"unknown brands (not seen in training): {unknown_brands}")
    if unknown_zones:
        raise ContractViolation(f"unknown zones (not seen in training): {unknown_zones}")

    nulls = {c: int(panel[c].isna().sum()) for c in REQUIRED_METRICS
             if panel[c].isna().any()}
    if nulls:
        raise ContractViolation(f"null values in required metrics: {nulls}")

    bad = panel[panel[TARGET] <= 0]
    if len(bad):
        raise ContractViolation(
            f"{len(bad)} rows have {TARGET} <= 0; the target is modelled in log space")

    sid = make_series_id(panel)
    depth = panel.assign(_sid=sid).groupby("_sid")["date"].nunique()
    shallow = sorted(depth[depth < MIN_HISTORY].index.tolist())
    if shallow:
        raise ContractViolation(
            f"{len(shallow)} series carry fewer than {MIN_HISTORY} months of history: "
            f"{shallow[:5]}{'...' if len(shallow) > 5 else ''}")

    # zone completeness — the reason this endpoint is batch-only
    seen = panel.groupby("zone")["brand"].nunique()
    expected = panel["brand"].nunique()
    incomplete = sorted(seen[seen < expected].index.tolist())
    if incomplete:
        raise ContractViolation(
            f"zone-incomplete payload: {incomplete} carry fewer than the {expected} brands "
            "present elsewhere. zt_d1 and share_l1 are computed from the zone total, so a "
            "partial brand set produces a wrong denominator. Send every brand per zone.")


def forecast(panel: pd.DataFrame, model, feature_order: list[str],
             brand_map: dict, zone_map: dict) -> pd.DataFrame:
    """History through T -> forecast for T+1, one row per series."""
    panel = panel.copy()
    panel["date"] = pd.to_datetime(panel["date"])

    as_of = panel["date"].max()
    target_month = (as_of + pd.offsets.MonthBegin(1)).normalize()

    # Append the target month as an empty row per series so the lag machinery
    # produces its feature vector. The target column is filled with the last
    # known value purely to keep log() defined; every feature reads t-1 or
    # earlier, so this value cannot influence the prediction. The
    # leakage test asserts exactly that.
    tail = (panel.sort_values("date").groupby(["brand", "zone"], as_index=False).last())
    tail["date"] = target_month
    extended = pd.concat([panel, tail], ignore_index=True)

    feats = transform(extended, brand_map, zone_map)
    rows = feats[feats["date"] == target_month].copy()
    if rows.empty:
        raise ContractViolation("no rows produced for the target month")

    incomplete = rows[align(rows, feature_order).isna().any(axis=1)]
    if len(incomplete):
        raise ContractViolation(
            f"{len(incomplete)} series lack sufficient history to build all features "
            f"(e.g. {incomplete['series_id'].head(3).tolist()})")

    X = align(rows, feature_order, brand_map, zone_map)
    try:
        delta = model.predict(X)
    except ValueError as exc:
        # Most likely a train/serve dtype skew on the categorical columns. Fail
        # loudly with the actual cause rather than as an opaque 500.
        raise RuntimeError(
            f"model.predict failed on an aligned frame: {exc}. "
            f"dtypes={ {c: str(X[c].dtype) for c in ('brand_code', 'zone_code') if c in X} }"
        ) from exc
    rows["prediction"] = reconstruct(rows["ly_l1"].values, delta)
    rows["last_actual"] = np.exp(rows["ly_l1"].values)
    rows["pct_change"] = 100 * (rows["prediction"] / rows["last_actual"] - 1)
    rows["target_month"] = target_month

    return rows[["brand", "zone", "target_month", "last_actual",
                 "prediction", "pct_change"]].reset_index(drop=True)


def reconcile_all_india(preds: pd.DataFrame) -> pd.DataFrame:
    """Bottom-up: the four zones sum exactly to All-India (verified 7.5e-10 in
    notebook 01), so no reconciliation matrix is needed."""
    out = (preds.groupby(["brand", "target_month"], as_index=False)
                .agg(prediction=("prediction", "sum"),
                     last_actual=("last_actual", "sum")))
    out["zone"] = "All India (U+R)"
    out["pct_change"] = 100 * (out["prediction"] / out["last_actual"] - 1)
    return out[["brand", "zone", "target_month", "last_actual", "prediction", "pct_change"]]


def rule_based_fallback(panel: pd.DataFrame) -> pd.DataFrame:
    """Naive persistence. The declared fallback when the model cannot serve.

    Its output is TAGGED at the response layer. An untagged fallback is
    indistinguishable from a healthy prediction, which is worse than an error.
    """
    panel = panel.copy()
    panel["date"] = pd.to_datetime(panel["date"])
    as_of = panel["date"].max()
    target_month = (as_of + pd.offsets.MonthBegin(1)).normalize()
    last = (panel[panel["date"] == as_of]
            .groupby(["brand", "zone"], as_index=False)[TARGET].last())
    last["target_month"] = target_month
    last["last_actual"] = last[TARGET]
    last["prediction"] = last[TARGET]
    last["pct_change"] = 0.0
    return last[["brand", "zone", "target_month", "last_actual", "prediction", "pct_change"]]
