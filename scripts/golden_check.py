"""Golden-payload parity check.

Run the SAME payload against local uvicorn, the container, and the LoadBalancer
and compare to the decimal. A container that returns a different number from
local is a corrupt artifact — and both would still return HTTP 200, which is
why this is automated rather than eyeballed.

    # 1. record expectations from a known-good local server
    python scripts/golden_check.py --url http://localhost:8000 --record

    # 2. compare anywhere else
    python scripts/golden_check.py --url http://localhost:8000        # docker
    python scripts/golden_check.py --url http://<lb-hostname>         # k8s

Exit codes: 0 match | 1 mismatch or transport failure | 2 endpoint not ready
"""
from __future__ import annotations

import argparse
import json
import os
import sys

import pandas as pd
import requests

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
FIXTURE = os.path.join(ROOT, "tests", "fixtures", "sample_panel.csv")
EXPECTED = os.path.join(ROOT, "tests", "golden", "expected.json")

COLS = ["brand", "zone", "date", "value_lakhs", "numeric_dist_pct", "wtd_dist_val",
        "num_stores", "ms_value", "volume_tonnes", "sah", "str_days", "pdo_rs"]


def build_payload(months: int = 10) -> dict:
    panel = pd.read_csv(FIXTURE, parse_dates=["date"])
    keep = sorted(panel.date.unique())[-months:]
    sub = panel[panel.date.isin(keep)].copy()
    sub["date"] = sub["date"].dt.strftime("%Y-%m-%d")

    # A payload with NULL categoricals is deliberately included: pandas NaN is
    # not valid JSON, and clients must send null. The API must reject it with
    # 422 rather than silently imputing.
    return {"history": sub[COLS].to_dict("records"), "include_all_india": True}


def null_payload() -> dict:
    p = build_payload()
    p["history"][3]["sah"] = None
    return p


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True)
    ap.add_argument("--record", action="store_true",
                    help="write expected.json instead of comparing")
    ap.add_argument("--tolerance", type=float, default=1e-6,
                    help="absolute tolerance on each prediction")
    args = ap.parse_args(argv)
    base = args.url.rstrip("/")

    try:
        h = requests.get(f"{base}/health", timeout=15)
    except Exception as exc:                       # noqa: BLE001
        print(f"FATAL: cannot reach {base}: {exc}", file=sys.stderr)
        return 1
    if h.status_code != 200:
        print(f"FATAL: {base}/health returned {h.status_code}: {h.text[:300]}", file=sys.stderr)
        return 2
    health = h.json()
    print(f"endpoint : {base}")
    print(f"version  : {health.get('model_version')}")
    print(f"estimator: {health.get('estimator')}")
    print(f"feat hash: {health.get('feature_hash')}")

    r = requests.post(f"{base}/v1/predict", json=build_payload(), timeout=120)
    if r.status_code != 200:
        print(f"FATAL: predict returned {r.status_code}: {r.text[:400]}", file=sys.stderr)
        return 1
    body = r.json()
    if body["source"] != "model":
        print(f"FATAL: source={body['source']} — a fallback is not a parity check",
              file=sys.stderr)
        return 1

    got = {f"{p['brand']}|{p['zone']}": p["prediction"] for p in body["predictions"]}

    # the null payload must be REJECTED, not served
    rn = requests.post(f"{base}/v1/predict", json=null_payload(), timeout=60)
    if rn.status_code != 422:
        print(f"FATAL: null categorical returned {rn.status_code}, expected 422", file=sys.stderr)
        return 1
    print("null-payload rejection: OK (422)")

    if args.record:
        os.makedirs(os.path.dirname(EXPECTED), exist_ok=True)
        with open(EXPECTED, "w") as fh:
            json.dump({"model_version": health.get("model_version"),
                       "feature_hash": health.get("feature_hash"),
                       "target_month": body["target_month"],
                       "predictions": got}, fh, indent=2, sort_keys=True)
        print(f"\nrecorded {len(got)} expectations -> {EXPECTED}")
        return 0

    if not os.path.exists(EXPECTED):
        print(f"FATAL: {EXPECTED} not found. Run with --record against a known-good "
              "local server first.", file=sys.stderr)
        return 1

    exp = json.load(open(EXPECTED))
    if exp["feature_hash"] != health.get("feature_hash"):
        print(f"FATAL: feature hash {health.get('feature_hash')} != recorded "
              f"{exp['feature_hash']} — the transform changed", file=sys.stderr)
        return 1

    missing = sorted(set(exp["predictions"]) - set(got))
    extra = sorted(set(got) - set(exp["predictions"]))
    if missing or extra:
        print(f"FATAL: series mismatch. missing={missing[:5]} extra={extra[:5]}", file=sys.stderr)
        return 1

    worst_key, worst = None, 0.0
    for k, want in exp["predictions"].items():
        d = abs(got[k] - want)
        if d > worst:
            worst_key, worst = k, d

    print(f"\ncompared {len(got)} series")
    print(f"largest absolute difference: {worst:.10f}  ({worst_key})")
    if worst > args.tolerance:
        print(f"FATAL: exceeds tolerance {args.tolerance}. The artifact or the library "
              "versions differ from the recorded environment.", file=sys.stderr)
        return 1
    print("GOLDEN PARITY OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
