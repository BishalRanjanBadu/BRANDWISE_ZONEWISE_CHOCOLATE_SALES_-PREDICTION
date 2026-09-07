"""Local-filesystem stand-in for S3. DEV ONLY — excluded from the image by
.dockerignore, and imported by nothing under src/."""
import json, os, pickle

ROOT = os.environ.get("LOCAL_S3_ROOT", "/tmp/fakes3")
from src import s3_io


def _p(key):
    return os.path.join(ROOT, key)


s3_io.get_bytes   = lambda key, bucket=None: open(_p(key), "rb").read()
s3_io.read_json   = lambda key: json.load(open(_p(key)))
s3_io.read_pickle = lambda key: pickle.load(open(_p(key), "rb"))
s3_io.exists      = lambda key, bucket=None: os.path.exists(_p(key))
print(f"LOCAL S3 SHIM ACTIVE -> {ROOT}  (dev only)")
