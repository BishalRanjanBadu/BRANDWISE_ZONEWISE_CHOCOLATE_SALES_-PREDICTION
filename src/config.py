"""Project constants. Single source of truth — every identifier is baked in here
and imported everywhere else. Nothing in src/ hardcodes a bucket, region or key."""
import os

# ── AWS / infrastructure ────────────────────────────────────────────────────
AWS_REGION      = os.getenv("AWS_REGION", "ap-south-2")
AWS_ACCOUNT_ID  = "117211782845"
S3_BUCKET       = os.getenv("S3_BUCKET", "brandwise-zonewise-chocolate-sales-prediction")
ECR_REPO        = "brandwise-zonewise-chocolate-sales-prediction"
ECR_REGISTRY    = f"{AWS_ACCOUNT_ID}.dkr.ecr.{AWS_REGION}.amazonaws.com"
ECR_IMAGE       = f"{ECR_REGISTRY}/{ECR_REPO}"
EKS_CLUSTER     = "chocolate-sales-eks"

# ── environment isolation. UAT and prod NEVER share a model key. ────────────
ENV             = os.getenv("APP_ENV", "prod")          # dev | uat | prod
_PREFIX         = "" if ENV == "prod" else f"{ENV}/"

# ── S3 keys ─────────────────────────────────────────────────────────────────
KEY_RAW         = "raw/BRAND_WISE_CHOCOLATE__ALL_INDIA.xlsx"
KEY_CONTRACT    = "contracts/schema_v1.json"
KEY_PANEL       = f"{_PREFIX}data/05_hypothesis_done.csv"   # cleaned panel, pre-feature
KEY_CURRENT     = f"{_PREFIX}models/CURRENT.json"           # alias -> version prefix
KEY_REFERENCE   = f"{_PREFIX}reference/training_reference.csv"
PREFIX_MODELS   = f"{_PREFIX}models/"
PREFIX_PREDS    = f"{_PREFIX}predictions/"

# legacy Phase-1 flat artifacts, migrated once by promote_phase1.py
KEY_LEGACY_MODEL   = "models/best_model.pkl"
KEY_LEGACY_FEATS   = "models/feature_names.pkl"
KEY_LEGACY_METRICS = "models/model_metrics.json"
KEY_LEGACY_CARD    = "models/model_card.md"

# ── modelling contract (must match notebook 06) ─────────────────────────────
TARGET        = "value_lakhs"
TARGET_XFORM  = "log_first_difference"
GRAIN         = ["brand", "zone"]
ZONES_PANEL   = ["North (U+R)", "East (U+R)", "West (U+R)", "South (U+R)"]
ZONE_TOTAL    = "All India (U+R)"
HORIZON       = 1
MAXLAG        = 6
MIN_HISTORY   = 7          # months of history a payload must carry per series
CONTRACT_VER  = 1

EXOG = ["numeric_dist_pct", "wtd_dist_val", "num_stores", "ms_value",
        "volume_tonnes", "sah", "str_days", "pdo_rs"]

# every metric a history record must carry
REQUIRED_METRICS = [TARGET] + EXOG

# ── quality gate (revised at Phase-1 sign-off; provisional and uncosted) ────
GATE_AI_WMAPE = 6.00    # absolute ceiling: point estimate + noise headroom
GATE_VS_NAIVE = 0.80    # must reach <=0.80x the naive-persistence wMAPE
GATE_MASE     = 1.00    # naive-relative MASE

# ── serving ─────────────────────────────────────────────────────────────────
API_PORT   = 8000
UI_PORT    = 8501
API_V      = "v1"
LOG_PREDICTIONS = os.getenv("LOG_PREDICTIONS", "false").lower() == "true"
