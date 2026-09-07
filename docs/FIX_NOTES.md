# CORRECTION — the feature contract is 25 features, not 28

`src/features.py` originally declared 28 features. The promoted artifact carries
**25**. `promote_phase1.py` refused the migration and printed both lists side by
side. That is the compatibility check working exactly as designed — it caught the
mismatch before anything reached a container.

## Cause

`d_l1`, `d_l2` and `rm3` are algebraically degenerate:

```
rm3  = mean(ly_l1..ly_l3) - ly_l1 = (-2*ly_l1 + ly_l2 + ly_l3)/3
d_l1 = ly_l1 - ly_l2
d_l2 = ly_l2 - ly_l3

-(2*d_l1 + d_l2)/3 = (-2*ly_l1 + ly_l2 + ly_l3)/3 == rm3     (verified to 1.8e-15)
```

Rank 2 across 3 columns, so the design matrix is singular and their VIF is
infinite. Notebook 06 detected that and dropped them — which is why the promoted
artifact has 25 features. The first port of `features.py` copied the list from
before that pruning step.

## Fix

`FEATURE_ORDER` now hard-codes the 25 features the artifact actually uses, and
the exclusion is **by name** rather than by re-running a numerical VIF check.

That distinction matters. Whether statsmodels reports `inf` or a very large
finite number for a singular column varies across library versions — which is
precisely why this fired on your Colab run and not on mine. A numerical check
makes the feature set environment-dependent, so a Phase-3 retrain on a different
machine could silently produce a different feature set. Naming the exclusion
makes it deterministic.

Two new tests pin it:

- `test_excluded_features_are_exactly_degenerate` — asserts the identity holds
  and that the rank is 2. If the identity ever stops holding, the exclusion is no
  longer justified and the test says so.
- `test_feature_order_matches_the_promoted_artifact` — pins all 25 names in
  order, so a drift between `src/` and the pickle is caught at commit time rather
  than at container load.

## Known improvement, deliberately deferred

Dropping all three loses the lag-1 and lag-2 momentum signal entirely. Keeping
`d_l1` and `d_l2` and dropping only `rm3` removes the singularity and is a
strictly better feature set.

That is a **retrain, not an edit**. It changes the feature hash, and every
existing artifact would correctly fail readiness against it. Logged as a Phase-3
candidate rather than smuggled into a deployment change.

## Verified after the fix

| check | result |
|---|---|
| Test suite, no cloud credentials | **34 passed** |
| Feature count | 25, exact match to the artifact |
| Feature hash | `48c939942ca4ef6a` |
| End-to-end serve + golden parity | 90 series, largest difference `0.0000000000` |

## Also: your `PROJECT_DIR` was never set

`cd "$PROJECT_DIR"` failed because the export in Step 0 still had the placeholder
path. Your real path contains **spaces**, so it must stay quoted everywhere:

```bash
export PROJECT_DIR="$HOME/Desktop/Bishal_/END TO END PROJECTS/ML PROJECTS/BRAND_WISE_ZONE_WISE_CHOCOLATE_SALES_PREDICTION/3.DEPLOYMENT"
cd "$PROJECT_DIR" && pwd
```

The runbook quotes `"$PROJECT_DIR"` at every use, so once the export is right
nothing else needs changing. An unquoted path with spaces would split into
separate arguments and fail in ways that look unrelated to the path.
