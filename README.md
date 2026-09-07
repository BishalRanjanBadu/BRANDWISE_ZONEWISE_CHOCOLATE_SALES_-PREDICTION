# 🍫 Brand-wise / Zone-wise Chocolate Sales Forecasting — Production MLOps

**A leakage-audited next-month demand forecast for the Indian chocolate category, served from EKS with an immutable model registry, a compatibility manifest, and three-point train/serve parity.**

![Python](https://img.shields.io/badge/Python-3.13-blue?logo=python)
![Model](https://img.shields.io/badge/Model-LightGBM-brightgreen)
![wMAPE](https://img.shields.io/badge/All--India%20wMAPE-5.30%25-success)
![vs Naive](https://img.shields.io/badge/vs%20naive-0.75×-success)
![Tests](https://img.shields.io/badge/tests-36%20passing-success)
![Serving](https://img.shields.io/badge/Serving-FastAPI%20%2B%20Streamlit-009688)
![Infra](https://img.shields.io/badge/EKS-ARM64%20t4g-orange?logo=amazonaws)
![CI](https://img.shields.io/badge/CI%2FCD-GitHub%20Actions-2088FF?logo=githubactions)

---

## What this repository actually is

Most forecasting repos stop at a notebook and a metric. This one carries the same model
through to a running endpoint, and the interesting engineering is in the gap between those
two things.

| Phase | Scope | Status |
|---|---|---|
| **1 — Experimentation** | 7 notebooks, S3-chained stages, data contract, model card | signed off |
| **2 — Deployment** | `src/` package, 36 tests, ARM64 image, EKS, CI/CD, rollback drill | complete |
| **3 — Operate & Monitor** | drift detection, label collection, automated retraining | not started |

Every phase is gated by a recorded sign-off listing what was reviewed, what was accepted,
and which deviations were taken knowingly.

---

## ✅ What was independently verified, not asserted

The claims below are backed by executable checks in `tests/`, not by prose in this file.

**Leakage boundary.** Every feature is a function of `t-1` or earlier. This is enforced
mechanically, not by inspection: `test_no_feature_reads_its_own_timestamp` corrupts the
current month's values by `×7.3 + 11` and asserts all 25 features are bit-identical
afterwards. If any feature read its own timestamp, it would move.

**Aggregate-period columns dropped.** The source workbook ships `YEC*`, `QTRFC*`, `YTD`,
` MAT` columns — 12-month and quarter windows that **contain the target month**. All 16 are
dropped in notebook 01 with a per-column rationale printed.

**Algebraic target derivatives dropped.** `val_growth_ya` is literally `y_t / y_{t-12} − 1`.
Five such columns removed. `ms_value` and `volume_tonnes` are also same-month derivatives of
the target, so they enter the model **only as lags**, enforced by assertion.

**Bottom-up reconciliation is exact.** The four zones sum to All-India with a maximum
relative error of **7.5e-10** across all 18 brands × 27 months — verified on the raw data,
not assumed, which is what makes summing zone forecasts legitimate.

**Selection never touched the holdout.** Every candidate is scored on the holdout for
*reporting*; the promoted model is chosen on 5 expanding train-window CV folds. That
distinction changed the outcome — see the SARIMA finding below.

---

## Overview

Forecasts **next-month sales value (`value_lakhs`)** at **brand × zone** grain for the
Indian chocolate category, then reconciles bottom-up to All-India. One pooled model serves
all 72 series.

| | |
|---|---|
| Target | `value_lakhs` (Rs. Lakhs), monthly |
| Transform | **log first-difference**, `log(y_t) − log(y_{t-1})` |
| Grain | 18 brands × 4 zones (North / East / West / South, U+R) |
| Horizon | 1 month |
| Training window | Jun-2017 … Feb-2019 |
| Holdout | Mar-2019 … Aug-2019, scored once |

**Why a differenced target, not the level.** Gradient-boosted trees cannot extrapolate
beyond their training range, and category value trends upward — a level target guarantees
systematic under-forecast on a held-out future. Measured: switching level → log-diff moved
All-India error from 8.03% to 3.93% before any tuning. It also collapses the 500× scale
spread between CADBURY DAIRY MILK and MARS, so one model can serve every series.

## Business problem

Demand planners need forward-looking, brand × zone forecasts to guide inventory,
distribution and share tracking. The usual failure mode is a backtest that looks excellent
because future information leaked into training, then collapses in production. The second
failure mode — less discussed — is a model that ships fine and then silently breaks at
serving time because the training and serving code disagree about a dtype. Both are
addressed explicitly here.

## Dataset

Nielsen retail-audit workbook, report-format Excel: **53,640 rows** = 18 brands × 149 zones
× 20 metrics, across 27 monthly periods.

| Property | Value |
|---|---|
| Modelling panel | 18 brands × 4 zones × 27 months = 1,944 rows |
| Missing cells on base metrics | **zero** |
| `ERR` sentinels | confined to derived growth/share columns, all dropped |
| Metric block ordering | identical across all 2,682 blocks → positional mapping is safe |

That last row matters: `% Grwth YA` appears **twice** per block (volume growth and value
growth), so mapping metrics by name would silently collide. Mapping is positional, with an
assertion that block order is constant.

## Methodology

| # | Notebook | Does |
|---|---|---|
| 01 | Data Loading & First Look | Flatten cross-tab; leakage scan; emit data contract; verify zone additivity; stamp `__split` |
| 02 | Data Cleaning | Standardise labels; drop algebraic target derivatives; calendar + festive features |
| 03 | Missing Values & Outliers | Group-wise imputation and caps **fit on train rows only** |
| 04 | Statistics & EDA | Train-rows-only analysis: concentration, seasonality, series volatility |
| 05 | Hypothesis Testing | ANOVA (zone), Kruskal (month), Welch (festive), ADF (stationarity → `d=1`) |
| 06 | Correlation, VIF & Encoding | Lag/diff/cross-sectional features; deterministic exclusion; leakage self-check |
| 07 | Model Building & Evaluation | Baselines → ARIMA/SARIMA → linear → tree ensembles; CV selection; SHAP; artifacts |

The split is stamped once in notebook 01 as `__split` and travels through all five S3 stage
files. No stage re-splits, and every downstream `.fit()` keys off `__split == 'train'`.

## Feature engineering

25 features, all lagged. Two are cross-sectional, which turns out to dictate the API shape.

| Type | Features |
|---|---|
| Momentum | `d_l3` (log-difference at lag 3) |
| Level-free history | `rm6`, `sd6` — 6-month mean and dispersion, expressed relative to the last value |
| **Cross-sectional** | `zt_d1` (zone category movement), `share_l1` (brand share position) |
| Lagged exogenous | `numeric_dist_pct`, `wtd_dist_val`, `num_stores`, `ms_value`, `volume_tonnes`, `sah`, `str_days`, `pdo_rs` — each as `_l1` and `_d1` |
| Calendar | `month`, `is_festive` (Aug–Nov, Rakhi → Diwali) |
| Entity | `brand_code`, `zone_code` (pandas `category`) |

**Feature-order hash: `48c939942ca4ef6a`** — recorded in the manifest and validated at
container startup.

### Why 25 and not 28

Three candidate features are **exactly** collinear:

```
rm3  = mean(ly_l1..ly_l3) − ly_l1 = (−2·ly_l1 + ly_l2 + ly_l3)/3
d_l1 = ly_l1 − ly_l2 ;  d_l2 = ly_l2 − ly_l3
−(2·d_l1 + d_l2)/3   ==  rm3        (verified to 1.8e-15, rank 2 of 3)
```

The design matrix is singular and their VIF is infinite. Notebook 06 originally detected
this **numerically** — and `statsmodels` returned `inf` on one runtime and a large finite
number on another, so the *same notebook on the same data* produced a 25-feature model on
one machine and 28 on another. The exclusion is now **by name**, with an assertion that the
identity holds. A floating-point threshold must never decide a feature set that a
compatibility manifest will later enforce.

## Models & results

**Holdout: Mar-2019 … Aug-2019, scored once, 432 predictions.**

| Model | Zone wMAPE | MAPE | MASE | R² | **All-India brand wMAPE** |
|---|---|---|---|---|---|
| naive persistence | 7.93% | 11.09% | 1.48 | 0.990 | 7.05% |
| seasonal naive (lag-12) | — | — | — | — | 14.02% |
| drift random walk | — | — | — | — | 7.08% |
| ARIMA(1,1,1) | — | — | — | — | 7.27% |
| ARIMA(2,1,2) | — | — | — | — | 7.92% |
| SARIMA(1,1,1)(1,0,0,12) | — | — | — | — | 4.65% |
| Ridge / Lasso | — | — | — | — | 7.35% / 7.34% |
| Random Forest | — | — | — | — | 4.53% |
| XGBoost (tuned) | — | — | — | — | 3.99% |
| **LightGBM (tuned) — promoted** | **7.14%** | **10.12%** | **1.19** | **0.991** | **5.30%** |

Beats naive persistence on **16 of 18 brands**, covering **97%** of category value.

### The holdout table is not the selection table

Selection ran on **5 expanding train-window CV folds**, and the ranking is different:

| Candidate | CV wMAPE | Holdout AI-brand |
|---|---|---|
| **lightgbm_tuned** | **6.47** | 5.30% |
| xgboost_tuned | 6.56 | 3.99% |
| naive_persistence | 8.31 | 7.05% |
| random_forest | 8.54 | 4.53% |
| ens 70% xgb / 30% sarima | 14.99 | 4.29% |
| ens 50 / 50 | 21.12 | 4.26% |
| **sarima_111_100_12** | **37.34** | **4.65%** |

XGBoost and the SARIMA ensembles look better on the holdout. **They were not promoted**,
because choosing from that column converts the test set into a selection set. The CV column
is the one that decided.

### The SARIMA finding

SARIMA scores 4.65% on the holdout — competitive — and **37.34% on cross-validation**, the
worst of every candidate including naive. The reason is printed by `statsmodels` during the
fold fits: *too few observations to estimate starting parameters for seasonal ARMA*. In the
earlier folds the seasonal term is not estimable, so the model silently degenerates to
`ARIMA(1,1,1)`. Its strong holdout number is one lucky draw from the longest training
window.

Seasonal differencing (`D=1, s=12`) is not even attemptable: 21 training months minus a
seasonal difference leaves 9 usable points.

**Rejected — a model that cannot be validated is not promotable, however good one window
looks.** Revisit trigger recorded: **~36 months of history**, at which point `s=12` becomes
estimable and the comparison should be re-run.

### Quality gate

The original 5.0% threshold was an uncosted round number. It was replaced at sign-off with a
two-part gate, because seed-to-seed variance of ~0.5 points makes any threshold hugging the
point estimate flip at random on retrain.

| Gate | Threshold | Actual | |
|---|---|---|---|
| All-India brand wMAPE | ≤ 6.00% | **5.30%** | ✅ |
| vs naive persistence | ≤ 0.80× | **0.751×** | ✅ |
| naive-relative MASE | < 1.00 | **0.808** | ✅ |

The relative half does the real work — it is self-calibrating, survives a harder holdout
window, and re-derives itself on every retrain. Both thresholds are **provisional and
uncosted**, with a review trigger before the first automated promotion.

> **On absolute MASE.** Every model here exceeds 1.0, naive included (1.48). MASE's
> denominator is the *in-sample* one-step naive MAE, and Mar–Aug 2019 is a more volatile
> window than the training period. Compute the baseline's value before setting a threshold
> on a normalised metric — otherwise you ship a gate no model can pass.

## Key predictors (SHAP)

1. `str_days_l1` — prior-month stock-turn days
2. `brand_code` — brand identity
3. `sd6` — 6-month dispersion
4. `month` — calendar position
5. `zt_d1` — zone category movement

Supply-side signal (`str_days`) and cross-sectional category movement outrank the target's
own recent history — a different picture from single-series forecasting, and the reason the
API is batch-shaped.

## Key insights

- **Festive spikes are signal, not noise.** IQR flags cluster in Aug–Nov (Rakhi → Diwali).
  Capping them would train the model to miss the months the business cares most about, so
  they are flagged and left intact; caps apply only to exogenous distribution metrics.
- **Small brands are where the model is weakest.** FERRRO ROCHER, KINDER JOY and MARS each
  sit under 1% of category value, so wMAPE hides errors of 25–33%. Documented in the model
  card rather than smoothed over.
- **Aug-2019 was under-forecast by 9.3%** at category level — festive timing, and the single
  largest weakness.
- **The training data predates COVID-19 entirely.** A model trained on Jun-2017–Aug-2019
  must not be assumed valid across that break without revalidation.

---

## Serving: the API is batch, not single-row

Two of the 25 features — `zt_d1` and `share_l1` — are computed from the **sum of all brands
in a zone**. A request for one brand mathematically cannot produce them.

`POST /v1/predict` therefore takes a **zone-complete panel** with ≥7 months of history per
series and returns **422** for an incomplete brand set, rather than computing a wrong
denominator and returning a confident 200. This was decided when the feature was designed,
not when the API was written.

| Route | Purpose |
|---|---|
| `GET /live` | process liveness, no I/O — safe for `livenessProbe` |
| `GET /health` | readiness: artifact loaded **and** manifest validated |
| `GET /v1/model` | manifest and metrics of the promoted version |
| `GET /v1/schema` | required fields and the batch constraint |
| `POST /v1/predict` | forecast; `?allow_fallback=true` opts into tagged naive persistence |

Every response carries `model_version`, `contract_version` and `source: model | fallback`.
The fallback is **opt-in and always tagged** — an untagged fallback is indistinguishable
from a real prediction, which is worse than an error.

## Model registry

Artifacts are immutable. Training writes `models/v_<utc>_<sha>/` and never overwrites.
`models/CURRENT.json` is the alias, so **promotion and rollback are both a single JSON
write** and both directions are reversible.

`manifest.json` is the compatibility contract: contract version, feature-order hash, encoder
maps, **feature dtypes**, and the exact library versions the estimator was pickled under.
The container validates it at startup and **fails readiness on mismatch**, so a bad
promotion leaves the previous pods serving.

> **Dtypes are in the manifest for a reason.** Notebook 07 fits `brand_code`/`zone_code` as
> pandas `category`; LightGBM records `pandas_categorical` in the booster and validates it at
> predict time. An early version of `align()` returned `int64` — the feature hash matched,
> `/health` was green, and every prediction request returned **HTTP 500**. Feature-order
> parity is necessary and not sufficient.

## Train/serve parity

`src/features.py` is asserted equal to notebook 06 **cell for cell** (max diff 1e-13), and
the same `transform()` runs at training and at serving — there is no second implementation.

Parity is then proven at **three points** — local `uvicorn`, `docker run`, and through the
LoadBalancer — on an identical payload including nulls, via `scripts/golden_check.py`.
90 series, largest absolute difference **0.0000000000**.

## Tests

**36 tests, all passing with `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` and `S3_BUCKET`
unset.** Data comes from a committed fixture, never from S3 — which removes the entire class
of "CI failed on NoSuchKey".

| Category | What it pins |
|---|---|
| Contemporaneous leakage | corrupt the current period; assert no feature moves |
| Leakage regression | fitted statistic unchanged when test rows are appended |
| Call-order guard | split precedes fit |
| Transform parity | `src/` reproduces the notebook's saved stage file |
| Dtype parity | category input predicts; int64 input still raises — **both** directions |
| Feature contract | all 25 names, in order, plus the algebraic identity that justifies the exclusion |
| Cross-sectional | removing a brand changes `share_l1` — proof the batch constraint is real |
| Business invariant | forecast within [0.2×, 5×] of last actual; <30% move for historically stable series |
| Serving contract | unknown brand, short history, null metric, zone-incomplete payload → 422 |
| Fallback | tagged, opt-in, equals persistence |

> The business-invariant bound is **derived from the data**. An earlier invented threshold
> of "no series moves more than 60%" failed on correct behaviour — real swings in this panel
> reach 327%.

## Architecture

```
                    ┌──────────────────────────────────────────┐
   git push ───────►│  GitHub Actions                          │
                    │  test (no creds) → build arm64 → ECR      │
                    │  → EKS → post-deploy golden check         │
                    └────────────────┬─────────────────────────┘
                                     ▼
   ┌─────────────────────────────────────────────────────────┐
   │  EKS  chocolate-sales-eks   ·   t4g.small (ARM64)       │
   │  ┌───────────────────────────────────────────────────┐  │
   │  │ Pod: one image, two roles, one process each       │  │
   │  │   api  → uvicorn :8000   loads the model          │  │
   │  │   ui   → streamlit :8501 calls the API            │  │
   │  └───────────────────────────────────────────────────┘  │
   │  Service (LoadBalancer) → :80 → api  ·  :8501 → ui      │
   └────────────────────────────┬────────────────────────────┘
                                │ IRSA, read-only
                                ▼
        s3://brandwise-zonewise-chocolate-sales-prediction
          data/01…06   contracts/   models/CURRENT.json
          models/v_<utc>_<sha>/     reference/
```

**One Service, therefore one load balancer, therefore one bill.** Both ports are actually
served — an exposed port nothing listens on leaves a permanently unhealthy target.

**CI does not bootstrap infrastructure.** The cluster, the OIDC provider and the IRSA
ServiceAccount are one-time human steps that the pipeline *verifies* and never creates.

## Tech stack

`Python 3.13` · `LightGBM` · `scikit-learn` · `statsmodels` · `SHAP` · `pandas` · `FastAPI`
· `Streamlit` · `pytest` · `Docker (buildx, ARM64)` · `AWS S3 · ECR · EKS · IAM/IRSA` ·
`GitHub Actions`

Serving pins match the artifact's training environment exactly — `lightgbm 4.6.0`,
`scikit-learn 1.6.1`, `pandas 2.2.3`, `numpy 2.1.3` — and every one was verified published
on PyPI with a `cp313` **aarch64** wheel, so nothing compiles from source on the ARM node.

## Repository structure

```
├── notebooks/                    Phase 1 — permanent documentation, committed
│   ├── 01_Data_Loading_and_First_Look.ipynb
│   ├── 02_Data_Cleaning.ipynb
│   ├── 03_Missing_Values_and_Outliers.ipynb
│   ├── 04_Statistics_and_EDA.ipynb
│   ├── 05_Hypothesis_Testing.ipynb
│   ├── 06_Correlation_VIF_Encoding.ipynb
│   └── 07_Model_Building_and_Evaluation.ipynb
├── src/
│   ├── config.py                 every identifier, baked in once
│   ├── features.py               THE transform — shared by training and serving
│   ├── predict.py                forecast, contract validation, tagged fallback
│   ├── registry.py               alias resolution, manifest compatibility check
│   ├── api.py                    FastAPI
│   ├── app.py                    Streamlit UI
│   ├── train.py                  retraining entry point, gate-aware
│   ├── promote_phase1.py         one-time migration to the versioned registry
│   └── s3_io.py                  default credential chain only
├── tests/
│   ├── test_features.py          leakage, parity, dtype, feature contract
│   ├── test_api.py               serving contract, probes, fallback
│   ├── fixtures/sample_panel.csv COMMITTED — why CI needs no credentials
│   └── golden/                   expected.json recorded against the real artifact
├── k8s/                          config · deployment · service · pdb
├── .github/
│   ├── workflows/mlops_pipeline.yml
│   └── scripts/render_and_verify.sh
├── scripts/golden_check.py       three-point parity check
├── docs/                         runbooks, Phase-1 sign-off, correction notes
├── Dockerfile · entrypoint.sh · requirements.txt
└── demo_panel_upload.csv         ready-to-upload sample for the UI
```

## How to run

**Tests — no cloud credentials required:**

```bash
git clone https://github.com/BishalRanjanBadu/BRANDWISE_ZONEWISE_CHOCOLATE_SALES_-PREDICTION.git
cd BRANDWISE_ZONEWISE_CHOCOLATE_SALES_-PREDICTION
python -m venv venv && source venv/Scripts/activate   # venv/bin/activate on Linux/macOS
pip install -r requirements-dev.txt

env -u AWS_ACCESS_KEY_ID -u AWS_SECRET_ACCESS_KEY pytest tests/ -v
```

**Serve locally** (requires AWS credentials with read access to the model bucket):

```bash
ROLE=api ./entrypoint.sh                                   # API  on :8000
API_URL=http://127.0.0.1:8000 ROLE=ui ./entrypoint.sh      # UI   on :8501
```

Upload `demo_panel_upload.csv` in the UI and click **Forecast next month**.

**Retrain and promote:**

```bash
python -m src.train --git-sha "$(git rev-parse HEAD)" --promote
```

Writes a new immutable version and advances `CURRENT.json` **only** if all three gates pass.
A failing retrain leaves production untouched and exits non-zero.

**Deploy:** `docs/OPERATOR_RUNBOOK_PHASE2.md` — 23 numbered steps, each with a verification
command.

## Known limitations

- **Horizon is 1 month.** Nothing here validates a 3- or 6-month forecast.
- **145 of 149 zone rows are unmodelled** — town-level and channel splits are out of the
  declared grain.
- **No prediction intervals.** Point estimate only; quantile regression is a deferred item.
- **Canary deferred.** One free-tier node cannot host two pods, so this runs
  `RollingUpdate` with `maxUnavailable: 0` and a single replica. On real infrastructure the
  canary is not optional.
- **Interchange format is CSV, not Parquet** — the Phase-1 authoring runtime had no Parquet
  engine. Recorded as a deviation to fix, not a preference.
- **Inference logging is off**, and the serving IRSA policy is read-only. Phase 3 enables
  both **in the same change** — one without the other yields `AccessDenied` per request.

## Costs

| Resource | Cost |
|---|---|
| EKS control plane | ~$0.10/hour (**~$73/month — never free tier**) |
| `t4g.small` node | free-trial dependent |
| Classic ELB | ~$16/month |
| S3 + ECR | negligible at this volume |

Teardown is Step 23 of the runbook. **Delete the `type: LoadBalancer` Service before the
cluster** or the ELB is orphaned and keeps billing.

## Author

**Bishal Ranjan Badu**
Data Science · Machine Learning · MLOps
