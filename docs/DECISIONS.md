# Decisions

Every notable thing this project does *not* do, and why. A reviewer's first
question about a small repository is usually "why isn't there a database?", so
the answers live here rather than in a commit message nobody will find.

---

## Scope

### No bank or ERP integrations

The CSV contract **is** the integration point. Two files and a schema are
something any finance system can already produce, and they make the whole engine
testable without a single credential or sandbox account. An adapter for a
specific bank would be a few hundred lines of that bank's quirks and would prove
nothing about the matching logic, which is the interesting part.

### No database

`reconcile()` is a pure function of `(ledger, statement, policy)`. Persistence
belongs to the caller: which invoices are open, and what to do with an
allocation once it exists, are decisions about *their* system, not this one.
Keeping state out is also what makes the determinism property testable at all —
there is no hidden state for a second run to disagree with.

### No web UI

The review queue is data, not a screen. It ships as JSON and CSV so it can go
into whatever the finance team already uses. A React app here would be the
largest and least interesting part of the repository.

### No multi-currency conversion

Currency must match for an allocation to happen; the invariant checker enforces
it. FX is a genuinely different problem with its own invariants (which rate,
whose rate, at what date, and what happens to the difference), and bolting a
rate lookup onto this engine would produce something that looks like it handles
FX without actually being correct.

### No custom model training

The model tier is a small hosted model behind an interface. The point of this
project is *where the model sits* — proposing, never deciding, always verified —
not how good it is. A fine-tuned reference parser would improve one number in
the benchmark and obscure the architectural claim.

### Outgoing payments and reversals are rejected

`Payment` requires a positive amount. Refunds, chargebacks and reversals invert
several of the invariants (money leaves, invoices re-open) and deserve to be
modelled deliberately rather than falling out of a sign change.

---

## Domain modelling

### Two conservation laws, not one

The informal one-liner is "allocations plus fees plus residual equals payments".
The precise statement is two laws, because a fee is money that *never arrived*:

- **Payment side:** `sum(allocations) + sum(residuals) == sum(payments)`
- **Invoice side:** `allocated + fee + open_balance == face_value`

A fee appears only in the second. Folding it into the first would require fees
to be negative on the payment side, which reads as "the bank paid us" and is
exactly the kind of sign confusion that loses money. Both are checked in
`settle/domain/invariants.py`.

### `settled_with_fee` is a distinct status from `paid`

A tolerance that silently closes invoices as `paid` is how a 3% leak becomes a
year-end write-off nobody can explain. The invoice closes, but into its own
state, with the shortfall recorded as a `Fee` line pointing at the payment that
caused it.

### The fee tolerance is the boundary between "fee" and "part payment"

A shortfall within tolerance is a fee; a larger one is a partial payment. The
two are never generated as competing candidates for the same invoice. If they
were, every short payment would be permanently ambiguous and the review queue
would fill with a distinction the engine already knows how to make.

### Confidence is `Decimal`, not `float`

Not for precision — confidence is not money. For **exact tie detection**. Two
candidates tie when their confidences are equal, and a tie is what sends a
payment to a human. On `Decimal` that is `==`; on `float` it is a comparison
against an epsilon nobody chose deliberately.

### Amounts are quantised to two decimal places for every currency

Correct for EUR, HUF-as-used-here, USD and most of the world; wrong for JPY
(zero minor units) and KWD (three). Currencies with different minor units need a
per-currency quantum, which is a small change to `money.py` and a large change
to the test matrix. Recorded here rather than pretended away.

### A greedy pass, not a globally optimal assignment

Payments are processed in `(date, id)` order against a running ledger. Optimal
assignment across a whole statement is NP-hard, and — more importantly — a
finance team cannot audit an answer that changes because an unrelated invoice
moved. The greedy pass is stable, explainable, and safe whatever it picks,
because the invariants hold regardless.

### Duplicate ids are an error, not a de-duplication

Identity is what makes a run idempotent. Silently merging two rows that claim to
be the same invoice hides a broken export upstream, and the failure would
surface later as a number nobody can trace.

---

## Matching policy

### Exact reference matching beats fuzzy matching outright

When a reference names any invoice exactly, near-misses are not considered at
all. **This was found by the benchmark**, not by design: invoice numbers in a
sequence differ by one digit, so `INV-2026-0042` fuzzy-matches `INV-2026-0142`
at 90 — above any threshold loose enough to survive a typo. With both tiers
contributing, perfectly clean data scored 0.94 recall instead of 1.00, because
every clear reference produced a tie. The fuzzy tier now only runs when nothing
matched exactly, which is the mistyped case it was for.

### Ambiguity goes to a human by default

When two candidates explain a payment equally well, the engine returns both,
ranked, and applies neither. `AmbiguityPolicy.RESOLVE_BY_POLICY` exists for
callers who would rather have a deterministic guess, but it is opt-in, because
guessing is the failure mode this project exists to avoid.

### Tie-breaking orders; it does not choose

`TieBreak` makes output stable and puts the policy's preferred invoice first in
the review queue. Whether a tie may be *applied* is `AmbiguityPolicy`. Conflating
the two is how "deterministic" quietly becomes "arbitrary but repeatable".

### The search reports when it gave up

Subset-sum is bounded by width, depth and a node budget. When the budget runs
out, `search_truncated` is set, the run warns, and the uniqueness bonus is
withheld — "I found one answer" means much less if you stopped looking early.

---

## Dependencies

### rapidfuzz is allowed in the domain; the Anthropic SDK is not

The rule is not "no third-party code in the domain", it is **no I/O and no
framework**. `rapidfuzz` is a pure function library: same input, same output, no
network, no clock. An SDK that makes HTTP calls is a different thing entirely,
so it sits behind a port (`domain/ports.py`) with its adapter in `parse/`.

### structlog is the one permitted side effect in the domain

Every allocation decision is logged. Observability was an explicit requirement,
and threading a logger through every function to preserve purity would cost more
readability than it buys. The domain remains a pure function of its *values*.

### Pydantic lives at the boundary, not in the domain

`MatchConfig` and `ConfigModel` are deliberately separate types that mirror each
other. The domain object must not grow a dependency on a validation framework
because one of its callers happens to speak HTTP.

### Lazy imports for optional extras

`anthropic`, `uvicorn` and `matplotlib` are imported inside functions so the
package works without them. `PLC0415` is disabled project-wide for this reason.

---

## Interfaces

### Money crosses the boundary as a string, never a JSON number

`1000.10` parsed as a float is `1000.0999999999999943...`. Both directions are
guarded: the request schema rejects float amounts outright with an explanatory
error, and every amount in the response is serialised as a string so a consumer
cannot reintroduce the bug at their end.

### Unknown CSV columns are ignored; unknown config keys are rejected

Real ledger exports carry extra columns, and requiring them to be trimmed would
make the tool annoying for no safety gain. A misspelled *config* key is
different: it silently means "you did not get the policy you asked for", so it
is an error.

### The CLI and the API share one serialiser

`result_to_dict` is the only place a result becomes JSON, which is what makes
`settle run` and `POST /reconcile` produce identical output. An integration test
asserts it, so the two cannot drift.

---

## Project

### The name

`settle` is taken on PyPI — and by a project in the same niche ("an open-source
reconciliation and matching kernel for financial systems"). The distribution
name is therefore `settle-engine`; the import package and repository stay
`settle`. If this is ever published or promoted, the name is worth revisiting,
because a reviewer searching "settle reconciliation" will find the other one.

### PolyForm Noncommercial, not MIT

The licence is [PolyForm Noncommercial 1.0.0](../LICENSE): free for individuals,
study, research, teaching, nonprofits and government; a commercial licence is
required to use it in or for a business.

MIT was the original intent and is wrong for this project. MIT grants unlimited
commercial use, and reconciliation is precisely the kind of problem companies
pay to solve — so MIT would give away the only thing here with a price on it,
irreversibly, since a published MIT grant cannot be withdrawn from copies
already made. Nothing else in the project has that property.

The cost is that settle is *source-available*, not OSI open source; the
noncommercial restriction is what disqualifies it, and it is deliberate. This
costs nothing that matters for the repository's actual purpose: the code stays
fully readable, runnable and forkable, and reading, running or evaluating it is
explicitly free use.

PolyForm was chosen over a bespoke licence because it is lawyer-drafted,
standard and short enough to read, and over the Business Source License because
BSL's time-delayed conversion to open source solves a problem this project does
not have.

### The benchmark numbers are generated, and CI proves it

`bench/check.py` re-runs the benchmark and compares it to the committed results.
A change that moves the numbers has to move the committed numbers too, so the
README cannot slowly become a claim about a version of the engine that no longer
exists.
