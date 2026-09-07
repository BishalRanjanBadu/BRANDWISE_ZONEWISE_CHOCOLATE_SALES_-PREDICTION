#!/usr/bin/env bash
# Render manifests to files, VERIFY, then apply. Never `envsubst | kubectl apply`.
#
# A piped render hides an unset variable: `image: registry/repo:` is valid YAML
# and fails minutes later as ImagePullBackOff, which looks nothing like a
# templating bug. This renders to disk, greps for unresolved placeholders on
# NON-COMMENT lines only, asserts the tag is non-empty, parses the YAML, and
# prints the resolved image reference before anything is applied.
set -euo pipefail

IMAGE_TAG="${IMAGE_TAG:-}"
REGISTRY="117211782845.dkr.ecr.ap-south-2.amazonaws.com"
REPO="brandwise-zonewise-chocolate-sales-prediction"
OUT="${OUT:-rendered}"

[ -n "$IMAGE_TAG" ] || { echo "FATAL: IMAGE_TAG is empty" >&2; exit 1; }
case "$IMAGE_TAG" in
  latest) echo "FATAL: refusing to deploy :latest - deploy by immutable git SHA" >&2; exit 1 ;;
esac

IMAGE="${REGISTRY}/${REPO}:${IMAGE_TAG}"
echo "resolved image: ${IMAGE}"

mkdir -p "$OUT"
for f in k8s/config.yml k8s/deployment.yml k8s/service.yml k8s/pdb.yml; do
  sed "s|IMAGE_PLACEHOLDER|${IMAGE}|g" "$f" > "${OUT}/$(basename "$f")"
done

fail=0
for f in "${OUT}"/*.yml; do
  # strip comments before grepping, or a literal ${VAR} in a comment reports a
  # false UNRESOLVED (this exact false positive has happened before)
  if sed 's/#.*//' "$f" | grep -nE '\$\{[A-Za-z_]+\}|IMAGE_PLACEHOLDER'; then
    echo "FATAL: unresolved placeholder in $f" >&2
    fail=1
  fi
  if grep -nE 'image:.*:[[:space:]]*$' "$f"; then
    echo "FATAL: empty image tag in $f" >&2
    fail=1
  fi
done
[ "$fail" -eq 0 ] || exit 1

PY="$(command -v python3 || command -v python)"     # Git Bash has no python3
if [ -n "$PY" ] && "$PY" -c 'import yaml' 2>/dev/null; then
  "$PY" - "$OUT" <<'PYEOF'
import glob, sys, yaml
imgs = []
for f in sorted(glob.glob(f"{sys.argv[1]}/*.yml")):
    for doc in yaml.safe_load_all(open(f)):
        if not doc:
            continue
        for c in (doc.get("spec", {}).get("template", {})
                     .get("spec", {}).get("containers", []) or []):
            imgs.append((c["name"], c["image"]))
print("containers to be applied:")
for n, i in imgs:
    print(f"  {n:6s} -> {i}")
    assert i.count(":") >= 1 and not i.endswith(":"), f"bad image ref: {i}"
PYEOF
else
  echo "WARNING: PyYAML unavailable, skipped structural parse (grep checks still ran)"
fi

echo "RENDER VERIFIED -> ${OUT}/"
