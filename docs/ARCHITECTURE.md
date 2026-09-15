# Architecture

## The shape

```
                    CLI ──┐
                          ├──► reconcile()  ──►  ReconciliationResult
       FastAPI /reconcile ─┤     (pure)                  │
                          │                              ▼
              benchmark ──┘                     result_to_dict()
                                                   │        │
                                               JSON files  HTTP body
```

One function does the work. Three callers wrap it and none of them contains a
matching rule, which is why `settle run` and `POST /reconcile` cannot disagree —
an integration test asserts they produce identical output.

## Layers, and which way the arrows point

```
src/settle/
├── domain/          depends on: nothing outward
│   ├── money.py         Decimal, never float
│   ├── models.py        frozen dataclasses
│   ├── config.py        every policy, as values
│   ├── normalize.py     deterministic reference parsing
│   ├── ports.py         what the domain needs from outside (Protocols)
│   ├── search.py        bounded subset-sum, exact apportionment
│   ├── score.py         the confidence weight table
│   ├── invariants.py    the money laws
│   ├── errors.py
│   └── match.py         the engine
├── parse/           depends on: domain
│   ├── base.py          re-exports the port, adds the tier chain
│   ├── rules.py         adapter over normalize.py
│   └── model.py         adapter over the Anthropic SDK (optional)
├── io/              depends on: domain
│   ├── schemas.py       Pydantic boundary types
│   ├── csv_io.py        the integration contract
│   └── json_io.py       the single serialiser
├── api/             depends on: domain, io
└── cli.py           depends on: domain, io, parse
```

The dependency arrow points inward everywhere. `domain/` imports nothing from
`parse/`, `io/`, or `api/` — which is the concrete meaning of "you could delete
the model tier and the matcher would not notice".

### The app, one layer further out

`settle-app` in [`app/`](../app/) is a fourth adapter beside `api/` and
`cli.py`, but in a separate distribution:

```
app/src/settle_app/   depends on: settle (the whole public surface)
├── settings.py       config, and the startup refusals
├── security.py       passwords, sessions, CSRF, throttling
├── store.py          sqlite3: run history
├── artifacts.py      the files a run produced
├── mapping.py        your column names -> settle's
├── mapper_model.py   the optional model tier for the residue
├── views.py          result files -> display rows
├── runner.py         the one place reconcile() is called
├── app.py            FastAPI, routes
└── cli.py            settle-app serve | backup | purge
```

The arrow points one way only: nothing under `src/settle/` imports anything
under `app/`, and `app/tests/test_app_separation.py` checks it. That is what
makes the engine installable on its own with no web stack at all.

The request path is `upload → mapping.to_canonical → csv_io.read_* →
reconcile() → csv_io.write_all → views.build_views → HTML`. Note where the
mapping sits: it normalises the file *before* the engine's readers, so the
engine has no branch for "came from the web app". And note that `views` reads
the written files rather than the in-memory result — the results page is a view
of the artefacts, which is what keeps screen and download identical.

### Why the port lives in `domain/`, not `parse/`

`ReferenceParser` is a Protocol in `domain/ports.py`. The domain declares what
it needs; `parse/` supplies implementations. If the interface lived in `parse/`,
`domain.match` would have to import from an outer layer to type its own
argument, and the direction would be inverted for no benefit.

`parse/base.py` re-exports it, so an implementer's import reads
`from settle.parse import ReferenceParser` as you would expect.

## The matching pass

For each payment, in `(date, id)` order against a running ledger of open
balances:

1. **Parse** the reference into candidate identifiers (rules; model only for the
   residue).
2. **Resolve** the customer — by id, then by exact name, then fuzzily.
3. **Narrow** to eligible invoices: open, same currency, inside the date window,
   right customer. An unresolvable payer narrows to reference-named invoices only.
4. **Gather reference evidence.** Exact token matches win outright; the fuzzy
   tier runs only if nothing matched exactly (see `DECISIONS.md` — this one was
   found by the benchmark).
5. **Generate candidates**, each a complete explanation of the payment:
   exact single, fee-tolerance, partial, overpayment, and bounded combinations.
6. **Score** each against the weight table.
7. **Rank**, then decide: apply the winner, or hand the ranked list to a human.
8. **Apply** — decrement balances, emit allocations, fees and residual.

Then build invoice states, the report, and verify the invariants.

### Why greedy

Optimal assignment across a whole statement is NP-hard, and an answer that
changes because an unrelated invoice moved is not auditable. The greedy pass is
stable and explainable, and it is safe whatever it picks because the invariants
hold regardless of the choice.

## The confidence table

Every number that moves a score is a named constant in `domain/score.py`. A
reviewer can add them up by hand.

| Term | Value | When |
|---|---:|---|
| base: exact single | 0.70 | one invoice, balance equals the payment |
| base: combination | 0.62 | several invoices summing to the payment |
| base: fee tolerance | 0.58 | short by less than the tolerance |
| base: partial | 0.50 | short by more than the tolerance |
| base: overpayment | 0.48 | pays the invoice and leaves a residual |
| reference, all | +0.29 | every invoice in the candidate was named |
| reference, some | +0.16 × coverage | some were named |
| customer resolved | +0.06 | the bank line was tied to a customer |
| unique explanation | +0.10 | nothing else explains this amount |
| sole open invoice | +0.04 | the customer had only one |
| combination size | −0.04 each | per invoice beyond the first |
| fee consumption | −0.06 × ratio | proportional to tolerance used |

Clamped to `[0, 0.99]`. Default threshold **0.80**; below it, a human decides.

Worked examples:

- Exact amount **and** reference → `0.70 + 0.29 = 0.99`. Applied.
- Exact amount, no reference, uniquely explained, customer known →
  `0.70 + 0.06 + 0.10 = 0.86`. Applied.
- Exact amount, no reference, **two** invoices share it → `0.76`, and a tie.
  Review, on both counts.
- Three invoices, all referenced, customer known, uniquely explained →
  `0.62 + 0.29 + 0.06 + 0.10 − 0.08 = 0.99`. Applied.
- 2.9% short, referenced, customer known →
  `0.58 + 0.29 + 0.06 − 0.058 ≈ 0.87`. Applied, and the invoice closes as
  `settled_with_fee`, not `paid`.

The threshold's effect is visible in the benchmark: precision stays near 100%
across every noise level because candidates below it are never applied.

## Bounds on the search

Subset-sum is NP-complete, so three bounds apply, all configuration:

- **width** — `max_combination_candidates` (24), applied after moving referenced
  invoices to the front so narrowing never drops the ones the customer named
- **depth** — `max_combination_size` (4)
- **work** — `search_node_budget` (50 000 nodes)

Two prunes make it tractable in practice: stop when the running sum exceeds the
upper bound, and stop when everything remaining cannot reach the target. Both
depend on all amounts being positive, which the search asserts.

The exact-sum search runs first; the tolerance window is only opened if nothing
adds up exactly, which keeps the common case cheap and makes "this is the only
combination that works" a meaningful signal. When the budget runs out the result
says so, the run warns, and the uniqueness bonus is withheld.

## Invariants at runtime

`invariants.check_result` runs at the end of every `reconcile()` by default, not
only in tests. It collects *all* violations before raising, because when
something is wrong the whole picture beats the first broken cent.

If it fires, the result is discarded rather than returned: the API answers 500
and the CLI exits 3. A reconciliation that broke a money law is not a
reconciliation to patch up.

## Testing strategy

| Layer | What it proves |
|---|---|
| `tests/property/` | Hypothesis: money is conserved, nothing is over-allocated, output is deterministic, input order is irrelevant, inputs are not mutated — on ledgers nobody wrote by hand |
| `tests/unit/` | Each hard case once: fee vs part payment, combinations, ties, budget exhaustion, reference formats, and the invariant checker itself (by breaking things on purpose) |
| `tests/integration/` | CLI and API end to end, and that they produce identical output |
| `bench/` | How well it works, at scale, against known ground truth |

CI runs the property tests at 1 000 examples each.
