# settle-app

A browser interface for the [settle](../README.md) reconciliation engine. Upload
an invoice ledger and a bank statement, confirm how your columns map onto
settle's, and get back the allocations, the review queue, and the closing
position of every invoice — with the history kept so you can come back to a run.

This is the second of two products in this repository:

| | [`settle-engine`](../README.md) | **`settle-app`** (this) |
|---|---|---|
| For | Experts, integrators, library users | Everyday use |
| Ships | Engine, CLI, `POST /reconcile` | Web UI, run history, column mapping |
| Has | No UI, no database, no state | All three, by design |
| Install | `pip install settle-engine` | `docker compose up` |

The engine has no idea this exists. It is a dependency here, never a sibling —
`app/tests/test_app_separation.py` checks that rather than trusting it.

## Status

Under construction. See `docs/DEPLOY.md` for the VPS guide once M7 lands.

## Development

From the repository root:

```bash
uv pip install -e ".[dev,api,bench,model]"   # the engine
uv pip install -e "./app[dev]"               # this app
.venv/bin/pytest                             # both suites
```

## Licence

[PolyForm Noncommercial 1.0.0](../LICENSE), the same as the engine — free for
individuals, study, research, teaching and nonprofits; commercial use needs a
licence. See [`COMMERCIAL.md`](../COMMERCIAL.md).
