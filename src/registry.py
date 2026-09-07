"""Model registry.

Artifacts are immutable: a training run writes a NEW `models/v_<utc>_<sha>/`
prefix and never overwrites one. Promotion writes `CURRENT.json`; rollback
rewrites it to a prior version. Both directions are a single JSON write, and
nothing is ever destroyed.

`manifest.json` is the compatibility contract. The serving container reads it
at startup and REFUSES READINESS on a mismatch — wrong contract version, wrong
feature-order hash, or a library-version delta on the pickled estimator. A bad
promotion then fails readiness, the previous pods keep serving, and no traffic
reaches a corrupt unpickle.
"""
from __future__ import annotations

import hashlib
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone

from src import s3_io
from src.config import CONTRACT_VER, KEY_CURRENT, PREFIX_MODELS
from src.features import feature_hash

# Libraries whose version affects the fitted state of a pickled estimator.
# A delta here means the unpickle may appear to succeed and be silently wrong.
CRITICAL_LIBS = ("lightgbm", "xgboost", "sklearn", "numpy", "pandas")


def runtime_versions() -> dict:
    import numpy, pandas, sklearn
    v = {"python": ".".join(map(str, sys.version_info[:3])),
         "sklearn": sklearn.__version__,
         "numpy": numpy.__version__,
         "pandas": pandas.__version__}
    try:
        import lightgbm
        v["lightgbm"] = lightgbm.__version__
    except ImportError:
        pass
    try:
        import xgboost
        v["xgboost"] = xgboost.__version__
    except ImportError:
        pass
    return v


def new_version_id(git_sha: str) -> str:
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"v_{ts}_{(git_sha or 'nosha')[:7]}"


def version_prefix(version_id: str) -> str:
    return f"{PREFIX_MODELS}{version_id}/"


def build_manifest(*, version_id: str, git_sha: str, feature_order: list[str],
                   estimator_class: str, data_hash: str, metrics: dict,
                   brand_map: dict, zone_map: dict) -> dict:
    return {
        "version_id": version_id,
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "git_sha": git_sha,
        "estimator_class": estimator_class,
        "contract_version": CONTRACT_VER,
        "feature_order": feature_order,
        "feature_count": len(feature_order),
        "feature_hash": feature_hash(feature_order),
        "data_hash": data_hash,
        # encoders travel WITH the artifact - a code remap without a retrain
        # would silently relabel every brand
        "brand_map": brand_map,
        "zone_map": zone_map,
        "library_versions": runtime_versions(),
        "metrics": metrics,
    }


def data_hash_of(df) -> str:
    return hashlib.sha256(
        __import__("pandas").util.hash_pandas_object(df, index=True).values.tobytes()
    ).hexdigest()[:16]


# ── promotion / rollback: alias writes only ─────────────────────────────────
def promote(version_id: str, reason: str = "") -> dict:
    if not s3_io.exists(f"{version_prefix(version_id)}manifest.json"):
        raise FileNotFoundError(f"{version_id} has no manifest — refusing to promote")
    alias = {"version_id": version_id,
             "prefix": version_prefix(version_id),
             "promoted_utc": datetime.now(timezone.utc).isoformat(),
             "reason": reason}
    s3_io.write_json(alias, KEY_CURRENT)
    return alias


def current_alias() -> dict:
    return s3_io.read_json(KEY_CURRENT)


def list_versions() -> list[str]:
    paginator = s3_io.client().get_paginator("list_objects_v2")
    seen = set()
    from src.config import S3_BUCKET
    for page in paginator.paginate(Bucket=S3_BUCKET, Prefix=PREFIX_MODELS, Delimiter="/"):
        for cp in page.get("CommonPrefixes", []):
            name = cp["Prefix"][len(PREFIX_MODELS):].strip("/")
            if name.startswith("v_"):
                seen.add(name)
    return sorted(seen)


class ManifestMismatch(RuntimeError):
    """Raised at load. Must fail readiness, never be swallowed."""


@dataclass
class LoadedModel:
    version_id: str
    estimator: object
    feature_order: list
    manifest: dict
    metrics: dict = field(default_factory=dict)

    def check_compatibility(self, strict_libs: bool = True) -> list[str]:
        problems = []
        if self.manifest.get("contract_version") != CONTRACT_VER:
            problems.append(
                f"contract version {self.manifest.get('contract_version')} != runtime {CONTRACT_VER}")

        expected = self.manifest.get("feature_hash")
        actual = feature_hash(self.feature_order)
        if expected != actual:
            problems.append(f"feature-order hash {actual} != manifest {expected}")

        want = self.manifest.get("library_versions", {})
        have = runtime_versions()
        for lib in CRITICAL_LIBS:
            if lib in want and lib in have and want[lib] != have[lib]:
                msg = f"{lib} {have[lib]} != training {want[lib]}"
                problems.append(msg) if strict_libs else None
        return problems


def load_current(strict_libs: bool = True) -> LoadedModel:
    """Resolve the alias, load the artifact, validate the manifest.

    Raises ManifestMismatch on any incompatibility. The caller (the FastAPI
    startup hook) lets that propagate so readiness fails and the old pods
    keep serving.
    """
    alias = current_alias()
    prefix = alias["prefix"]
    manifest = s3_io.read_json(f"{prefix}manifest.json")
    estimator = s3_io.read_pickle(f"{prefix}model.pkl")
    feature_order = s3_io.read_pickle(f"{prefix}feature_names.pkl")
    metrics = s3_io.read_json(f"{prefix}metrics.json") if s3_io.exists(f"{prefix}metrics.json") else {}

    loaded = LoadedModel(alias["version_id"], estimator, list(feature_order), manifest, metrics)

    got = type(estimator).__name__
    want = manifest.get("estimator_class")
    if want and got != want:
        raise ManifestMismatch(f"pickle is {got} but manifest declares {want}")

    problems = loaded.check_compatibility(strict_libs=strict_libs)
    if problems:
        raise ManifestMismatch(
            f"artifact {alias['version_id']} is incompatible with this runtime: "
            + "; ".join(problems))
    return loaded
