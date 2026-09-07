"""Training entrypoint. Split-before-fit, immutable output, gated promotion.

    python -m src.train --git-sha $(git rev-parse HEAD) --promote

Writes a NEW `models/v_<utc>_<sha>/` prefix every run and never overwrites one.
`CURRENT.json` is only advanced when the quality gate passes AND --promote is
given, so a failing retrain leaves production untouched.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone

import numpy as np
import pandas as pd

from src import registry, s3_io
from src.config import (GATE_AI_WMAPE, GATE_MASE, GATE_VS_NAIVE, KEY_PANEL,
                        KEY_REFERENCE, PREFIX_MODELS, TARGET, ZONE_TOTAL)
from src.features import (FEATURE_ORDER, add_training_target,
                          assert_no_contemporaneous, clean_for_training, transform)

TRAIN_END = "2019-02-01"
SPLIT_COL = "__split"


def wmape(y, yhat) -> float:
    y, yhat = np.asarray(y, float), np.asarray(yhat, float)
    return float(100 * np.abs(y - yhat).sum() / y.sum())


def build_maps(panel: pd.DataFrame) -> tuple[dict, dict]:
    return ({b: i for i, b in enumerate(sorted(panel["brand"].unique()))},
            {z: i for i, z in enumerate(sorted(panel["zone"].unique()))})


def evaluate(frame: pd.DataFrame, pred: np.ndarray, denom: dict) -> dict:
    t = frame.copy()
    t["pred"] = pred
    ai = t.groupby(["brand", "date"]).agg(y=("y", "sum"), pred=("pred", "sum"))
    return {"zone_wmape": wmape(t.y, t.pred),
            "ai_brand_wmape": wmape(ai.y, ai.pred),
            "mase": float(np.mean(np.abs(t.y - t.pred) / t.series_id.map(denom)))}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--git-sha", default="local")
    ap.add_argument("--promote", action="store_true",
                    help="advance CURRENT.json if the gate passes")
    ap.add_argument("--panel-key", default=KEY_PANEL)
    args = ap.parse_args(argv)

    assert_no_contemporaneous(FEATURE_ORDER)

    raw = s3_io.read_csv(args.panel_key, parse_dates=["date"])
    panel = raw[raw["zone"] != ZONE_TOTAL].copy()
    brand_map, zone_map = build_maps(panel)

    # SPLIT BEFORE ANY FIT. The split is temporal and is decided here, once.
    panel[SPLIT_COL] = np.where(panel["date"] <= pd.Timestamp(TRAIN_END), "train", "test")

    feats = clean_for_training(add_training_target(transform(panel, brand_map, zone_map)))
    tr = feats[feats[SPLIT_COL] == "train"]
    te = feats[feats[SPLIT_COL] == "test"]
    print(f"train {tr.shape} {tr.date.min():%b-%Y}..{tr.date.max():%b-%Y} | "
          f"test {te.shape} {te.date.min():%b-%Y}..{te.date.max():%b-%Y}")
    if te.empty:
        print("FATAL: empty holdout", file=sys.stderr)
        return 1

    import lightgbm as lgb
    params = dict(subsample_freq=1, subsample=0.8, reg_lambda=0.0, num_leaves=15,
                  n_estimators=500, min_child_samples=10, learning_rate=0.02,
                  colsample_bytree=0.9, random_state=42, verbose=-1)
    model = lgb.LGBMRegressor(**params)
    model.fit(tr[FEATURE_ORDER], tr["target_d"])          # train rows only

    denom = {s: float(np.mean(np.abs(np.diff(g.sort_values("date").y.values))))
             for s, g in tr.groupby("series_id")}
    pred = np.exp(te["ly_l1"].values + model.predict(te[FEATURE_ORDER]))
    m_model = evaluate(te, pred, denom)
    m_naive = evaluate(te, np.exp(te["ly_l1"].values), denom)

    vs_naive = m_model["ai_brand_wmape"] / m_naive["ai_brand_wmape"]
    rel_mase = m_model["mase"] / m_naive["mase"]
    gate = {
        "ai_brand_wmape": (m_model["ai_brand_wmape"], GATE_AI_WMAPE,
                           m_model["ai_brand_wmape"] <= GATE_AI_WMAPE),
        "vs_naive":       (vs_naive, GATE_VS_NAIVE, vs_naive <= GATE_VS_NAIVE),
        "naive_rel_mase": (rel_mase, GATE_MASE, rel_mase < GATE_MASE),
    }
    for name, (got, thr, ok) in gate.items():
        print(f"{name:16s} {got:7.4f}  threshold {thr:<6} {'PASS' if ok else 'FAIL'}")
    gate_passed = all(ok for _, _, ok in gate.values())
    print(f"GATE_PASSED = {gate_passed}")

    version_id = registry.new_version_id(args.git_sha)
    prefix = registry.version_prefix(version_id)
    metrics = {"model": "lightgbm_tuned", "params": params,
               "metrics_model": m_model, "metrics_naive": m_naive,
               "vs_naive": vs_naive, "naive_relative_mase": rel_mase,
               "gate": {k: {"value": v[0], "threshold": v[1], "passed": v[2]}
                        for k, v in gate.items()},
               "gate_passed": gate_passed,
               "trained_utc": datetime.now(timezone.utc).isoformat()}
    manifest = registry.build_manifest(
        version_id=version_id, git_sha=args.git_sha, feature_order=FEATURE_ORDER,
        estimator_class=type(model).__name__, data_hash=registry.data_hash_of(tr[FEATURE_ORDER]),
        metrics=metrics, brand_map=brand_map, zone_map=zone_map)

    s3_io.write_pickle(model, f"{prefix}model.pkl")
    s3_io.write_pickle(FEATURE_ORDER, f"{prefix}feature_names.pkl")
    s3_io.write_json(manifest, f"{prefix}manifest.json")
    s3_io.write_json(metrics, f"{prefix}metrics.json")
    s3_io.write_csv(tr, KEY_REFERENCE)            # drift baseline, refreshed on every train
    print(f"wrote {PREFIX_MODELS}{version_id}/")

    if not args.promote:
        print("not promoting (--promote not given)")
    elif not gate_passed:
        print("GATE FAILED — CURRENT.json untouched, production unchanged")
        return 2
    else:
        print("promoted:", json.dumps(registry.promote(version_id, "automated retrain")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
