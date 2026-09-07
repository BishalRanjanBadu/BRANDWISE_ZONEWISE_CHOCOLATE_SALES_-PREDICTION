# Golden payloads

`expected.json` is deliberately NOT committed yet. It must be recorded from a
local server running the REAL promoted artifact — not the fixture-trained test
model — or the post-deploy CI check compares the live endpoint against numbers
from a different model and fails every time.

Record once, then commit (Phase 2 runbook, step 11):

    python scripts/golden_check.py --url http://localhost:8000 --record
    git add tests/golden/expected.json

The same file is then used at all three parity points: local uvicorn,
`docker run`, and through the LoadBalancer.
