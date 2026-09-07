# START HERE

Complete project: Phase 1 (notebooks, signed off) and Phase 2 (serving,
containerisation, CI/CD). This is the only file you need to read first.

**Everything is already baked in.** Bucket `brandwise-zonewise-chocolate-sales-prediction`,
region `ap-south-2`, account `117211782845`, ECR repo
`brandwise-zonewise-chocolate-sales-prediction`, cluster `chocolate-sales-eks`,
namespace `chocolate-prod`. The only values you ever type are two AWS credentials
and two GitHub secrets.

---

## Do not improvise the setup — follow the runbook

Open **`docs/OPERATOR_RUNBOOK_PHASE2.md`** and start at **Step 0**. It is 23
numbered steps, each with a verification command, and it already accounts for the
things that have bitten this project: a nested folder that hides `.github` from
CI, a missing venv, Windows resolving `localhost` to IPv6, and a foreground server
dying with its terminal.

The first three steps matter most and are easy to get wrong:

- **Step 1 — clean slate.** Everything goes **directly into `3.DEPLOYMENT`**,
  which is your repo root. A nested `chocolate-sales/` folder means GitHub
  Actions never sees `.github/workflows/` and CI silently never runs. Step 1 also
  deletes any earlier copy — a stale one contains the 28-feature `features.py`
  that caused an HTTP 500.
- **Step 2 — copy with `cp -r "$SRC"/. .`** The trailing `/.` is what carries the
  dotfiles `.github`, `.gitignore` and `.dockerignore`. Without it they are
  silently left behind.
- **Step 4 — recreate the venv**, since Step 1 removed it.

The single check that tells you the copy is correct:

```bash
cd "$PROJECT_DIR"
source venv/Scripts/activate
python -c "
from src.features import FEATURE_ORDER, feature_hash
print('features:', len(FEATURE_ORDER), '| hash:', feature_hash(FEATURE_ORDER))
"
env -u AWS_ACCESS_KEY_ID -u AWS_SECRET_ACCESS_KEY pytest tests/ -q
```

**Expect `features: 25 | hash: 48c939942ca4ef6a` and `36 passed`.**

- 28 features means an old `features.py` — the copy did not land.
- 34 passed means the pre-fix test files — the dtype regression tests are missing,
  and Step 8 will return HTTP 500.

The first step that touches AWS is **Step 6**, which migrates your flat
`models/best_model.pkl` into an immutable versioned prefix with a manifest. Until
that runs, `/health` returns 503 and there is no rollback path.

> ### Before Step 13
> **The EKS control plane costs ~$0.10/hour (~$73/month) from the moment the
> cluster exists, and it is not free tier.** Add ~$16/month for the load
> balancer. Step 22 is teardown — delete the LoadBalancer Service *before* the
> cluster or the ELB is orphaned and keeps billing.

---

## What is in this archive

```
notebooks/        01..07   Phase 1. Permanent documentation, committed, not gitignored.
src/              application code. features.py is THE transform, shared by
                  training and serving — there is no second implementation.
tests/            34 tests + the committed fixture that lets CI run with no credentials
k8s/              manifests, sized for one free-tier t4g.small node
.github/          workflow + the render-verify script
scripts/          golden-payload parity check, and dev-only local helpers
docs/             the runbooks, the Phase-1 sign-off, and the correction note
Dockerfile, entrypoint.sh, requirements.txt, requirements-dev.txt
README.md         architecture and design decisions
```

## Documents, in reading order

| file | what it is |
|---|---|
| `START_HERE.md` | this file |
| `README.md` | architecture, endpoints, registry design, why the API is batch |
| `docs/OPERATOR_RUNBOOK_PHASE2.md` | the 22 steps you will actually execute |
| `docs/PHASE1_HANDOFF.md` | signed-off Phase 1: results, gate, deviations |
| `docs/FIX_NOTES.md` | the 25-vs-28 feature correction, in full |
| `docs/OPERATOR_RUNBOOK_PHASE1.md` | how the notebooks were run. Reference only — Phase 1 is done. |

## Three things worth knowing before you start

**The API is batch, not single-row.** Two of the 25 features are computed from
the sum of all brands in a zone, so a single-brand request cannot produce them.
`POST /v1/predict` takes a zone-complete panel with at least 7 months of history
and returns 422 for an incomplete brand set rather than serving a confident wrong
number.

**The node is ARM64.** `t4g.small` is ARM; an amd64 image fails there with
`exec format error` and no useful diagnostic. Every pin was verified to have an
aarch64 wheel, so nothing compiles from source — but the build must specify
`--platform linux/arm64`, and under QEMU on Windows that takes 8–15 minutes.

**Runbook Step 8 is not optional.** It records `tests/golden/expected.json`
against your real promoted artifact and you must commit it. The post-deploy CI
job reads it. I deliberately did not ship a pre-recorded copy, because one
generated from the test fixture would compare your live endpoint against a
different model and fail every time.

---

## Current state

| | |
|---|---|
| Phase 1 | signed off. LightGBM, 25 features, All-India brand wMAPE **5.298%**, naive-relative MASE **0.808** |
| Phase 2 | code complete, 36 tests green under your exact library versions; **not yet deployed** |
| Phase 3 | not started |

Nothing is running in AWS yet beyond S3 objects. No cluster, no ECR image, no
load balancer, so nothing is billing beyond a few cents of storage.
