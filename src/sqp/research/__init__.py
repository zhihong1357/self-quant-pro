"""Research runner (G2): a trial is registered on main first, then run once in GitHub Actions.

`trials.py` reads and checks the trial cards in registry/ledger/trials.jsonl;
`run.py` runs one card; `studies/` holds the research code itself, which may read
data only through the point-in-time interface (the `research-boundary` check).
"""
