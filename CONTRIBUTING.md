# Contributing

1. Create a branch from `main` and use Python 3.11 or newer.
2. Install with `python -m pip install -e ".[dev]"`.
3. Keep secrets and generated `data/runs/` artifacts out of Git.
4. Add tests for behavior changes, especially API failures and analytical selection rules.
5. Run `python -m unittest discover -s tests -v` and `python -m ruff check src tests`.
6. Explain schema or configuration changes in the pull request and update the migration path.

Collection tests must be deterministic and offline. A live benchmark may supplement tests but must
never be required by CI.
