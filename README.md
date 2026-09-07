# Brand-wise / Zone-wise Chocolate Sales Prediction

Monthly value forecast (Rs. Lakhs) for the Indian chocolate category at
**brand x zone** grain, served from EKS behind a FastAPI + Streamlit container.

## Automation model

**Trigger 1 — code:** `git push` -> GitHub Actions -> test (no credentials) ->
build ARM64 -> ECR -> EKS -> post-deploy golden-payload check.

**Trigger 2 — data:** *(Phase 3, not yet built)* EventBridge -> Lambda -> drift
detection -> retrain -> evaluate -> promote alias -> pods reload.

> **CI does not bootstrap infrastructure.** The EKS cluster, the OIDC provider and
> the IRSA ServiceAccount are one-time human steps that the pipeline *verifies*
> and never creates. See `OPERATOR_RUNBOOK_PHASE2.md`.

## Model

| | |
|---|---|
| Estimator | LightGBM, one pooled model across all 72 series |
| Target | `value_lakhs`, modelled as the **log first-difference** |
| Grain | 18 brands x 4 zones (North/East/West/South, U+R) |
| Horizon | 1 month |
| All-India | bottom-up sum of the four zones (additivity verified to 7.5e-10) |
| Holdout | Mar-2019 .. Aug-2019, All-India brand wMAPE **5.30%** |
| Selection | lowest wMAPE on 5 expanding train-window CV folds; the holdout is scored once, for reporting |

SARIMA was benchmarked and **rejected**: competitive on the holdout (4.68%) but
worst of all candidates on CV (40.9%), because 21 training months cannot estimate
a seasonal ARMA. Revisit at ~36 months. Full reasoning in notebook 07 and
`PHASE1_HANDOFF.md`.

## The API is batch, not single-row

Two of the 28 features — `zt_d1` and `share_l1` — are computed from the **sum of
all brands in a zone**. A single-brand request cannot produce them. `POST
/v1/predict` therefore takes a zone-complete panel with >= 7 months of history per
series, and rejects an incomplete brand set with 422 rather than serving a
confident wrong number.

## Endpoints

| route | purpose |
|---|---|
| `GET /live` | process liveness, no I/O — safe for `livenessProbe` |
| `GET /health` | readiness: artifact loaded **and** manifest validated |
| `GET /v1/model` | manifest and metrics of the promoted version |
| `GET /v1/schema` | required fields and the batch constraint |
| `POST /v1/predict` | forecast; `?allow_fallback=true` opts into tagged naive persistence |

Every response carries `model_version`, `contract_version` and
`source: model \| fallback`. An untagged fallback would be indistinguishable from a
real prediction, so it is always tagged.

## Registry

Artifacts are immutable. Training writes `models/v_<utc>_<sha>/` and never
overwrites. `models/CURRENT.json` is the alias; **promotion and rollback are both
a single JSON write**, so both directions are reversible.

`manifest.json` is the compatibility contract: contract version, feature-order
hash, encoder maps, and the exact library versions the estimator was pickled
under. The container validates it at load and **fails readiness on mismatch** — a
bad promotion leaves the previous pods serving.

## Layout

```
src/        features.py  <- THE transform, shared by training and serving
            api.py  app.py  predict.py  registry.py  s3_io.py  train.py
            promote_phase1.py  config.py
tests/      test_features.py  test_api.py  conftest.py
            fixtures/sample_panel.csv   <- COMMITTED; CI needs no credentials
            golden/                     <- expected.json recorded in runbook step 7
k8s/        config.yml  deployment.yml  service.yml  pdb.yml
scripts/    golden_check.py  serve_local.py  local_s3.py (dev only)
notebooks/  01..07  <- Phase 1, permanent documentation
```

## Local development

```bash
python -m venv venv && source venv/Scripts/activate
pip install -r requirements-dev.txt
env -u AWS_ACCESS_KEY_ID -u AWS_SECRET_ACCESS_KEY pytest tests/ -v
ROLE=api ./entrypoint.sh                       # API on :8000
API_URL=http://localhost:8000 ROLE=ui ./entrypoint.sh   # UI on :8501
```

## Train / serve parity

`src/features.py` is asserted equal to notebook 06 cell-for-cell, and the same
`transform()` runs at training and at serving. Parity is then proven at three
points — local uvicorn, `docker run`, and through the LoadBalancer — on an
identical payload including nulls, via `scripts/golden_check.py`.

## Costs

The EKS control plane is **~$73/month and is not free tier**, plus ~$16/month for
the load balancer. Teardown is Step 22 of the Phase 2 runbook. Delete the
`type: LoadBalancer` Service *before* the cluster or the ELB is orphaned and keeps
billing.
