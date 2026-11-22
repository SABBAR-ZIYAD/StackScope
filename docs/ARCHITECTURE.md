# Architecture

GitHub Ecosystem Miner is a run-isolated pipeline:

```text
REST Search → snapshot enrichment → Git Trees/files → evidence/cleaning
            → association mining → clustering → robustness → HTML report/export
```

- `collection.py`, `github_client.py`, `rate_limits.py` and `retrieval.py` own GitHub I/O.
- `storage.py` owns the versioned DuckDB schema.
- `cleaning.py`, `parsers.py` and `normalization.py` create traceable technology evidence.
- `association.py` mines rules, substitutes and communities.
- `clustering.py` compares feature weighting and clustering methods, then stores the selected model's
  assignments and explainable profiles.
- `robustness.py` evaluates sensitivity and explicitly separates creation cohorts from true observed
  commit snapshots.
- `reporting.py` reads recorded artifacts only; report regeneration requires no GitHub access.

Every command operates on a run ID. The run manifest locks configuration and records code,
dependencies, seeds, stage status and artifacts. Generated data stays under `data/runs/` and is not
committed.
