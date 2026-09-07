"""Shared test fixtures.

Everything here is built from the committed CSV fixture. No test touches S3,
so the CI test job runs with NO cloud credentials at all — which removes the
whole class of "CI failed on NoSuchKey / missing credentials / unset RAW_KEY".
"""
from __future__ import annotations

import os

import lightgbm as lgb
import pandas as pd
import pytest

from src import registry
from src.features import (FEATURE_ORDER, add_training_target, clean_for_training,
                          feature_hash, restore_categoricals, transform)

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "sample_panel.csv")


@pytest.fixture(scope="session")
def fixture_panel() -> pd.DataFrame:
    return pd.read_csv(FIXTURE, parse_dates=["date"])


@pytest.fixture(scope="session")
def encoder_maps(fixture_panel):
    return ({b: i for i, b in enumerate(sorted(fixture_panel.brand.unique()))},
            {z: i for i, z in enumerate(sorted(fixture_panel.zone.unique()))})


@pytest.fixture(scope="session")
def fixture_model(fixture_panel, encoder_maps):
    """Small deterministic model trained on the fixture. Stands in for the real
    artifact so the serving path can be exercised without S3."""
    bmap, zmap = encoder_maps
    feats = clean_for_training(add_training_target(transform(fixture_panel, bmap, zmap)))
    # Notebook 07 casts brand_code/zone_code to `category` before fitting, so the
    # booster stores pandas_categorical and validates it at predict time. Training
    # the fixture model on int64 instead made this suite blind to that skew, and a
    # 500 reached a live endpoint. Train it the way production trains it.
    X = restore_categoricals(feats[FEATURE_ORDER], bmap, zmap)
    assert str(X["brand_code"].dtype) == "category"
    model = lgb.LGBMRegressor(n_estimators=300, num_leaves=15, learning_rate=0.04,
                              min_child_samples=5, random_state=42, verbose=-1)
    model.fit(X, feats["target_d"])
    assert model.booster_.pandas_categorical, (
        "fixture model was not fitted with categorical dtypes — it cannot catch "
        "train/serve dtype skew")
    return model


@pytest.fixture(scope="session")
def loaded_model(fixture_model, encoder_maps):
    bmap, zmap = encoder_maps
    manifest = {
        "version_id": "v_test_fixture",
        "estimator_class": type(fixture_model).__name__,
        "contract_version": 1,
        "feature_order": FEATURE_ORDER,
        "feature_hash": feature_hash(FEATURE_ORDER),
        "brand_map": bmap,
        "zone_map": zmap,
        "library_versions": registry.runtime_versions(),
    }
    return registry.LoadedModel("v_test_fixture", fixture_model, list(FEATURE_ORDER),
                                manifest, {"note": "fixture-trained"})


@pytest.fixture()
def client(loaded_model, monkeypatch):
    """FastAPI TestClient with the registry short-circuited to the fixture model."""
    from fastapi.testclient import TestClient

    from src import api
    monkeypatch.setattr(api.registry, "load_current", lambda **kw: loaded_model)
    with TestClient(api.app) as c:
        yield c


@pytest.fixture()
def broken_client(monkeypatch):
    """Client whose artifact fails the manifest check — readiness must fail."""
    from fastapi.testclient import TestClient

    from src import api

    def boom(**kw):
        raise registry.ManifestMismatch("lightgbm 9.9.9 != training 4.6.0")

    monkeypatch.setattr(api.registry, "load_current", boom)
    with TestClient(api.app) as c:
        yield c


def payload_from(panel: pd.DataFrame, months: int = 8) -> dict:
    cols = ["brand", "zone", "date", "value_lakhs", "numeric_dist_pct", "wtd_dist_val",
            "num_stores", "ms_value", "volume_tonnes", "sah", "str_days", "pdo_rs"]
    keep = sorted(panel.date.unique())[-months:]
    sub = panel[panel.date.isin(keep)].copy()
    sub["date"] = pd.to_datetime(sub["date"]).dt.strftime("%Y-%m-%d")
    return {"history": sub[cols].to_dict("records"), "include_all_india": True}
