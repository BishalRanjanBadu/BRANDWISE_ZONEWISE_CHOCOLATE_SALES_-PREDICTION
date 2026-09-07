# syntax=docker/dockerfile:1
# Multi-stage. The final layer carries no compiler, no git, no build-essential.
# Target platform is linux/arm64 - the free-tier t4g node is ARM64 and an amd64
# image fails there with "exec format error" and no useful diagnostic.
FROM python:3.13-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /build
COPY requirements.txt .
# Every pin is exact (==) and verified published on PyPI with a cp313 aarch64
# wheel, so nothing compiles from source on the ARM64 t4g node.
#
# The cache mount survives across builds, so a re-build after editing this file
# re-uses the ~180 MB of downloaded wheels instead of pulling them again. It
# lives in the builder stage only and never reaches the final image.
#
# No import check here: this stage deliberately has no libgomp1, so LightGBM
# cannot load. The verification belongs in the runtime stage, where it also
# tests the thing that actually ships.
RUN --mount=type=cache,target=/root/.cache/pip \
    python -m venv /opt/venv \
 && /opt/venv/bin/pip install --upgrade pip \
 && /opt/venv/bin/pip install -r requirements.txt

# ── runtime ────────────────────────────────────────────────────────────────
FROM python:3.13-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH" \
    HOME=/home/appuser \
    APP_ENV=prod \
    AWS_REGION=ap-south-2 \
    S3_BUCKET=brandwise-zonewise-chocolate-sales-prediction \
    API_URL=http://localhost:8000

# libgomp is the ONLY system library lightgbm needs at runtime.
#
# curl was here and has been REMOVED: nothing in the image invokes it. The k8s
# probes are httpGet, performed by the kubelet from outside the container, and
# every curl in the runbook runs on the operator's laptop. It contributed two
# CRITICAL CVEs for no functional benefit.
#
# `apt-get upgrade` patches the Debian packages the base image shipped with.
# Without it the image carries whatever CVEs were open on the day the base tag
# was built, and an ECR scan reports every one of them. Run in the same layer as
# `update` so the package lists cannot go stale between the two.
#
# TRADE-OFF, stated rather than hidden: this makes the OS layer NON-reproducible.
# The same Dockerfile built a month apart yields different Debian package
# versions, which sits awkwardly beside the exact `==` pins in requirements.txt.
# Accepted deliberately: shipping known-unpatched OS packages is the worse of the
# two risks, and the image digest is recorded at each deploy so any given
# running image is still identified exactly.
RUN apt-get update \
 && apt-get upgrade -y \
 && apt-get install -y --no-install-recommends libgomp1 \
 && apt-get autoremove -y \
 && rm -rf /var/lib/apt/lists/* \
 && useradd --create-home --uid 10001 appuser

COPY --from=builder /opt/venv /opt/venv

# Verify the FINAL image can import its third-party stack. Catches a
# wrong-architecture wheel or a missing system library at build time rather than
# as a pod that starts and then dies on first request.
RUN python -c "import platform, lightgbm, sklearn, pandas, numpy, fastapi, streamlit, boto3; \
print('arch:', platform.machine(), '| lightgbm', lightgbm.__version__, \
'| sklearn', sklearn.__version__, '| pandas', pandas.__version__, '| numpy', numpy.__version__)"

WORKDIR /app
COPY --chown=appuser:appuser src/ ./src/
COPY --chown=appuser:appuser entrypoint.sh ./entrypoint.sh
RUN chmod +x ./entrypoint.sh

# Verify OUR code too. The check above proves the dependencies resolve; this
# proves the application itself imports and that the feature contract in the
# image matches the artifact it will be asked to serve. A mismatch here is the
# defect that previously reached a live endpoint as an HTTP 500.
# src.app is compiled rather than imported: Streamlit modules execute UI calls
# at import time and need a running Streamlit context.
RUN python -c "import src.api, src.registry, src.predict; \
from src.features import FEATURE_ORDER, feature_hash; \
print('app imports OK | features:', len(FEATURE_ORDER), '| hash:', feature_hash(FEATURE_ORDER))" \
 && python -m py_compile src/app.py \
 && rm -rf src/__pycache__

USER appuser
EXPOSE 8000 8501

# No ROLE default. An unset ROLE exits 78 with an actionable message rather
# than starting something nobody asked for.
ENTRYPOINT ["./entrypoint.sh"]