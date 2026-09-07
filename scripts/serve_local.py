"""Serve the API against a LOCAL artifact directory instead of S3.

    python scripts/serve_local.py --artifacts /tmp/fakes3
"""
import argparse, os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ap = argparse.ArgumentParser()
ap.add_argument("--artifacts", default="/tmp/fakes3")
ap.add_argument("--port", type=int, default=8000)
a = ap.parse_args()

os.environ["LOCAL_S3_ROOT"] = a.artifacts
import scripts.local_s3   # noqa: E402,F401
import uvicorn            # noqa: E402
from src.api import app   # noqa: E402

uvicorn.run(app, host="127.0.0.1", port=a.port, log_level="info")
