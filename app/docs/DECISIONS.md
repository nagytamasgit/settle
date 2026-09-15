# Decisions: the app

Every notable thing `settle-app` does *not* do, and why. The engine keeps its
own list in [`../../docs/DECISIONS.md`](../../docs/DECISIONS.md); this one covers
the choices that only exist because there is a browser and a database involved.

The engine's entries about having no database and no web UI are still true *of
the engine*. This is where the opposite choice was made, and it is made once,
deliberately, in a package the engine cannot see.

---

## Boundaries

### The engine is a dependency, never a sibling

Nothing under `settle/` imports anything under `settle_app/`, and
`app/tests/test_app_separation.py` checks it rather than trusting it. That is
what lets `pip install settle-engine` still deliver a pure engine with no
FastAPI, no SQLite and no templates.

There is deliberately no `settle web` command. Adding one would make the engine
aware of the app, and the separation would last exactly until the next
convenient shortcut.

### No matching logic lives here

The app reads CSVs with the engine's readers, calls `reconcile()`, and writes
outputs with the engine's writer. `test_the_web_result_is_byte_identical_to_the_engines`
asserts the served `result.json` equals what `settle run` would have written for
the same input. If that diverges, the engine's test suite stops being evidence
about this app, which is the whole reason the engine's suite is worth anything.

### The results page renders the files, not a rehydrated result

Every table is built by reading back the CSVs the engine wrote and joining them
against the uploaded ledger. Deserialising `result.json` into a
`ReconciliationResult` would have been a second implementation of the result
model, free to drift from `json_io`. It also keeps a promise worth keeping: what
is on screen is the file you can download, because it is the file you can
download.

---

## Storage

### SQLite, not Postgres

One writer, one box, and a backup is a file. Postgres would add a second
service, a second backup story and a network boundary to secure, for a workload
of a few hundred rows a day. Revisit if this ever needs more than one instance —
that is the real trigger, not row count.

### No ORM

Six tables and about twenty queries, all parameterised. An ORM would be more
code than the queries it replaced. The one place user input reaches an f-string
is the sort column, which is why `SORTABLE` is a fixed tuple and the test that
passes `amount; DROP TABLE runs` exists.

### Money is `TEXT`

A `REAL` column would reintroduce, in storage, exactly the bug the engine spends
its entire design avoiding. `1000.10` would come back as
`1000.0999999999999943`. An integer-minor-units column would work too, but the
values here are for display and comparison, never arithmetic, so the string the
engine already produced is the honest thing to keep.

### The database records outputs; it is never an input

No stored row is ever read back into `reconcile()`. The engine's determinism
property is therefore unaffected by anything in the database — there is no
hidden state for a second run to disagree with, because the second run does not
consult it.

### Run history is append-only

A run is never re-run in place and never edited. Correcting a mapping means a
new run. That is what makes a stored run a stable thing to cite in a
conversation three weeks later.

---

## Access

### One shared password, not user accounts

Per-user accounts mean a users table, registration, password reset, and
per-user audit — a different product. The cost of not having them is real and
worth naming: **there is no record of *who* did anything**, only that somebody
with the password did, and revoking access for one person means rotating for
everyone.

Two things pay part of that back. The session signing key is derived from the
password, so rotating it genuinely invalidates every outstanding session rather
than appearing to. And `access_log` records IP and timestamp per login,
download and delete, which is not identity but is better than nothing.

`docs/DEPLOY.md` says all of this in the same words, because the person
deploying it is the one who needs to decide whether it is enough.

### The app refuses to start unprotected on a public interface

Binding anywhere but loopback without a password raises before the socket opens.
An instance in that state would serve other people's invoices and bank
statements to anyone who found it, and a warning in a log nobody reads is not a
control.

Loopback with no password is still allowed, because that is how you try it
locally, and the footer says plainly that it is unprotected.

### No JavaScript, enforced rather than claimed

`Content-Security-Policy: default-src 'none'` with no script source. The cost is
real — there is no client-side file size check, so an oversized upload is
rejected only after it arrives — and it is accepted because a finance tool with
no script surface is a smaller thing to reason about. Caddy's `request_body
max_size` blunts the upload cost at the edge.

---

## The model

### The model may propose a column mapping; it may never apply one

Same shape as the engine's reference parser: it only proposes, it runs last, its
proposals are verified against the closed set of real headers, and it never
raises. A proposal naming a column that is not in the file is dropped. It cannot
overwrite what the alias table resolved, and it cannot claim one column twice.

**A human confirms every mapping before a run**, including when every column
matched by alias and there is nothing to change. The confirmation screen is not
conditional on anything, because the moment it is, the model tier is one flag
away from deciding where somebody's money went unsupervised. The bundled sample
goes through it too, which is why the sample is written to the upload filenames
rather than the canonical ones.

### Headers are sent; values are not

By default the model sees column headers — `Invoice No`, `Client Name` — and
nothing else. Headers are rarely personal data. The values underneath them are
customer names and payment references, which is why sending samples is a
separate opt-in on top of an opt-in, and why a test asserts the values do not
leave when it is off.

---

## Operations

### Persisting runs makes the operator a data controller

Nothing else in this repository has that property; the CLI keeps nothing. The
app therefore has deletion that removes files as well as rows, a retention
window, a sweep for uploads nobody confirmed, and a deploy guide that opens by
saying what is stored and who can read it.

Keeping runs forever is allowed but never implicit: with no window configured,
`settle-app purge` says so on stderr rather than reporting success.

### Reconciliation runs inside the request

Synchronous, with a row cap and a 120-second proxy timeout. This does not scale
and is not claimed to; the cap exists so that the failure is a clear refusal
naming `settle run` rather than a request that dies at a proxy. Background jobs
are the first thing to add if the cap starts to bite, and that is the trigger to
watch for.

### The sweep runs at startup

A deployment restarted for an update should not need a cron job before it stops
holding data it was told to forget. `settle-app purge` exists for instances that
stay up for months, where "at startup" is not often enough.
