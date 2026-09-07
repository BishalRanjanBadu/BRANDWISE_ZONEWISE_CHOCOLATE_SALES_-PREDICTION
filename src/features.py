"""THE transform. One implementation, used by training and by serving.

This is a direct port of notebook 06. `tests/test_features.py` asserts it
reproduces `data/06_encoded_tree.csv` cell-for-cell — if this file and the
notebook ever diverge, that test fails rather than the divergence reaching
production as a quiet accuracy loss.

Two rules hold throughout:
  * every feature is a function of t-1 or earlier. Nothing reads its own
    timestamp. `assert_no_contemporaneous()` enforces it mechanically.
  * `transform()` fits nothing and drops no rows. `clean_for_training()` is
    the only function permitted to see the target or filter rows.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from src.config import EXOG, MAXLAG, MIN_HISTORY, TARGET

FESTIVE_MONTHS = (8, 9, 10, 11)          # Rakhi -> Diwali gifting window
SUMMER_MONTHS = (4, 5, 6)

# ── feature order is a contract ─────────────────────────────────────────────
# d_l1, d_l2 and rm3 are ALGEBRAICALLY DEGENERATE and are excluded:
#
#   rm3  = mean(ly_l1..ly_l3) - ly_l1 = (-2*ly_l1 + ly_l2 + ly_l3)/3
#   d_l1 = ly_l1 - ly_l2 ;   d_l2 = ly_l2 - ly_l3
#   -(2*d_l1 + d_l2)/3       = (-2*ly_l1 + ly_l2 + ly_l3)/3   == rm3   exactly
#
# Those three columns have rank 2, so the design matrix is singular and their
# VIF is infinite. Notebook 06 detected this numerically and dropped them, which
# is why the promoted artifact carries 25 features and not 28.
#
# They are excluded BY NAME here rather than by re-running a VIF check, because
# whether statsmodels reports inf or a very large finite number for a singular
# column varies across library versions - so a numerical check would make the
# feature set environment-dependent, and a Phase-3 retrain on a different
# machine could silently produce a different feature set.
#
# Restoring the momentum signal without the singularity means keeping d_l1 and
# d_l2 and dropping only rm3 - a strictly better feature set. That is a RETRAIN,
# not an edit: it changes the feature hash and every existing artifact would
# fail readiness. Logged as a Phase-3 candidate.
DEGENERATE_EXCLUDED = ["d_l1", "d_l2", "rm3"]

FEATURE_ORDER = (
    ["d_l3", "rm6", "sd6", "zt_d1", "share_l1"]
    + [f"{c}_{s}" for c in EXOG for s in ("l1", "d1")]
    + ["month", "is_festive", "brand_code", "zone_code"]
)
assert not set(FEATURE_ORDER) & set(DEGENERATE_EXCLUDED)
assert len(FEATURE_ORDER) == 25, f"expected 25 features, got {len(FEATURE_ORDER)}"


def make_series_id(df: pd.DataFrame) -> pd.Series:
    return df["brand"].astype(str) + "|" + df["zone"].astype(str)


def add_calendar(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["date"] = pd.to_datetime(df["date"])
    df["month"] = df["date"].dt.month
    df["is_festive"] = df["month"].isin(FESTIVE_MONTHS).astype(int)
    df["is_summer"] = df["month"].isin(SUMMER_MONTHS).astype(int)
    return df


def transform(panel: pd.DataFrame, brand_map: dict, zone_map: dict) -> pd.DataFrame:
    """Row-preserving, target-free at the feature level, fits nothing.

    `panel` is long: one row per (brand, zone, date) with the metric columns.
    Returns the same rows plus the engineered columns. Rows without enough
    history carry NaN features — the caller decides what to do with them,
    this function does not silently drop them.
    """
    df = panel.copy()
    df["date"] = pd.to_datetime(df["date"])
    if "series_id" not in df.columns:
        df["series_id"] = make_series_id(df)
    df = df.sort_values(["series_id", "date"]).reset_index(drop=True)
    df = add_calendar(df)

    g = df.groupby("series_id", sort=False)
    df["y"] = df[TARGET].astype(float)
    df["ly"] = np.log(df["y"])

    for lag in range(1, MAXLAG + 2):
        df[f"ly_l{lag}"] = g["ly"].shift(lag)

    for lag in range(1, 4):                                     # momentum
        df[f"d_l{lag}"] = df[f"ly_l{lag}"] - df[f"ly_l{lag + 1}"]

    lagcols = [f"ly_l{lag}" for lag in range(1, MAXLAG + 1)]
    df["rm3"] = df[lagcols[:3]].mean(axis=1) - df["ly_l1"]      # level-free
    df["rm6"] = df[lagcols[:6]].mean(axis=1) - df["ly_l1"]
    df["sd6"] = df[lagcols[:6]].std(axis=1)

    # cross-sectional: the zone's whole-category movement, lagged.
    # This is why the serving contract is zone-complete — the sum runs
    # across every brand present at that (zone, date).
    zone_total = df.groupby(["zone", "date"])["y"].transform("sum")
    df["lzt"] = np.log(zone_total)
    df["zt_l1"] = g["lzt"].shift(1)
    df["zt_d1"] = df["zt_l1"] - g["lzt"].shift(2)
    df["share_l1"] = df["ly_l1"] - df["zt_l1"]

    for col in EXOG:
        df[f"{col}_l1"] = g[col].shift(1)
        df[f"{col}_d1"] = df[f"{col}_l1"] - g[col].shift(2)

    df["brand_code"] = df["brand"].map(brand_map)
    df["zone_code"] = df["zone"].map(zone_map)
    return df


def add_training_target(df: pd.DataFrame) -> pd.DataFrame:
    """Target construction. Training only — needs the current month's value."""
    out = df.copy()
    out["target_d"] = out["ly"] - out["ly_l1"]
    return out


def clean_for_training(df: pd.DataFrame) -> pd.DataFrame:
    """May see the target and may drop rows. Never on the serving path."""
    return df.dropna(subset=FEATURE_ORDER + ["target_d", "ly_l1", "y"]).reset_index(drop=True)


# Columns the model was trained on as pandas `category` dtype. LightGBM records
# `pandas_categorical` in the booster at fit time and validates it at predict
# time, so serving these as plain int64 raises
#   ValueError: train and valid dataset categorical_feature do not match
# Restoring the dtype is part of train/serve symmetry, not a cosmetic detail.
CATEGORICAL_FEATURES = ["brand_code", "zone_code"]


def restore_categoricals(df: pd.DataFrame, brand_map: dict, zone_map: dict) -> pd.DataFrame:
    """Re-apply the training dtypes, with the FULL category set from the encoder
    maps — not just the values present in this payload. A payload containing 3
    brands must still carry all 18 categories, or the codes shift."""
    out = df.copy()
    for col, mapping in (("brand_code", brand_map), ("zone_code", zone_map)):
        if col in out.columns:
            out[col] = pd.Categorical(out[col], categories=sorted(mapping.values()))
    return out


def align(df: pd.DataFrame, feature_order: list[str],
          brand_map: dict | None = None, zone_map: dict | None = None) -> pd.DataFrame:
    """Reindex to the trained feature order: fill missing, drop extras, fix order,
    and restore the training dtypes.

    A count or order mismatch between training and serving silently corrupts
    predictions with no error; a dtype mismatch on the categorical columns raises
    at predict time. Neither is optional on the serving path.
    """
    missing = [f for f in feature_order if f not in df.columns]
    if missing:
        raise ValueError(f"features absent after transform: {missing}")
    out = df.reindex(columns=feature_order)
    if brand_map is not None and zone_map is not None:
        out = restore_categoricals(out, brand_map, zone_map)
    return out


def reconstruct(ly_l1: np.ndarray, pred_delta: np.ndarray) -> np.ndarray:
    """log first-difference -> level. Inverse of the training target."""
    return np.exp(np.asarray(ly_l1, float) + np.asarray(pred_delta, float))


def feature_hash(feature_order: list[str]) -> str:
    """Stable hash of the feature ORDER. Recorded in the manifest and checked
    at container load; a reorder without a retrain fails readiness."""
    import hashlib
    return hashlib.sha256("|".join(feature_order).encode()).hexdigest()[:16]


# ── leakage guards, callable from tests and from training ───────────────────
LAG_SUFFIXES = tuple(f"_l{i}" for i in range(1, 13)) + tuple(f"_d{i}" for i in range(1, 13))
CALENDAR_OK = {"month", "is_festive", "is_summer", "quarter", "t_index"}
DERIVED_OK = {"rm3", "rm6", "sd6", "share_l1", "brand_code", "zone_code"}


def assert_no_contemporaneous(feature_order: list[str]) -> None:
    """Every feature must carry lag/diff provenance, be a calendar constant,
    or be an explicitly whitelisted derived term."""
    bad = [f for f in feature_order
           if f not in CALENDAR_OK and f not in DERIVED_OK
           and not f.endswith(LAG_SUFFIXES)]
    if bad:
        raise AssertionError(f"features with no lag provenance: {bad}")


def history_depth(panel: pd.DataFrame) -> pd.Series:
    """Months of history per series — used by the API contract check."""
    return panel.groupby(make_series_id(panel)).size()


def check_history(panel: pd.DataFrame, min_months: int = MIN_HISTORY) -> list[str]:
    depth = history_depth(panel)
    return sorted(depth[depth < min_months].index.tolist())
