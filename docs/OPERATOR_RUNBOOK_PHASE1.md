# OPERATOR RUNBOOK — PHASE 1
## Brand-wise / Zone-wise Chocolate Sales Prediction

Shell: **Git Bash on Windows**. Every value below is real — nothing to substitute except the two
credential values you type yourself in Step 4, and one file path you select from a prompt in Step 7.

**Scope of this phase:** local environment → AWS credentials → S3 bucket and security → raw data
upload and header verification → Python environment → notebook execution 01–07 → artifact review →
commit and push. Nothing is deployed. See "What is NOT in this phase" at the end.

**If a step fails: read the actual error before re-running it.** Do not blind-retry an identical
command. Step 17 lists the diagnosis order for the two failures that actually happen.

---

## Step 0 — Session variables

Run this once per terminal session. Everything below references these.

```bash
export AWS_REGION=ap-south-2
export AWS_ACCOUNT_ID=117211782845
export S3_BUCKET=brandwise-zonewise-chocolate-sales-prediction
export PROJECT_DIR="$HOME/projects/chocolate-sales"
export RAW_KEY="raw/BRAND_WISE_CHOCOLATE__ALL_INDIA.xlsx"
export PY=$(command -v python3 || command -v python)
```

**Verify:**
```bash
echo "region=$AWS_REGION account=$AWS_ACCOUNT_ID bucket=$S3_BUCKET"
echo "python=$PY"; "$PY" --version
```
Expect a Python 3.11 or 3.12 version string. Git Bash has no `python3`, which is why `$PY` is
resolved rather than assumed.

---

## Step 1 — Confirm the toolchain exists

```bash
aws --version
git --version
"$PY" -m pip --version
```

**Verify:** three version lines, no "command not found". If `aws` is missing, install the AWS CLI
v2 for Windows from the AWS console download page and **reopen Git Bash** — the PATH is only
refreshed in a new shell.

---

## Step 2 — Create the project directory and clone the repo

```bash
mkdir -p "$PROJECT_DIR"
cd "$PROJECT_DIR"
git clone https://github.com/BishalRanjanBadu/BRANDWISE_ZONEWISE_CHOCOLATE_SALES_-PREDICTION.git repo
cd repo
```

**Verify:**
```bash
pwd
git remote -v
git log --oneline -1
```
If the repo is empty, `git log` errors with "does not have any commits yet" — that is expected on
a fresh repo and is not a failure.

---

## Step 3 — Directory skeleton

```bash
cd "$PROJECT_DIR/repo"
mkdir -p notebooks runs artifacts
```

**Verify:**
```bash
ls -d notebooks runs artifacts && echo "SKELETON OK"
```

---

## Step 4 — Configure AWS credentials

**You type the two credential values. They are never printed by anything in this runbook and never
written into the repository.** `aws configure` writes them to `~/.aws/credentials`, which lives
outside `$PROJECT_DIR` and therefore cannot be committed by accident.

```bash
aws configure
```

Answer the four prompts:
- `AWS Access Key ID` — paste the key ID only, **not** the whole `AWS_ACCESS_KEY_ID=AKIA...` line
- `AWS Secret Access Key` — paste the secret only
- `Default region name` — type `ap-south-2`
- `Default output format` — type `json`

**Verify:**
```bash
aws sts get-caller-identity
```
Expect JSON containing `"Account": "117211782845"`. If this errors, go to Step 17 before doing
anything else.

**Also confirm no environment variable is shadowing the file** — env vars take precedence over
`~/.aws/credentials`, and a stale one silently wins:
```bash
env | grep -i AWS_ || echo "no AWS_ env vars set (this is what you want, apart from AWS_REGION)"
aws configure list
```
In `aws configure list`, the `Type` column names the winning source. It should say
`shared-credentials-file` for `access_key` and `secret_key`.

---

## Step 5 — Confirm the S3 bucket exists (and its region)

```bash
aws s3api head-bucket --bucket "$S3_BUCKET"
echo "head-bucket exit code: $?"
aws s3api get-bucket-location --bucket "$S3_BUCKET"
```

**Verify:** exit code `0`, and `"LocationConstraint": "ap-south-2"`.

**Only if `head-bucket` returned a 404** (the bucket does not exist yet):
```bash
aws s3api create-bucket \
  --bucket "$S3_BUCKET" \
  --region "$AWS_REGION" \
  --create-bucket-configuration LocationConstraint="$AWS_REGION"
aws s3api get-bucket-location --bucket "$S3_BUCKET"
```
`ap-south-2` is not `us-east-1`, so `--create-bucket-configuration` is mandatory; omitting it
fails with `IllegalLocationConstraintException`.

---

## Step 6 — Bucket security controls

This bucket holds competitor market-share data. It contains no personal data (see the Phase-1
handoff, §7), but it is commercially confidential, so the controls are the same.

```bash
aws s3api put-public-access-block \
  --bucket "$S3_BUCKET" \
  --public-access-block-configuration \
  BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true

aws s3api put-bucket-encryption \
  --bucket "$S3_BUCKET" \
  --server-side-encryption-configuration '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"},"BucketKeyEnabled":true}]}'

aws s3api put-bucket-versioning \
  --bucket "$S3_BUCKET" \
  --versioning-configuration Status=Enabled
```

**Verify:**
```bash
aws s3api get-public-access-block --bucket "$S3_BUCKET"
aws s3api get-bucket-encryption --bucket "$S3_BUCKET"
aws s3api get-bucket-versioning --bucket "$S3_BUCKET"
```
Expect all four public-access flags `true`, `SSEAlgorithm: AES256`, and `Status: Enabled`.

**A note on the encryption choice, so it is a decision and not an accident.** This uses SSE-S3
(`AES256`), which is free. SSE-KMS with a customer-managed key gives you per-key access control and
CloudTrail records of every decrypt, at roughly $1/month for the key plus per-request charges.
For competitor share data with a single consumer, SSE-S3 is defensible. If you want KMS, do it now
rather than after objects exist — existing objects are not re-encrypted by changing the default.

Versioning is on because Step 8 overwrites stage objects on every notebook re-run, and versioning
is what lets you recover the previous one.

---

## Step 7 — Upload the raw workbook

Locate the file on disk first rather than assuming where it is:

```bash
find /c/Users -maxdepth 5 -name "BRAND_WISE_CHOCOLATE__ALL_INDIA.xlsx" -print 2>&1 | grep -v "Permission denied"
```

Take the path it prints and set it through a prompt — this avoids pasting a path directly into a
command, which is how a literal path gets baked into a shell history and then re-used wrongly:

```bash
read -e -p "Full path to the workbook: " RAW_LOCAL
export RAW_LOCAL
ls -la "$RAW_LOCAL"
```

**Verify** the file is present and roughly 27 MB, then upload:

```bash
aws s3 cp "$RAW_LOCAL" "s3://$S3_BUCKET/$RAW_KEY"
```

**Verify:**
```bash
aws s3api head-object --bucket "$S3_BUCKET" --key "$RAW_KEY" \
  --query '{size:ContentLength, modified:LastModified}'
```
Expect a size near `27210958`.

---

## Step 8 — Verify the raw object's contents, not just its key name

A key named `raw/` is not evidence that the object is raw. This reads the actual header from S3
and confirms the structure the notebooks expect.

```bash
cd "$PROJECT_DIR/repo"
cat > verify_raw.py << 'EOF'
import io, os, boto3, pandas as pd
s3 = boto3.client("s3", region_name=os.environ["AWS_REGION"])
obj = s3.get_object(Bucket=os.environ["S3_BUCKET"], Key=os.environ["RAW_KEY"])
df = pd.read_excel(io.BytesIO(obj["Body"].read()), sheet_name="Sheet1", dtype=object, nrows=25)
df.columns = [str(c).strip() for c in df.columns]
print("shape (first 25 rows):", df.shape)
print("columns:", list(df.columns))
MONTHS = ["JUN17","JUL17","AUG17","SEP17","OCT17","NOV17","DEC17","JAN18","FEB18","MAR18",
          "APR18","MAY18","JUN18","JUL18","AUG18","SEP18","OCT18","NOV18","DEC18","JAN19",
          "FEB19","MAR19","APR19","MAY19","JUN19","JUL19","AUG19"]
missing = [m for m in MONTHS if m not in df.columns]
assert not missing, f"missing monthly columns: {missing}"
assert {"BRAND","ZONES","METRICS"}.issubset(df.columns), "header row is not the expected cross-tab"
print("first metric block:", df["METRICS"].head(20).tolist())
print("RAW HEADER VERIFIED: 27 monthly columns present, cross-tab structure intact")
EOF
"$PY" verify_raw.py
```

**Verify:** the script ends with `RAW HEADER VERIFIED`. If it raises on a missing monthly column,
the object in `raw/` is not the file you think it is — stop and reconcile before continuing, because
every downstream stage inherits the error.

---

## Step 9 — Python environment

```bash
cd "$PROJECT_DIR/repo"
"$PY" -m venv venv
source venv/Scripts/activate
python -m pip install --upgrade pip
```

On Git Bash the activate script is at `venv/Scripts/activate`, not `venv/bin/activate`.

**Verify:**
```bash
which python
python --version
```
`which python` must point inside `$PROJECT_DIR/repo/venv`.

---

## Step 10 — Pinned dependencies

Every version below was verified as published on PyPI with a Windows wheel, and is the exact
version the notebooks were executed under. Do **not** regenerate this file from `pip freeze` on
another machine — a local environment can hold pre-release builds that do not exist on the index.

```bash
cd "$PROJECT_DIR/repo"
cat > requirements-phase1.txt << 'EOF'
# Phase 1 — notebook experimentation. Exact pins, all verified on PyPI.
pandas==3.0.2
numpy==2.4.4
scipy==1.17.1
scikit-learn==1.8.0
xgboost==3.4.1
lightgbm==4.7.0
statsmodels==0.15.0
shap==0.52.0
matplotlib==3.10.8
openpyxl==3.1.5
boto3==1.43.87
notebook==7.6.2
ipykernel==7.3.0
nbconvert==7.17.1
nbstripout==0.9.1
EOF
pip install -r requirements-phase1.txt
```

**Verify:**
```bash
python -c "
import pandas, numpy, sklearn, xgboost, lightgbm, statsmodels, shap, boto3, openpyxl
for m in (pandas, numpy, sklearn, xgboost, lightgbm, statsmodels, shap, boto3, openpyxl):
    print(f'{m.__name__:14s} {m.__version__}')
"
pip check
```
Expect `pandas 3.0.2`, `sklearn 1.8.0`, `xgboost 3.4.1` and `pip check` reporting no broken
requirements. **These exact versions become the train/serve parity contract for Phase 2** — the
model pickle produced in Step 13 is only safe to unpickle under them.

---

## Step 11 — Place the notebooks

Copy the seven `.ipynb` files from the Phase-1 delivery into `notebooks/`.

**Verify:**
```bash
cd "$PROJECT_DIR/repo"
ls -1 notebooks/*.ipynb | wc -l
ls -1 notebooks/
grep -l "YOUR_AWS_ACCESS_KEY_HERE" notebooks/*.ipynb | wc -l
```
Expect `7`, the seven filenames `01_…` to `07_…`, and `7` again on the last command — every
notebook must carry the placeholder S3 cell, not a real key.

---

## Step 12 — Confirm the notebooks will authenticate through `~/.aws`

The S3 cell leaves `AWS_ACCESS_KEY_ID` at its placeholder unless an environment variable overrides
it, in which case boto3 falls through to the shared credentials file. Confirm that is what happens:

```bash
cd "$PROJECT_DIR/repo"
python -c "
import boto3
s = boto3.Session()
c = s.get_credentials()
print('resolved credential source:', c.method)
print('region:', s.region_name)
print('identity:', boto3.client('sts').get_caller_identity()['Account'])
"
```

**Verify:** `resolved credential source: shared-credentials-file` and account `117211782845`. If
it says `env`, an environment variable is shadowing the file — unset it before continuing:
`unset AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY`.

There is deliberately no `.env` file in this phase. A credential in `.env` both leaks and shadows.

---

## Step 13 — Execute the notebooks, in order

Each notebook reads its input stage from S3 and writes its output stage back, so the order is not
optional. Executed copies land in `runs/` so that `notebooks/` stays output-free.

Run them one at a time and check the S3 object after each, rather than running all seven and
debugging backwards.

```bash
cd "$PROJECT_DIR/repo"
jupyter nbconvert --to notebook --execute --ExecutePreprocessor.timeout=3600 \
  --output-dir runs notebooks/01_Data_Loading_and_First_Look.ipynb
aws s3 ls "s3://$S3_BUCKET/data/01_raw_loaded.csv"
aws s3 ls "s3://$S3_BUCKET/contracts/schema_v1.json"
```

```bash
jupyter nbconvert --to notebook --execute --ExecutePreprocessor.timeout=3600 \
  --output-dir runs notebooks/02_Data_Cleaning.ipynb
aws s3 ls "s3://$S3_BUCKET/data/02_cleaned.csv"
```

```bash
jupyter nbconvert --to notebook --execute --ExecutePreprocessor.timeout=3600 \
  --output-dir runs notebooks/03_Missing_Values_and_Outliers.ipynb
aws s3 ls "s3://$S3_BUCKET/data/03_imputed.csv"
aws s3 ls "s3://$S3_BUCKET/models/imputer_outlier_maps.pkl"
```

```bash
jupyter nbconvert --to notebook --execute --ExecutePreprocessor.timeout=3600 \
  --output-dir runs notebooks/04_Statistics_and_EDA.ipynb
aws s3 ls "s3://$S3_BUCKET/data/04_eda_complete.csv"
```

```bash
jupyter nbconvert --to notebook --execute --ExecutePreprocessor.timeout=3600 \
  --output-dir runs notebooks/05_Hypothesis_Testing.ipynb
aws s3 ls "s3://$S3_BUCKET/data/05_hypothesis_done.csv"
```

```bash
jupyter nbconvert --to notebook --execute --ExecutePreprocessor.timeout=3600 \
  --output-dir runs notebooks/06_Correlation_VIF_Encoding.ipynb
aws s3 ls "s3://$S3_BUCKET/data/06_encoded_tree.csv"
aws s3 ls "s3://$S3_BUCKET/data/06_encoded_linear.csv"
aws s3 ls "s3://$S3_BUCKET/models/preprocessor.pkl"
```

Notebook 07 takes roughly **4–6 minutes** — it fits 1,296 ARIMA models for the classical benchmark,
plus 70 randomised-search fits and the SARIMA cross-validation.

```bash
jupyter nbconvert --to notebook --execute --ExecutePreprocessor.timeout=3600 \
  --output-dir runs notebooks/07_Model_Building_and_Evaluation.ipynb
aws s3 ls "s3://$S3_BUCKET/models/"
aws s3 ls "s3://$S3_BUCKET/reference/"
```

**Verify the full chain in one pass:**
```bash
for K in data/01_raw_loaded.csv data/02_cleaned.csv data/03_imputed.csv \
         data/04_eda_complete.csv data/05_hypothesis_done.csv \
         data/06_encoded_tree.csv data/06_encoded_linear.csv data/06_feature_names.csv \
         contracts/schema_v1.json models/preprocessor.pkl models/imputer_outlier_maps.pkl \
         models/best_model.pkl models/feature_names.pkl models/model_metrics.json \
         models/model_card.md reference/training_reference.csv; do
  SIZE=$(aws s3api head-object --bucket "$S3_BUCKET" --key "$K" --query ContentLength --output text)
  echo "OK   $K   $SIZE bytes"
done
echo "ALL 16 STAGE OBJECTS PRESENT"
```
If any key is missing, `head-object` fails and prints the reason and the loop stops there. That is
intentional — a masked error here would let you proceed on an incomplete chain.

---

## Step 14 — Review the results

```bash
cd "$PROJECT_DIR/repo"
aws s3 cp "s3://$S3_BUCKET/models/model_metrics.json" artifacts/model_metrics.json
aws s3 cp "s3://$S3_BUCKET/models/model_card.md"     artifacts/model_card.md
aws s3 cp "s3://$S3_BUCKET/contracts/schema_v1.json" artifacts/schema_v1.json

python -c "
import json
m = json.load(open('artifacts/model_metrics.json'))
print('selected model  :', m['model'])
print('selection rule  :', m['selection_rule'])
print('CV scores       :', m['cv_scores'])
print('AI-brand wMAPE  :', m['metrics_all_india_brand']['wmape'])
print('naive-rel MASE  :', m['naive_relative_mase'])
print('beats naive     :', m['beats_naive_brands'])
"
```

**Verify against the Phase-1 handoff.** Expected: `xgboost_tuned` selected, All-India brand wMAPE
near `4.52`, naive-relative MASE near `0.84`, `14/18` brands beating naive. Small differences in
the last decimal across machines are expected — `n_jobs=-1` with threaded boosters is not
bit-reproducible, which is recorded rather than papered over. A difference in the **first**
decimal is not expected and should be investigated before sign-off.

---

## Step 15 — Strip outputs, commit, push

Notebooks are permanent documentation and **are** committed. Outputs are stripped first, because
embedded plot data bloats the repository past usefulness.

```bash
cd "$PROJECT_DIR/repo"
nbstripout --install
nbstripout notebooks/*.ipynb
```

```bash
cat > .gitignore << 'EOF'
venv/
runs/
__pycache__/
*.pyc
.env
.ipynb_checkpoints/
# data and model binaries live in S3, not in git
*.pkl
*.xlsx
# NOTE: *.ipynb is deliberately NOT ignored - the notebooks are the phase deliverable.
# NOTE: artifacts/*.json and *.md ARE committed - they are the sign-off record.
EOF
```

**Scan for credentials before pushing.** Read any hit before acting on it:
```bash
git add -A
git diff --cached --name-only
git diff --cached | grep -nE "AKIA[0-9A-Z]{16}|aws_secret_access_key[[:space:]]*=" \
  && echo ">>> STOP: inspect the lines above before committing" \
  || echo "credential scan: clean"
```

```bash
git commit -m "Phase 1: notebooks 01-07, data contract v1, model card, XGBoost baseline (AI-brand wMAPE 4.52%)"
git push -u origin main
```

**Verify:**
```bash
git log --oneline -1
git show --stat HEAD | head -25
git show HEAD:notebooks/07_Model_Building_and_Evaluation.ipynb | head -5
```
The last command reads the file **as it exists in the repository**, which is the only version that
matters. Confirming your working copy is not the same thing.

---

## Step 16 — Cost and teardown

Phase 1 creates S3 objects and nothing else. **No compute is left running, and nothing bills
continuously beyond storage.**

| item | approximate monthly cost |
|---|---|
| ~30 MB raw workbook + ~15 MB stage objects, `ap-south-2` | well under $0.05 |
| Versioning (old versions retained on re-runs) | grows slowly; see below |
| Requests | negligible at this volume |

Re-running the notebooks creates a new version of every stage object. After many runs, clear the
non-current versions:
```bash
aws s3api list-object-versions --bucket "$S3_BUCKET" --prefix data/ \
  --query 'length(Versions)' --output text
```

**Teardown** — only if you are abandoning the project. This is irreversible:
```bash
aws s3 rm "s3://$S3_BUCKET" --recursive
aws s3api delete-bucket --bucket "$S3_BUCKET" --region "$AWS_REGION"
```
With versioning enabled, `s3 rm --recursive` leaves delete markers and old versions behind, and
`delete-bucket` will then fail with `BucketNotEmpty`. Remove versions first if you hit that.

---

## Step 17 — Failure diagnosis

**Read the real error at its source before acting. Never blind-retry.**

### `InvalidAccessKeyId` / `InvalidClientTokenId` / `SignatureDoesNotMatch`

The signing key is not the one you think it is. In order:
```bash
env | grep -i AWS_
aws configure list
aws sts get-caller-identity
```
`aws configure list` names the winning source in the `Type` column. Environment variables beat
`~/.aws/credentials`, so a stale `AWS_ACCESS_KEY_ID` silently overrides a freshly configured key.

Then check the file's **structure**, not just its content:
```bash
head -1 ~/.aws/credentials
awk -F= '/aws_access_key_id/{gsub(/[ "]/,"",$2); print "id_len="length($2)}
         /aws_secret_access_key/{gsub(/[ "]/,"",$2); print "secret_len="length($2)}' ~/.aws/credentials
```
Expect a `[default]` header, `id_len=20`, `secret_len=40`. A length of 40+ on the ID means the
paste included the `AWS_ACCESS_KEY_ID=` prefix — the common Git Bash paste error.

If `get-caller-identity` returns an Arn ending in `:root`, you are using root keys. They cannot be
scoped by any policy and log with no per-principal attribution. Create an IAM user with S3 access
for this project instead.

### `AccessDenied` on `s3api put-bucket-encryption` or `put-public-access-block`

Your IAM principal lacks `s3:PutEncryptionConfiguration` or `s3:PutBucketPublicAccessBlock`. This
is a **policy block, not a bug** — attach the permission or have an admin apply Step 6. Do not
continue with an unencrypted, publicly-accessible bucket and plan to fix it later.

### A notebook fails mid-execution

`nbconvert` writes the partially-executed notebook to `runs/`. Open it and read the traceback in
the failing cell — do not re-run the notebook first:
```bash
python -c "
import nbformat, sys
nb = nbformat.read('runs/06_Correlation_VIF_Encoding.ipynb', as_version=4)
for i, c in enumerate(nb.cells):
    for o in c.get('outputs', []):
        if o.get('output_type') == 'error':
            print('cell', i, o['ename'], o['evalue'])
            print('\n'.join(o.get('traceback', [])[-8:]))
"
```
Change the filename to whichever notebook failed. Then re-run **that notebook only** — the earlier
stages are already in S3 and do not need regenerating.

### Removing the virtual environment

```bash
deactivate
rm -rf "$PROJECT_DIR/repo/venv"
```
`deactivate` first, always. Deleting the venv you are standing inside removes the `python` on your
PATH and the next command fails confusingly.

---

## What is NOT in this phase

Deliberately absent, and **not** an oversight:

- **No Docker image, no ECR repository, no EKS cluster, no Kubernetes manifests.** Phase 2.
- **No FastAPI service, no `src/` package, no tests.** The notebooks are the deliverable; the
  productionisation mapping (which notebook becomes which `.py`) is decided at Phase-2 start.
- **No CI/CD, no GitHub Actions workflow, no repository secrets or variables.** Phase 2. Note in
  advance: **CI cannot bootstrap infrastructure.** The cluster, the OIDC provider and the IRSA
  ServiceAccounts are one-time human steps that the pipeline verifies but never creates.
- **No IRSA role, no IAM policy for pods.** Phase 2, once the cluster name exists.
- **No drift job, no retraining pipeline, no CloudWatch alarms, no SNS topic.** Phase 3.
- **No `models/CURRENT.json` alias, no immutable version directories.** Phase 1 writes
  `models/best_model.pkl` flat. The versioned registry layout
  (`models/v_<utc>_<gitsha>/` plus an alias) is introduced in Phase 2 alongside the manifest
  compatibility check — introducing it now would mean building a promotion mechanism with nothing
  to promote to.
- **No prediction intervals.** Open item 5 in the Phase-1 handoff; a decision, not a gap.

---

## Exit criteria for Phase 1

- [ ] All 16 S3 stage objects present (Step 13 verification loop)
- [ ] `model_metrics.json` reproduces the handoff figures to the first decimal (Step 14)
- [ ] Notebooks committed with outputs stripped, credential scan clean (Step 15)
- [ ] MASE gate ruling recorded — option (a) or (b) from the handoff §6
- [ ] Sign-off table in `PHASE1_HANDOFF.md` §11 filled in

**Phase 2 does not begin until the last two are done.**
