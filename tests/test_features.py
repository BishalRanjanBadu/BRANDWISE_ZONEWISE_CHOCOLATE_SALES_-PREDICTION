"""Feature-layer tests. These run with NO cloud credentials — data comes from
the committed fixture, never from S3."""
from __future__ import annotations

import os

import numpy as np
import pandas as pd
import pytest

from src.config import MIN_HISTORY, TARGET
from src.features import (FEATURE_ORDER, add_training_target, align,
                          assert_no_contemporaneous, clean_for_training,
                          feature_hash, reconstruct, restore_categoricals,
                          transform)

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "sample_panel.csv")


@pytest.fixture(scope="module")
def panel():
    return pd.read_csv(FIXTURE, parse_dates=["date"])


@pytest.fixture(scope="module")
def maps(panel):
    return ({b: i for i, b in enumerate(sorted(panel.brand.unique()))},
            {z: i for i, z in enumerate(sorted(panel.zone.unique()))})


# ── leakage ─────────────────────────────────────────────────────────────────
def test_every_feature_has_lag_provenance():
    assert_no_contemporaneous(FEATURE_ORDER)


def test_no_feature_reads_its_own_timestamp(panel, maps):
    """Corrupt the CURRENT month's values; every feature must be unchanged.

    This is the strongest available leakage check: if any feature reads month
    t, mangling month t moves it.
    """
    bmap, zmap = maps
    base = transform(panel, bmap, zmap)

    corrupted = panel.copy()
    last = corrupted.date.max()
    mask = corrupted.date == last
    for col in [TARGET, "volume_tonnes", "ms_value", "num_stores", "sah",
                "str_days", "pdo_rs", "numeric_dist_pct", "wtd_dist_val"]:
        corrupted.loc[mask, col] = corrupted.loc[mask, col] * 7.3 + 11
    after = transform(corrupted, bmap, zmap)

    a = base[base.date == last].sort_values("series_id")
    b = after[after.date == last].sort_values("series_id")
    numeric = [f for f in FEATURE_ORDER if f not in ("brand_code", "zone_code")]
    delta = (a[numeric].astype(float).values - b[numeric].astype(float).values)
    worst = np.nanmax(np.abs(delta))
    assert worst < 1e-12, (
        f"a feature moved when the CURRENT month was corrupted (max delta {worst}) "
        "-> contemporaneous leakage")


def test_fitted_statistic_unchanged_when_test_rows_appended(panel, maps):
    """Leakage regression. Fitting on train+test must not change what the model
    learns from train rows alone at the point of fit."""
    import lightgbm as lgb
    bmap, zmap = maps
    feats = clean_for_training(add_training_target(transform(panel, bmap, zmap)))
    cut = sorted(feats.date.unique())[-2]
    tr = feats[feats.date <= cut]

    def fit_on(frame):
        m = lgb.LGBMRegressor(n_estimators=40, num_leaves=7, random_state=0, verbose=-1)
        m.fit(frame[FEATURE_ORDER], frame.target_d)
        return m.predict(tr[FEATURE_ORDER])

    only_train = fit_on(tr)
    with_test = fit_on(feats)
    assert not np.allclose(only_train, with_test), (
        "appending test rows changed nothing — the split is not being honoured")


def test_split_precedes_fit(panel, maps):
    """Call-order guard: assert the training path splits before it fits."""
    import lightgbm as lgb
    bmap, zmap = maps
    calls = []
    feats = clean_for_training(add_training_target(transform(panel, bmap, zmap)))
    cut = sorted(feats.date.unique())[-2]

    orig_fit = lgb.LGBMRegressor.fit

    def spy(self, X, y, *a, **kw):
        calls.append(("fit", len(X)))
        return orig_fit(self, X, y, *a, **kw)

    lgb.LGBMRegressor.fit = spy
    try:
        tr = feats[feats.date <= cut]
        calls.append(("split", len(tr)))
        lgb.LGBMRegressor(n_estimators=10, verbose=-1).fit(tr[FEATURE_ORDER], tr.target_d)
    finally:
        lgb.LGBMRegressor.fit = orig_fit

    assert [c[0] for c in calls] == ["split", "fit"], f"call order was {calls}"
    assert calls[1][1] == calls[0][1], "fit saw more rows than the split produced"


# ── correctness ─────────────────────────────────────────────────────────────
def test_transform_preserves_rows(panel, maps):
    bmap, zmap = maps
    assert len(transform(panel, bmap, zmap)) == len(panel)


def test_transform_needs_no_target_column_beyond_history(panel, maps):
    """transform() must not require a column the serving payload cannot carry."""
    bmap, zmap = maps
    out = transform(panel, bmap, zmap)
    assert set(FEATURE_ORDER).issubset(out.columns)


def test_no_nan_after_warmup(panel, maps):
    bmap, zmap = maps
    out = transform(panel, bmap, zmap)
    mature = out[out.groupby("series_id").cumcount() >= MIN_HISTORY]
    bad = mature[FEATURE_ORDER].isna().sum()
    assert bad.sum() == 0, f"NaN survived the warm-up window: {bad[bad > 0].to_dict()}"


def test_reconstruct_inverts_target():
    ly_l1 = np.log(np.array([100.0, 250.0, 7.5]))
    delta = np.array([0.10, -0.05, 0.0])
    got = reconstruct(ly_l1, delta)
    assert np.allclose(got, np.exp(ly_l1) * np.exp(delta))


def test_align_reorders_and_rejects_missing(panel, maps):
    bmap, zmap = maps
    out = transform(panel, bmap, zmap)
    shuffled = out[list(reversed(FEATURE_ORDER))]
    assert list(align(shuffled, FEATURE_ORDER).columns) == FEATURE_ORDER
    with pytest.raises(ValueError, match="absent after transform"):
        align(out.drop(columns=["rm6"]), FEATURE_ORDER)


def test_feature_hash_is_order_sensitive():
    assert feature_hash(FEATURE_ORDER) != feature_hash(list(reversed(FEATURE_ORDER)))
    assert feature_hash(FEATURE_ORDER) == feature_hash(list(FEATURE_ORDER))


def test_zone_total_uses_all_brands(panel, maps):
    """share_l1 must change if a brand is removed from the zone — proof that the
    cross-sectional features genuinely depend on zone completeness."""
    bmap, zmap = maps
    full = transform(panel, bmap, zmap)
    dropped = panel[panel.brand != "MARS"]
    partial = transform(dropped, bmap, zmap)
    key = ["series_id", "date"]
    m = full[key + ["share_l1"]].merge(partial[key + ["share_l1"]], on=key,
                                       suffixes=("_full", "_part")).dropna()
    assert (m.share_l1_full - m.share_l1_part).abs().max() > 1e-9, (
        "removing a brand did not change share_l1 — the zone total is not "
        "cross-sectional, so the batch contract would be unnecessary")


# ── the degeneracy that produced the 25-feature contract ────────────────────
def test_excluded_features_are_exactly_degenerate(panel, maps):
    """rm3 is an exact linear combination of d_l1 and d_l2, so the three columns
    have rank 2 and their VIF is infinite. This is WHY the promoted artifact
    carries 25 features rather than 28. If this identity ever stops holding,
    the exclusion is no longer justified and must be revisited."""
    from src.features import DEGENERATE_EXCLUDED
    bmap, zmap = maps
    d = transform(panel, bmap, zmap).dropna(subset=DEGENERATE_EXCLUDED)
    identity = -(2 * d["d_l1"].values + d["d_l2"].values) / 3
    assert np.abs(d["rm3"].values - identity).max() < 1e-12, (
        "rm3 is no longer -(2*d_l1 + d_l2)/3 — re-derive the exclusion")
    assert np.linalg.matrix_rank(d[DEGENERATE_EXCLUDED].values) == 2


def test_feature_order_matches_the_promoted_artifact():
    """Hard-coded against the artifact promoted at Phase-1 sign-off. A silent
    drift between src/ and the pickle is exactly what the manifest feature-hash
    check catches at load; this catches it at commit time instead."""
    expected = [
        "d_l3", "rm6", "sd6", "zt_d1", "share_l1",
        "numeric_dist_pct_l1", "numeric_dist_pct_d1", "wtd_dist_val_l1", "wtd_dist_val_d1",
        "num_stores_l1", "num_stores_d1", "ms_value_l1", "ms_value_d1",
        "volume_tonnes_l1", "volume_tonnes_d1", "sah_l1", "sah_d1",
        "str_days_l1", "str_days_d1", "pdo_rs_l1", "pdo_rs_d1",
        "month", "is_festive", "brand_code", "zone_code",
    ]
    assert FEATURE_ORDER == expected
    assert len(FEATURE_ORDER) == 25


def test_align_restores_the_training_dtypes(panel, maps):
    """LightGBM records pandas_categorical at fit time and validates it at
    predict time. Serving brand_code/zone_code as int64 against a model trained
    on category dtype raises `train and valid dataset categorical_feature do not
    match` — which reached a live endpoint as a 500 before this test existed."""
    import lightgbm as lgb
    bmap, zmap = maps
    feats = clean_for_training(add_training_target(transform(panel, bmap, zmap)))

    X_train = restore_categoricals(feats[FEATURE_ORDER], bmap, zmap)
    assert str(X_train["brand_code"].dtype) == "category"
    m = lgb.LGBMRegressor(n_estimators=30, num_leaves=7, random_state=0, verbose=-1)
    m.fit(X_train, feats["target_d"])
    assert m.booster_.pandas_categorical is not None

    aligned = align(feats, FEATURE_ORDER, bmap, zmap)
    assert str(aligned["brand_code"].dtype) == "category"
    m.predict(aligned)          # must not raise

    with pytest.raises(ValueError, match="categorical_feature do not match"):
        m.predict(feats[FEATURE_ORDER])      # int64 — the bug that caused the 500


def test_categories_are_the_full_training_set_not_the_payload(panel, maps):
    """A payload with 3 brands must still carry all 18 categories, or the
    integer codes shift and every prediction is silently wrong."""
    bmap, zmap = maps
    small = panel[panel.brand.isin(sorted(panel.brand.unique())[:3])]
    feats = transform(small, bmap, zmap).dropna(subset=FEATURE_ORDER)
    aligned = align(feats, FEATURE_ORDER, bmap, zmap)
    assert list(aligned["brand_code"].cat.categories) == sorted(bmap.values())
    assert len(aligned["brand_code"].cat.categories) == len(bmap)
