"""Serving-layer tests. Credential-free: the model comes from conftest."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from tests.conftest import payload_from


# ── probes ──────────────────────────────────────────────────────────────────
def test_live_does_no_io_and_always_answers(broken_client):
    """Liveness must pass even when the artifact cannot load, or a bad model
    would trigger an endless CrashLoopBackOff instead of failing readiness."""
    r = broken_client.get("/live")
    assert r.status_code == 200
    assert r.json()["status"] == "alive"


def test_readiness_fails_on_manifest_mismatch(broken_client):
    r = broken_client.get("/health")
    assert r.status_code == 503
    assert "lightgbm" in str(r.json()).lower()


def test_readiness_reports_version_and_hash(client):
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ready"
    assert body["model_version"] == "v_test_fixture"
    assert body["feature_hash"]


def test_predict_blocked_while_model_unloaded(broken_client, fixture_panel):
    r = broken_client.post("/v1/predict", json=payload_from(fixture_panel))
    assert r.status_code == 503


# ── contract ────────────────────────────────────────────────────────────────
def test_missing_required_field_is_422_not_a_prediction(client, fixture_panel):
    p = payload_from(fixture_panel)
    del p["history"][0]["num_stores"]
    r = client.post("/v1/predict", json=p)
    assert r.status_code == 422
    assert "prediction" not in r.text


def test_empty_history_rejected(client):
    assert client.post("/v1/predict", json={"history": []}).status_code == 422


def test_unknown_brand_rejected(client, fixture_panel):
    p = payload_from(fixture_panel)
    for rec in p["history"]:
        if rec["brand"] == "MARS":
            rec["brand"] = "NOT_A_REAL_BRAND"
    r = client.post("/v1/predict", json=p)
    assert r.status_code == 422
    assert "unknown brands" in r.text


def test_zone_incomplete_payload_rejected(client, fixture_panel):
    """The defining constraint of this API: a partial brand set makes the zone
    total wrong, so it must 4xx rather than serve a confident wrong number."""
    p = payload_from(fixture_panel)
    p["history"] = [r for r in p["history"]
                    if not (r["zone"] == "North (U+R)" and r["brand"] == "MARS")]
    r = client.post("/v1/predict", json=p)
    assert r.status_code == 422
    assert "zone-incomplete" in r.text


def test_insufficient_history_rejected(client, fixture_panel):
    r = client.post("/v1/predict", json=payload_from(fixture_panel, months=3))
    assert r.status_code == 422
    assert "history" in r.text.lower()


def test_non_positive_target_rejected(client, fixture_panel):
    p = payload_from(fixture_panel)
    p["history"][0]["value_lakhs"] = 0.0
    r = client.post("/v1/predict", json=p)
    assert r.status_code == 422


def test_null_in_required_metric_rejected(client, fixture_panel):
    """pandas NaN is not valid JSON — clients send null, and null must 4xx."""
    p = payload_from(fixture_panel)
    p["history"][5]["sah"] = None
    r = client.post("/v1/predict", json=p)
    assert r.status_code == 422


# ── predictions ─────────────────────────────────────────────────────────────
def test_happy_path_shape_and_tagging(client, fixture_panel):
    r = client.post("/v1/predict", json=payload_from(fixture_panel))
    assert r.status_code == 200
    b = r.json()
    assert b["source"] == "model"
    assert b["model_version"] == "v_test_fixture"
    assert b["request_id"]
    df = pd.DataFrame(b["predictions"])
    zones = df[df.zone != "All India (U+R)"]
    assert len(zones) == 18 * 4
    assert np.isfinite(df.prediction).all()
    assert (df.prediction > 0).all()


def test_target_month_is_one_month_after_history(client, fixture_panel):
    p = payload_from(fixture_panel)
    last = max(r["date"] for r in p["history"])
    b = client.post("/v1/predict", json=p).json()
    expected = (pd.Timestamp(last) + pd.offsets.MonthBegin(1)).strftime("%Y-%m-%d")
    assert b["target_month"] == expected


def test_all_india_equals_sum_of_zones(client, fixture_panel):
    """Bottom-up reconciliation: additivity was verified exactly on the raw data,
    so the roll-up must be an exact sum, not an approximation."""
    b = client.post("/v1/predict", json=payload_from(fixture_panel)).json()
    df = pd.DataFrame(b["predictions"])
    ai = df[df.zone == "All India (U+R)"].set_index("brand").prediction
    zs = df[df.zone != "All India (U+R)"].groupby("brand").prediction.sum()
    assert np.allclose(ai.sort_index().values, zs.sort_index().values, rtol=1e-9)


def test_business_invariant_forecast_stays_in_series_envelope(client, fixture_panel):
    """Two invariants, both derived from the data rather than invented.

    1. Every forecast sits within [0.2x, 5x] of the last actual. A broken
       log/exp round trip lands far outside this; real volatility does not.
    2. For STABLE series - those whose own history never moved more than 30%
       month-over-month - the forecast must also stay under 30%.

    A flat threshold across all series would be wrong: KINDER JOY West really
    did move +198% in one month, and the panel's worst genuine swing is 327%.
    """
    b = client.post("/v1/predict", json=payload_from(fixture_panel)).json()
    df = pd.DataFrame(b["predictions"])
    zones = df[df.zone != "All India (U+R)"].copy()

    ratio = zones["prediction"] / zones["last_actual"]
    assert ratio.between(0.2, 5.0).all(), (
        "forecast outside [0.2x, 5x] of last actual - suspect the log/exp round trip: "
        f"{zones.loc[~ratio.between(0.2, 5.0), ['brand', 'zone']].to_dict('records')}")

    hist = fixture_panel.copy()
    hist["sid"] = hist.brand + "|" + hist.zone
    swing = (hist.sort_values("date").groupby("sid").value_lakhs
                 .apply(lambda s: np.abs(100 * (s.values[1:] / s.values[:-1] - 1)).max()))
    zones["sid"] = zones.brand + "|" + zones.zone
    stable = zones[zones.sid.map(swing) < 30]
    assert len(stable) > 20, "fixture has too few stable series to test against"
    assert stable["pct_change"].abs().max() < 30, (
        "a historically stable series was forecast to jump: "
        f"{stable.loc[stable['pct_change'].abs().idxmax()].to_dict()}")


def test_prediction_is_deterministic(client, fixture_panel):
    p = payload_from(fixture_panel)
    a = client.post("/v1/predict", json=p).json()["predictions"]
    c = client.post("/v1/predict", json=p).json()["predictions"]
    assert [x["prediction"] for x in a] == [x["prediction"] for x in c]


def test_column_order_does_not_change_predictions(client, fixture_panel):
    """Feature alignment: a caller sending fields in a different order must get
    identical numbers. Without reindexing this silently corrupts output."""
    p = payload_from(fixture_panel)
    base = client.post("/v1/predict", json=p).json()["predictions"]
    shuffled = {"history": [dict(reversed(list(r.items()))) for r in p["history"]],
                "include_all_india": True}
    other = client.post("/v1/predict", json=shuffled).json()["predictions"]
    assert [x["prediction"] for x in base] == [x["prediction"] for x in other]


# ── fallback ────────────────────────────────────────────────────────────────
def test_fallback_is_tagged_and_equals_persistence(broken_client, fixture_panel):
    p = payload_from(fixture_panel)
    r = broken_client.post("/v1/predict?allow_fallback=true", json=p)
    assert r.status_code == 200
    b = r.json()
    assert b["source"] == "fallback", "an untagged fallback is indistinguishable from a prediction"
    assert b["model_version"] == "none"
    df = pd.DataFrame(b["predictions"])
    assert np.allclose(df["pct_change"], 0.0)


def test_fallback_is_opt_in(broken_client, fixture_panel):
    r = broken_client.post("/v1/predict", json=payload_from(fixture_panel))
    assert r.status_code == 503, "fallback must never be served silently"


# ── metadata ────────────────────────────────────────────────────────────────
def test_schema_endpoint_documents_the_batch_constraint(client):
    b = client.get("/v1/schema").json()
    assert b["min_history_months"] >= 7
    assert "zone" in b["note"].lower()


def test_model_endpoint_exposes_manifest(client):
    b = client.get("/v1/model").json()
    assert b["version_id"] == "v_test_fixture"
    assert b["manifest"]["feature_hash"]
