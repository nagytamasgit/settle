# settle-app

A browser interface for the [settle](../README.md) reconciliation engine. Sign
in, upload an invoice ledger and a bank statement, confirm which of your columns
is which, and get back the allocations, the review queue, and the closing
position of every invoice — with the history kept, so last month's run is still
there when somebody asks.

This is the second of two products in this repository:

| | [`settle-engine`](../README.md) | **`settle-app`** (this) |
|---|---|---|
| For | Integrators, library users, the command line | Everyday use in a browser |
| Ships | Engine, CLI, `POST /reconcile` | Web UI, run history, column mapping |
| Has | No UI, no database, no state | All three, by design |
| Install | `pip install settle-engine` | `docker compose up` |

The engine has no idea this exists. It is a dependency here, never a sibling —
`tests/test_app_separation.py` checks that rather than trusting it.

---

## Run it

```bash
cd app
mkdir -p secrets && openssl rand -base64 24 > secrets/settle_password
chmod 600 secrets/settle_password
echo "SETTLE_DOMAIN=settle.example.com" > .env
docker compose up -d
```

Caddy gets a TLS certificate automatically, provided the domain already points
at the box. The full walkthrough from a bare VPS, including backups and a
rehearsed restore, is in [`../docs/DEPLOY.md`](../docs/DEPLOY.md).

To try it locally without any of that:

```bash
SETTLE_WEB_PASSWORD=a-long-enough-password settle-app serve --port 8000
```

Then open <http://127.0.0.1:8000> and click **Try the sample** — a generated
ledger with merged payments, part payments, bank fees and unreadable references,
so there is something to look at before you upload anything real.

---

## What it does that the engine doesn't

**Reads your column names.** Real exports say `Invoice Number`, `Num`,
`szamlaszam` or `ContactName`, never settle's names. An alias table and a fuzzy
pass work out which is which; a model tier can be enabled for the rest. Whatever
proposes a mapping, **you confirm it on screen before anything runs** — there is
no code path that skips that step.

**Keeps a history.** Runs are stored, browsable and filterable, and survive a
restart. That is also why this app has a retention setting and a deploy guide
that says plainly what is stored and who can read it: persisting other people's
financial data is a responsibility the command line does not have.

**Puts a password in front of it.** One shared password for the instance, not
user accounts. The honest costs of that are written down in
[`docs/DECISIONS.md`](docs/DECISIONS.md).

---

## What it deliberately doesn't do

No JavaScript — the Content-Security-Policy forbids scripts outright, and that
is enforced rather than claimed. No user accounts. No bank or ERP connections.
No matching logic: every answer comes from the engine, and a test asserts the
`result.json` it serves is byte-identical to what `settle run` writes for the
same input.

Reconciliation runs inside the HTTP request, with a row cap. That does not scale
and is not claimed to; a ledger past the cap gets a clear refusal pointing at
`settle run`, which handles any size.

---

## Configuration

Everything is read from the environment, once, into one object. The complete set:

| Variable | Default | What it does |
|---|---|---|
| `SETTLE_WEB_DATA_DIR` | `data` | Database and run files |
| `SETTLE_WEB_PASSWORD` | — | The shared password, minimum 12 characters |
| `SETTLE_WEB_PASSWORD_FILE` | — | Read it from a file instead; preferred, since it stays out of `docker inspect` |
| `SETTLE_WEB_HOST` / `_PORT` | `127.0.0.1` / `8000` | Bind address |
| `SETTLE_WEB_BEHIND_PROXY` | off | Trust `X-Forwarded-For`. Only behind a proxy you control |
| `SETTLE_WEB_HTTPS` | off | Mark the session cookie `Secure`, send HSTS |
| `SETTLE_WEB_MAX_UPLOAD_BYTES` | 8 MB | Per file |
| `SETTLE_WEB_MAX_ROWS` | 50 000 | Per file |
| `SETTLE_WEB_RETENTION_DAYS` | unset | Delete runs older than this. Unset keeps them forever |
| `SETTLE_WEB_MODEL_MAPPER` | off | Let a model propose column mappings it could not resolve by rule |
| `SETTLE_WEB_MODEL_SAMPLES` | off | Also send example values. These are customer data; read the deploy guide first |

**The app refuses to start** bound to anything but loopback without a password.
That instance would serve other people's invoices to whoever found it, so the
refusal is deliberate — don't work around it.

## Commands

```bash
settle-app serve      # run it
settle-app backup     # safe copy of the database, via SQLite's backup API
settle-app purge      # delete expired runs and abandoned uploads
settle-app version
```

## Development

From the repository root:

```bash
uv pip install -e ".[dev,api,bench,model]"   # the engine
uv pip install -e "./app[dev,model]"         # this app
.venv/bin/pytest                             # both suites
```

## Licence

[PolyForm Noncommercial 1.0.0](../LICENSE), the same as the engine — free for
individuals, study, research, teaching and nonprofits; commercial use needs a
licence. See [`COMMERCIAL.md`](../COMMERCIAL.md).
