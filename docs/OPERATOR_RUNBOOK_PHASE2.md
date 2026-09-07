# OPERATOR RUNBOOK — PHASE 2 (Deployment)
## Brand-wise / Zone-wise Chocolate Sales Prediction

Shell: **Git Bash on Windows** (VS Code terminal). Every value is real. The only
things you type yourself are the two AWS credential values in Step 2 and the two
GitHub secret values in Step 16.

**Order:** clean slate → place the tree → credentials → environment → tests → migrate the
Phase-1 artifact → serve locally → golden payloads → UI → Docker build → container parity →
ECR push → instance pre-check → EKS create → node headroom → namespace + IRSA → deploy →
verify live → rollback drill → GitHub + CI/CD → cost teardown.

**23 steps. Run them in order.** Every step has a verification command; the verifications
are how you find out a step failed, so do not skip them.

> ### Money warning, before you run anything
> **The EKS control plane costs ~$0.10/hour (~$73/month) from the moment the cluster
> exists, and it is not free-tier.** Add ~$16/month for the LoadBalancer. The `t4g.small`
> node may fall under a free trial; the control plane never does. Step 19 tears everything
> down. Do not skip it if this is a portfolio build.

**If a step fails: read the actual error at its source before re-running.** Step 20 gives
the diagnosis order for the failures that actually happen.

---

## Step 0 — Session variables

```bash
export AWS_REGION=ap-south-2
export AWS_ACCOUNT_ID=117211782845
export S3_BUCKET=brandwise-zonewise-chocolate-sales-prediction
export ECR_REPO=brandwise-zonewise-chocolate-sales-prediction
export ECR_REGISTRY=117211782845.dkr.ecr.ap-south-2.amazonaws.com
export EKS_CLUSTER=chocolate-sales-eks
export K8S_NAMESPACE=chocolate-prod
export SERVICE_ACCOUNT=chocolate-sa
export PROJECT_DIR="$HOME/Desktop/Bishal_/END TO END PROJECTS/ML PROJECTS/BRAND_WISE_ZONE_WISE_CHOCOLATE_SALES_PREDICTION/3.DEPLOYMENT"
export PY=$(command -v python3 || command -v python)
```

**Verify:**
```bash
echo "cluster=$EKS_CLUSTER  ecr=$ECR_REGISTRY/$ECR_REPO  ns=$K8S_NAMESPACE"
"$PY" --version
```

---

## Step 1 — Clean slate

Your repo root is `3.DEPLOYMENT` — that is where `.git` lives, and GitHub Actions only
reads `.github/workflows/` from the repo root. Everything goes **directly there**, not in
a nested folder.

Close VS Code entirely first. Windows will not delete a directory that an editor or
another terminal holds open, and that is what "Device or resource busy" means.

```bash
export PROJECT_DIR="$HOME/Desktop/Bishal_/END TO END PROJECTS/ML PROJECTS/BRAND_WISE_ZONE_WISE_CHOCOLATE_SALES_PREDICTION/3.DEPLOYMENT"
cd "$PROJECT_DIR" && pwd
ls -a
```

**Read that listing.** Remove any earlier copies — a stale one still contains the
28-feature `features.py` that caused an HTTP 500, and committing three copies of the
codebase is worse than committing none:

```bash
cd "$PROJECT_DIR"
rm -rf chocolate-sales.v1 chocolate-sales.v2 venv rendered runs
rm -rf src tests k8s scripts .github notebooks docs
rm -f Dockerfile entrypoint.sh requirements.txt requirements-dev.txt \
      .dockerignore README.md START_HERE.md FIX_NOTES.md FILE_MANIFEST.txt
ls -a
```

Only `.git`, `.gitignore` and `venv`-less directories should remain. If `rm` reports
"Device or resource busy", something still holds the folder — close every editor and
terminal, then retry.

---

## Step 2 — Extract and place the tree

Extract under `$HOME`, not `/tmp` — `/tmp` is unreliable on Git Bash for Windows. Your
browser may auto-extract or rename the download, so find what actually landed:

```bash
ls ~/Downloads/*.zip
```

```bash
rm -rf "$HOME/cs_extract" && mkdir -p "$HOME/cs_extract"
unzip -q ~/Downloads/chocolate-sales-complete.zip -d "$HOME/cs_extract"
SRC="$HOME/cs_extract/chocolate-sales"
ls "$SRC"
```

If the download saved as `chocolate-sales-complete (1).zip`, use that exact name.

Copy the **contents** to the repo root. The trailing `/.` matters — without it the
dotfiles `.github`, `.gitignore` and `.dockerignore` are silently left behind, and CI
then never runs:

```bash
cd "$PROJECT_DIR"
cp -r "$SRC"/. .
```

**Verify — all six must print OK:**
```bash
cd "$PROJECT_DIR"
test -f src/features.py                      && echo "src OK"
test -f tests/fixtures/sample_panel.csv      && echo "fixture OK"
test -f k8s/deployment.yml                   && echo "k8s OK"
test -f .github/workflows/mlops_pipeline.yml && echo ".github OK"
test -f notebooks/07_Model_Building_and_Evaluation.ipynb && echo "notebooks OK"
test -f docs/OPERATOR_RUNBOOK_PHASE2.md      && echo "docs OK"
chmod +x entrypoint.sh .github/scripts/render_and_verify.sh
```

Git Bash sometimes strips the executable bit on copy, which is why `chmod` runs
unconditionally rather than only when it looks needed.

```bash
git check-ignore -v tests/fixtures/sample_panel.csv \
  && echo "!!! FIXTURE IS GITIGNORED - CI will fail" || echo "fixture is committable: OK"
```

---

## Step 3 — Credentials

You rotated the leaked Colab key at the end of Phase 1. If not, do it now before anything else.

```bash
aws configure
aws sts get-caller-identity
```

**Verify:** `"Account": "117211782845"`. Then confirm nothing shadows the file:
```bash
env | grep -i AWS_ || echo "no AWS_ env vars (apart from AWS_REGION) — correct"
aws configure list
```
The `Type` column must read `shared-credentials-file` for `access_key`.

---

## Step 4 — Python environment

Step 1 deleted the old virtual environment, so this creates a fresh one.

```bash
cd "$PROJECT_DIR"
"$PY" -m venv venv
source venv/Scripts/activate
python -m pip install --upgrade pip
pip install -r requirements-dev.txt
```

**Verify — these must match the artifact's training versions:**
```bash
python -c "
import lightgbm, sklearn, numpy, pandas, fastapi, sys
print('python  ', '.'.join(map(str, sys.version_info[:3])))
for m in (lightgbm, sklearn, numpy, pandas, fastapi):
    print(f'{m.__name__:12s}', m.__version__)
"
pip check
```
Expect `lightgbm 4.6.0`, `sklearn 1.6.1`, `numpy 2.1.3`, `pandas 2.2.3`. A delta here is
the thing `registry.load_current()` refuses at startup — fix it now, not after a deploy.

---

## Step 5 — Tests, with NO credentials

```bash
cd "$PROJECT_DIR"
env -u AWS_ACCESS_KEY_ID -u AWS_SECRET_ACCESS_KEY -u AWS_SESSION_TOKEN -u S3_BUCKET \
  pytest tests/ -v
```

**Verify:** 36 passed. Unsetting the credentials is the point — if a test needs data it
must come from the committed fixture, never from S3. This removes the whole class of
"CI failed on NoSuchKey".

---

## Step 6 — Migrate the Phase-1 artifact into a versioned prefix

Phase 1 wrote `models/best_model.pkl` flat, which has no rollback path — a promotion
overwrites the only copy. This copies it into an immutable `models/v_<utc>_<sha>/` with
a manifest recording the Colab library versions it was trained under.

Dry run first:
```bash
cd "$PROJECT_DIR"
python -m src.promote_phase1 --dry-run
```

**Verify** the printed `estimator`, `features` count (28), `training libs`, and
`gate_passed` before writing anything. Then:
```bash
# Resolve the SHA explicitly. A `2>/dev/null || echo fallback` here would also
# swallow a genuine git failure — the specific benign case (no commits yet) is
# handled by name instead.
if git rev-parse HEAD >/dev/null 2>&1; then
  GIT_SHA=$(git rev-parse HEAD)
  echo "using commit $GIT_SHA"
else
  echo "repo has no commits yet — using 'phase1' as the version suffix"
  GIT_SHA=phase1
fi
export GIT_SHA
python -m src.promote_phase1 --git-sha "$GIT_SHA"
```

**Verify:**
```bash
aws s3 ls "s3://$S3_BUCKET/models/" --recursive | grep -E "CURRENT|v_"
aws s3 cp "s3://$S3_BUCKET/models/CURRENT.json" - | cat
```
`CURRENT.json` must name a `v_...` prefix containing `model.pkl`, `feature_names.pkl`,
`manifest.json`, `metrics.json`.

---

## Step 7 — Serve locally and prove it works

Run it **backgrounded with a log file**, in one terminal. A foreground server occupies
its terminal and does not return a prompt, and closing that terminal kills it.

```bash
cd "$PROJECT_DIR"
source venv/Scripts/activate
export ROLE=api
./entrypoint.sh > api.log 2>&1 &
sleep 8
tail -6 api.log
```

**Verify it is actually listening before anything else:**
```bash
netstat -ano | grep ":8000"
curl -sS http://127.0.0.1:8000/live;   echo
curl -sS http://127.0.0.1:8000/health; echo
curl -sS http://127.0.0.1:8000/v1/model | head -c 400; echo
```

Use `curl -sS`, not `-s`. Plain `-s` silences connection errors, so a dead server looks
like an empty response instead of a refusal.

Use **`127.0.0.1`, not `localhost`.** On Windows `localhost` can resolve to IPv6 `::1`
while uvicorn binds IPv4, which presents as `ERR_CONNECTION_REFUSED` even though the
server is up. `localhost:8501` in a browser is fine.

**Verify:** `netstat` shows a `LISTENING` line; `/live` returns 200 with no model needed;
`/health` returns 200 with the `model_version` from Step 5 and
`feature_hash: 48c939942ca4ef6a`.

- If `netstat` is empty, read `api.log` — the reason is in there.
- If `/health` is 503, its `reason` field names the cause. Read it rather than guessing.
- If the hash differs, stop: the artifact was built from a different feature order than
  this code.

Stop it later with `pkill -f uvicorn`.

---

## Step 8 — Record the golden payloads

With the API still running:
```bash
cd "$PROJECT_DIR"
source venv/Scripts/activate
python scripts/golden_check.py --url http://127.0.0.1:8000 --record
```

**Verify:** `recorded 90 expectations`, and `null-payload rejection: OK (422)`.

```bash
git add tests/golden/expected.json
```
These 90 numbers are now the parity contract checked at three points: local, container,
and through the LoadBalancer. **Commit this file** — CI's post-deploy job reads it.

Leave the API running — Step 8 needs it.

---

## Step 9 — Run the Streamlit UI locally

The API from Step 6 is still running in the background. Start the UI in the foreground:
```bash
cd "$PROJECT_DIR"
source venv/Scripts/activate
export API_URL=http://127.0.0.1:8000
export ROLE=ui
./entrypoint.sh
```

**Verify:** browse to `http://localhost:8501`. The header shows API ready plus the model
version. Upload `tests/fixtures/sample_panel.csv`, click **Forecast next month**, and
confirm you get 72 brand × zone rows plus the All-India roll-up, tagged `source = model`.

Ctrl-C the UI when done. Stop the API with `pkill -f uvicorn`.

---

## Step 10 — Build the container (ARM64)

The `t4g` node is ARM64. An amd64 image fails there with `exec format error` and no
useful diagnostic, so the platform is not optional.

```bash
cd "$PROJECT_DIR"
docker buildx create --use --name chocolate-builder 2>&1 | tail -1 || docker buildx use chocolate-builder
docker buildx build --platform linux/arm64 -t chocolate-api:local --load .
```

Expect **8–15 minutes** on Windows x86 — buildx runs ARM under QEMU emulation. GitHub
Actions builds natively and is much faster.

**Verify:**
```bash
docker image inspect chocolate-api:local --format '{{.Os}}/{{.Architecture}}'
docker image inspect chocolate-api:local --format '{{.Config.User}}'
docker run --rm chocolate-api:local 2>&1 | head -2; echo "exit=$?"
```
Expect `linux/arm64`, user `appuser`, and the no-ROLE run exiting **78** with
`FATAL: ROLE is not set` — a configuration error, not a crash.

---

## Step 11 — Second parity point: the container

```bash
docker run --rm -p 8000:8000 \
  -e ROLE=api \
  -e AWS_REGION=ap-south-2 \
  -e S3_BUCKET=brandwise-zonewise-chocolate-sales-prediction \
  -v "$HOME/.aws:/home/appuser/.aws:ro" \
  chocolate-api:local
```

From a second terminal (the container occupies this one):
```bash
cd "$PROJECT_DIR" && source venv/Scripts/activate
python scripts/golden_check.py --url http://127.0.0.1:8000
```

**Verify:** `GOLDEN PARITY OK` with a largest absolute difference of `0.0000000000`.
A container returning different numbers from local is a corrupt artifact — and both
would still return HTTP 200, which is exactly why this is automated.

Stop the container.

---

## Step 12 — Push to ECR

The repository already exists.

```bash
aws ecr describe-repositories --repository-names "$ECR_REPO" --region "$AWS_REGION" \
  --query 'repositories[0].{name:repositoryName,uri:repositoryUri,scan:imageScanningConfiguration}'

aws ecr put-image-scanning-configuration --repository-name "$ECR_REPO" \
  --image-scanning-configuration scanOnPush=true --region "$AWS_REGION"

aws ecr get-login-password --region "$AWS_REGION" \
  | docker login --username AWS --password-stdin "$ECR_REGISTRY"

if git rev-parse --short=7 HEAD >/dev/null 2>&1; then
  IMAGE_TAG=$(git rev-parse --short=7 HEAD)
else
  echo "repo has no commits yet — tagging this build 'manual1'"
  IMAGE_TAG=manual1
fi
export IMAGE_TAG
docker tag chocolate-api:local "$ECR_REGISTRY/$ECR_REPO:$IMAGE_TAG"
docker tag chocolate-api:local "$ECR_REGISTRY/$ECR_REPO:latest"
docker push "$ECR_REGISTRY/$ECR_REPO:$IMAGE_TAG"
docker push "$ECR_REGISTRY/$ECR_REPO:latest"
```

**Verify:**
```bash
aws ecr describe-images --repository-name "$ECR_REPO" --region "$AWS_REGION" \
  --query 'sort_by(imageDetails,&imagePushedAt)[-1].{tags:imageTags,size:imageSizeInBytes,pushed:imagePushedAt}'
echo "will deploy tag: $IMAGE_TAG"
```
Both tags exist; **deployment always uses the SHA tag, never `latest`.**

---

## Step 13 — Instance-type and architecture pre-check

A restricted account can only launch free-tier-eligible types. Others fail with **no
error and no instances, in every region** — region-hopping never fixes it.

```bash
aws ec2 describe-instance-types --region "$AWS_REGION" \
  --filters Name=free-tier-eligible,Values=true \
  --query 'InstanceTypes[].{type:InstanceType,arch:ProcessorInfo.SupportedArchitectures[0],mem:MemoryInfo.SizeInMiB}' \
  --output table

aws ec2 describe-instance-type-offerings --region "$AWS_REGION" \
  --location-type availability-zone \
  --filters Name=instance-type,Values=t4g.small \
  --query 'InstanceTypeOfferings[].Location' --output text
```

**Verify:** `t4g.small` appears as free-tier eligible with `arm64`, and is offered in at
least one AZ. If it is not eligible on your account, stop and decide — do not proceed and
discover it as a silent capacity failure.

---

## Step 14 — Create the EKS cluster

> **This is the expensive step. Billing starts now (~$0.10/hour for the control plane).**

```bash
eksctl create cluster \
  --name chocolate-sales-eks \
  --region ap-south-2 \
  --version 1.31 \
  --nodegroup-name chocolate-ng \
  --node-type t4g.small \
  --nodes 1 --nodes-min 1 --nodes-max 2 \
  --node-volume-size 20 \
  --managed \
  --with-oidc
```

Takes 15–25 minutes. **An `eksctl` timeout is not a failure** — the CLI can give up while
CloudFormation is still `CREATE_IN_PROGRESS`. If it times out, check the stack, not the exit code:

```bash
aws cloudformation describe-stacks --region "$AWS_REGION" \
  --query 'Stacks[?contains(StackName,`chocolate-sales-eks`)].{name:StackName,status:StackStatus}' \
  --output table
```

**Verify:**
```bash
aws eks describe-cluster --name "$EKS_CLUSTER" --region "$AWS_REGION" \
  --query 'cluster.{status:status,version:version,oidc:identity.oidc.issuer}'
aws eks update-kubeconfig --name "$EKS_CLUSTER" --region "$AWS_REGION"
kubectl get nodes -o wide
kubectl get node -o jsonpath='{.items[0].status.nodeInfo.architecture}'; echo
```
Node `Ready`, architecture **arm64**, and a non-null OIDC issuer.

`--with-oidc` matters: without it `eksctl` warns *"OIDC is disabled on the cluster"* and
**continues anyway**, and IRSA then silently cannot work.

---

## Step 15 — Node headroom, before deploying

```bash
kubectl describe node | grep -A10 "Allocated resources"
kubectl get node -o jsonpath='{.items[0].status.allocatable.memory}'; echo
```

**Verify:** allocatable memory minus current requests must exceed **650Mi** (400Mi api +
250Mi ui). If it does not, use the API-only path in Step 22 rather than watching the pod
sit `Pending`.

---

## Step 16 — Namespace, config, and IRSA

```bash
cd "$PROJECT_DIR"
kubectl apply -f k8s/config.yml
kubectl get ns chocolate-prod
kubectl get configmap chocolate-config -n chocolate-prod -o yaml | head -20
```

Create the IRSA ServiceAccount. `eksctl` creates it — it is **never** hand-defined in a
manifest, and the manifests reference it by name only.

```bash
cat > irsa-policy.json << 'EOF'
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "ReadModelAndData",
      "Effect": "Allow",
      "Action": ["s3:GetObject", "s3:ListBucket"],
      "Resource": [
        "arn:aws:s3:::brandwise-zonewise-chocolate-sales-prediction",
        "arn:aws:s3:::brandwise-zonewise-chocolate-sales-prediction/*"
      ]
    }
  ]
}
EOF

aws iam create-policy \
  --policy-name ChocolateSalesS3ReadOnly \
  --policy-document file://irsa-policy.json \
  --region "$AWS_REGION"
```

The policy file is written into the repo directory, not `/tmp` — `/tmp` is unreliable
under Git Bash on Windows. It contains no secrets; add it to git or delete it, your call.

```bash
eksctl create iamserviceaccount \
  --cluster chocolate-sales-eks \
  --region ap-south-2 \
  --namespace chocolate-prod \
  --name chocolate-sa \
  --attach-policy-arn arn:aws:iam::117211782845:policy/ChocolateSalesS3ReadOnly \
  --approve
```

**Verify:**
```bash
kubectl get serviceaccount chocolate-sa -n chocolate-prod -o yaml | grep -A2 annotations
```
The `eks.amazonaws.com/role-arn` annotation must be present. Without it the pod falls
back to the node role and gets `AccessDenied` on the model load, which surfaces as a
readiness failure rather than anything obviously credential-shaped.

**This policy is read-only on purpose.** Phase 3 adds a scoped `s3:PutObject` on
`predictions/` when inference logging is switched on, and retraining runs under a
different role because it writes to `models/`.

---

## Step 17 — Render, verify, apply

Never `envsubst | kubectl apply` — a piped render hides an unset variable, and
`image: registry/repo:` is valid YAML that fails minutes later as `ImagePullBackOff`.

```bash
cd "$PROJECT_DIR"
pip install pyyaml
IMAGE_TAG="$IMAGE_TAG" ./.github/scripts/render_and_verify.sh
cat rendered/deployment.yml | grep -n "image:"
```

**Verify:** `RENDER VERIFIED`, and both containers show the full
`117211782845.dkr.ecr.ap-south-2.amazonaws.com/...:<sha>` reference with a non-empty tag.

```bash
kubectl apply -f rendered/config.yml
kubectl apply -f rendered/deployment.yml
kubectl apply -f rendered/service.yml
kubectl apply -f rendered/pdb.yml
kubectl rollout status deployment/chocolate-api -n chocolate-prod --timeout=300s
```

**If the rollout does not complete, read the reason before touching anything:**
```bash
kubectl get pods -n chocolate-prod -o wide
kubectl describe pod -n chocolate-prod -l app=chocolate-api | tail -40
kubectl logs -n chocolate-prod -l app=chocolate-api -c api --tail=60
kubectl get events -n chocolate-prod --sort-by=.lastTimestamp | tail -20
```

---

## Step 18 — Verify live, and the third parity point

```bash
kubectl get svc chocolate-svc -n chocolate-prod
export LB=$(kubectl get svc chocolate-svc -n chocolate-prod \
  -o jsonpath='{.status.loadBalancer.ingress[0].hostname}')
echo "API: http://$LB"
echo "UI : http://$LB:8501"
```

The hostname takes 2–4 minutes to resolve after creation.

```bash
curl -s "http://$LB/live" | cat;   echo
curl -s "http://$LB/health" | cat; echo
```

**Verify:** `/health` returns 200 with the same `model_version` and `feature_hash` you
saw locally. Then the third parity point:

```bash
cd "$PROJECT_DIR" && source venv/Scripts/activate
python scripts/golden_check.py --url "http://$LB"
```

**Verify:** `GOLDEN PARITY OK`. Local, container, and LoadBalancer now agree to the
decimal on all 90 series. Open `http://$LB:8501` and confirm the UI works end to end.

---

## Step 19 — Rollback drill (required before sign-off)

An untested rollback is not a rollback.

```bash
aws s3 ls "s3://$S3_BUCKET/models/" | grep "v_"
```

With only one version, create a second so there is something to roll back to:
```bash
cd "$PROJECT_DIR"
python -m src.train --git-sha "$(git rev-parse --short HEAD)"
aws s3 cp "s3://$S3_BUCKET/models/CURRENT.json" - | cat
```

Note the two version ids, then promote and roll back:
```bash
python -c "
from src import registry
vs = registry.list_versions()
print('versions:', vs)
print('current :', registry.current_alias()['version_id'])
"
```

```bash
read -e -p "version id to roll back TO: " ROLLBACK_TO
python -c "
import sys
from src import registry
print(registry.promote(sys.argv[1], 'rollback drill'))
" "$ROLLBACK_TO"

kubectl rollout restart deployment/chocolate-api -n chocolate-prod
kubectl rollout status deployment/chocolate-api -n chocolate-prod --timeout=300s
curl -s "http://$LB/health" | cat; echo
```

**Verify:** `/health` now reports the rolled-back `model_version`. Time it, and record
the elapsed seconds in the Phase-2 sign-off. Then promote forward again the same way.

Note: promotion is a **JSON write**, and pods pick it up on restart. Nothing is
overwritten, so both directions are reversible.

---

## Step 20 — Push to GitHub and enable CI/CD

```bash
cd "$PROJECT_DIR"
git add -A
git diff --cached --name-only | head -40
git diff --cached | grep -nE "AKIA[0-9A-Z]{16}|aws_secret_access_key[[:space:]]*=" \
  && echo ">>> STOP: read those lines before committing" \
  || echo "credential scan: clean"
```

```bash
git commit -m "Phase 2: FastAPI + Streamlit serving, versioned registry with manifest check, ARM64 image, EKS deploy, CI/CD"
git push -u origin main
```

Add the two repository **secrets** in GitHub → Settings → Secrets and variables → Actions:

| secret | value |
|---|---|
| `AWS_ACCESS_KEY_ID` | your key ID |
| `AWS_SECRET_ACCESS_KEY` | your secret |

**No repository Variables are required.** Every non-secret identifier is baked in as
`${{ vars.X || 'concrete-default' }}`, so a fresh clone works without you creating any.

**If the CI credential is not the cluster creator**, every `kubectl` call returns
`Unauthorized`. Map it once:
```bash
aws sts get-caller-identity --query Arn --output text
eksctl get iamidentitymapping --cluster chocolate-sales-eks --region ap-south-2
```

**Verify the pipeline:** push a trivial change and watch Actions. Four jobs run —
`test` (no credentials), `build` (ARM64 → ECR), `deploy` (pre-flight, render-verify,
apply), `post_deploy` (golden check against the live LoadBalancer).

---

## Step 21 — Failure diagnosis

**Read the real error at its source. Never blind-retry.**

| symptom | first command | likely cause |
|---|---|---|
| `exec format error` | `kubectl get node -o jsonpath='{.items[0].status.nodeInfo.architecture}'` | amd64 image on an arm64 node — rebuild with `--platform linux/arm64` |
| Pod `Pending` | `kubectl describe pod -n chocolate-prod \| tail -20` | insufficient memory on the node → Step 21 |
| `ImagePullBackOff` | `kubectl describe pod -n chocolate-prod \| grep -A5 Events` | tag not in ECR, or the node role lacks ECR pull |
| Readiness never passes | `kubectl logs -n chocolate-prod -l app=chocolate-api -c api --tail=50` | model load failed — the log names the reason |
| `AccessDenied` on model load | `kubectl get sa chocolate-sa -n chocolate-prod -o yaml` | missing IRSA annotation, or the OIDC provider was never associated |
| `ManifestMismatch` in logs | compare `manifest.json` `library_versions` against `requirements.txt` | working as designed — the artifact and image disagree |
| CI `kubectl` `Unauthorized` | `eksctl get iamidentitymapping --cluster chocolate-sales-eks --region ap-south-2` | CI principal is not mapped |
| `eksctl` "timeout" | `aws cloudformation describe-stacks --region ap-south-2` | CLI gave up; AWS may still be working |

Removing the venv: **`deactivate` first**, then `rm -rf venv`. Deleting the venv you are
standing inside removes the `python` on your PATH.

---

## Step 22 — Free-tier fallback: API only

If Step 15 shows the node cannot hold both containers:

```bash
cd "$PROJECT_DIR"
kubectl patch deployment chocolate-api -n chocolate-prod --type=json \
  -p '[{"op":"remove","path":"/spec/template/spec/containers/1"}]'
kubectl rollout status deployment/chocolate-api -n chocolate-prod --timeout=300s
```

Then run the UI from your laptop against the live API:
```bash
export API_URL="http://$LB"
ROLE=ui ./entrypoint.sh
```

Record this in the sign-off as a documented deviation. Also remove port 8501 from the
Service if the UI container is gone — **an exposed port nothing listens on leaves a
permanently unhealthy load-balancer target.**

---

## Step 23 — Cost and teardown

| resource | cost |
|---|---|
| EKS control plane | ~$0.10/hr, **~$73/month, not free tier** |
| `t4g.small` node | free-trial dependent, otherwise ~$0.017/hr |
| Network Load Balancer | ~$16/month + data |
| ECR storage | ~$0.10/GB-month |
| S3 | negligible |

**Delete the LoadBalancer Service BEFORE the cluster**, or the ELB is orphaned and keeps
billing after the cluster is gone:

```bash
kubectl delete svc chocolate-svc -n chocolate-prod
sleep 60
aws elbv2 describe-load-balancers --region "$AWS_REGION" \
  --query 'LoadBalancers[].{name:LoadBalancerName,state:State.Code}' --output table

eksctl delete cluster --name chocolate-sales-eks --region ap-south-2 --wait
```

**Verify the teardown actually completed — half-created paid resources still bill:**
```bash
aws eks list-clusters --region "$AWS_REGION"
aws cloudformation describe-stacks --region "$AWS_REGION" \
  --query 'Stacks[?StackStatus!=`DELETE_COMPLETE`].{name:StackName,status:StackStatus}' --output table
aws elbv2 describe-load-balancers --region "$AWS_REGION" --query 'length(LoadBalancers)'
```
Expect an empty cluster list, no lingering stacks, and zero load balancers.

---

## What is NOT in this phase

- **No drift detection, no retraining trigger, no SNS alerts, no CloudWatch alarms.** Phase 3.
- **No inference logging.** `LOG_PREDICTIONS=false` in the ConfigMap, and the serving IRSA
  policy is read-only. Phase 3 turns it on *and* grants the scoped `s3:PutObject` — enabling
  one without the other yields `AccessDenied` on every request.
- **No canary or shadow deployment.** One free-tier node cannot host two pods.
  `RollingUpdate` with `maxUnavailable: 0` is the documented deviation. **On real
  infrastructure the canary is not optional.**
- **No HPA.** A single fixed node has nothing to scale onto.
- **No UAT namespace.** Deploying prod only, with UAT dormant behind its branch gate, is a
  deliberate cost decision: two namespaces means two ELBs means two bills.
- **No OIDC federation for CI.** Static keys in GitHub Secrets, stated plainly. OIDC is the
  upgrade and removes the long-lived credential.
- **No SageMaker Pipeline.** `src/train.py` runs manually or from CI. Phase 3 wires it to
  the drift trigger.

---

## Phase 2 sign-off record

| field | value |
|---|---|
| Phase | 2 — Deployment |
| Reviewed by | _____________________ |
| Date | _____________________ |
| Image tag deployed (SHA) | _____________________ |
| Model version promoted | _____________________ |
| Three-point parity (local / container / LB) | ☐ passed, largest diff ________ |
| Rollback drill | ☐ rehearsed, elapsed ________ seconds |
| Node headroom at deploy | ____________ Mi free |
| Accepted deviations | canary deferred (single node); static CI keys; UAT not deployed; inference logging off |
| Decision | ☐ approved ☐ approved with conditions ☐ rework |

**Phase 3 does not begin until this is filled in.**
