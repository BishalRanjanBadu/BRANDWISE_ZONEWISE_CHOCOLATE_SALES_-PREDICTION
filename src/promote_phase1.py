"""One-time migration: flat Phase-1 artifacts -> immutable versioned prefix + alias.

Phase 1 wrote `models/best_model.pkl` flat, which has no rollback path — a
promotion overwrites the only copy. This copies that artifact into
`models/v_<utc>_phase1/` with a manifest recording the exact library versions
it was trained under, then points `CURRENT.json` at it.

Idempotent: re-running creates a new version prefix rather than overwriting an
existing one. Run once:

    python -m src.promote_phase1
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone

from src import registry, s3_io
from src.config import (KEY_CURRENT, KEY_LEGACY_CARD, KEY_LEGACY_FEATS,
                        KEY_LEGACY_METRICS, KEY_LEGACY_MODEL, PREFIX_MODELS)
from src.features import FEATURE_ORDER, feature_hash


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--git-sha", default="phase1")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    for key in (KEY_LEGACY_MODEL, KEY_LEGACY_FEATS, KEY_LEGACY_METRICS):
        if not s3_io.exists(key):
            print(f"FATAL: {key} not found — run notebook 07 first", file=sys.stderr)
            return 1

    model = s3_io.read_pickle(KEY_LEGACY_MODEL)
    feature_order = list(s3_io.read_pickle(KEY_LEGACY_FEATS))
    metrics = s3_io.read_json(KEY_LEGACY_METRICS)

    # The Phase-1 artifact must agree with the transform this codebase ships.
    if feature_order != FEATURE_ORDER:
        print("FATAL: Phase-1 feature order does not match src/features.FEATURE_ORDER",
              file=sys.stderr)
        print(f"  phase1 ({len(feature_order)}): {feature_order}", file=sys.stderr)
        print(f"  src    ({len(FEATURE_ORDER)}): {FEATURE_ORDER}", file=sys.stderr)
        return 1

    estimator_class = type(model).__name__
    declared = metrics.get("estimator_class")
    if declared and declared != estimator_class:
        print(f"FATAL: pickle is {estimator_class} but model_metrics.json says {declared}",
              file=sys.stderr)
        return 1

    # Library versions come from the file, NOT from this runtime — the pickle was
    # created on Colab and the manifest must record that, not wherever this runs.
    train_libs = metrics.get("library_versions")
    if not train_libs:
        print("FATAL: model_metrics.json has no library_versions; cannot build a "
              "compatibility manifest", file=sys.stderr)
        return 1

    prep = None
    for candidate in ("models/preprocessor.pkl",):
        if s3_io.exists(candidate):
            prep = s3_io.read_pickle(candidate)
            break
    if prep is None or "brand_map" not in prep:
        print("FATAL: models/preprocessor.pkl missing brand_map/zone_map", file=sys.stderr)
        return 1

    version_id = f"v_{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}_{args.git_sha[:7]}"
    prefix = f"{PREFIX_MODELS}{version_id}/"

    manifest = {
        "version_id": version_id,
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "git_sha": args.git_sha,
        "source": "phase-1 notebook 07, migrated by promote_phase1.py",
        "estimator_class": estimator_class,
        "contract_version": 1,
        "feature_order": feature_order,
        "feature_count": len(feature_order),
        "feature_hash": feature_hash(feature_order),
        "brand_map": prep["brand_map"],
        "zone_map": prep["zone_map"],
        "data_hash": metrics.get("data_hash", "unknown-phase1"),
        "library_versions": train_libs,
        "metrics": {"ai_brand_wmape": metrics.get("metrics_all_india_brand", {}).get("wmape"),
                    "naive_relative_mase": metrics.get("naive_relative_mase"),
                    "gate_passed": metrics.get("gate_passed")},
    }

    print(f"version_id      : {version_id}")
    print(f"estimator       : {estimator_class}")
    print(f"features        : {len(feature_order)}  hash={manifest['feature_hash']}")
    print(f"training libs   : {train_libs}")
    print(f"AI-brand wMAPE  : {manifest['metrics']['ai_brand_wmape']}")
    print(f"gate_passed     : {manifest['metrics']['gate_passed']}")

    if args.dry_run:
        print("\n--dry-run: nothing written")
        return 0

    s3_io.write_pickle(model, f"{prefix}model.pkl")
    s3_io.write_pickle(feature_order, f"{prefix}feature_names.pkl")
    s3_io.write_json(manifest, f"{prefix}manifest.json")
    s3_io.write_json(metrics, f"{prefix}metrics.json")
    if s3_io.exists(KEY_LEGACY_CARD):
        s3_io.copy(KEY_LEGACY_CARD, f"{prefix}model_card.md")

    alias = registry.promote(version_id, reason="phase-1 migration")
    print(f"\nwrote  s3://.../{prefix}")
    print(f"alias  {KEY_CURRENT} -> {json.dumps(alias, indent=2)}")

    loaded = registry.load_current(strict_libs=False)
    print(f"\nreadback OK: {loaded.version_id} / {type(loaded.estimator).__name__}")
    problems = loaded.check_compatibility(strict_libs=True)
    if problems:
        print("\nNOTE: this runtime differs from the training environment:")
        for p in problems:
            print(f"  - {p}")
        print("The serving image pins the training versions, so this is expected locally.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
