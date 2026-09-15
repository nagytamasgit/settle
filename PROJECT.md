# settle: a payment-to-invoice reconciliation engine

*Design brief, 11 September 2026.*

---

## 1. The problem it solves

Money arrives in a bank account. The finance team has to answer one question for every incoming line: **which invoices did this payment pay?**

In practice the answer is rarely clean. One transfer covers three invoices. A customer pays two thirds now and the rest next month. An international payment arrives 2.9% short because a bank took a fee in the middle. The payment reference says "INV-2026-0042", or "szamla 42", or "0042/2026", or the customer's dog's name. Two invoices for the same customer carry the same amount on the same day. Every finance team on earth resolves this by hand, and every enterprise wants it automated.

The goal of settle is to take an invoice ledger and a bank statement and return, for every payment, a set of allocations to invoices, with a confidence score and a stated reason, such that every allocation conserves money to the cent and anything uncertain is routed to a human rather than guessed.

## 2. The principles it is built on

Three commitments shape every decision in this repository.

**Correctness before features.** This is a small, unforgiving domain where correctness is binary. Money as `Decimal`, never float. A pure domain layer with no I/O and no framework in it. Property-based tests that state the invariants of money: conservation, no over-allocation, determinism. Idempotent processing. Clean separation of matching logic from data loading and from the API.

**A model only where a model is warranted.** The engine is deterministic at its core and uses a model only where the input is messy by nature: parsing free-text payment references. Even there the model proposes, the engine verifies against the ledger, and a human confirms anything below a confidence threshold. That constraint is the thesis, and it is stated at the top of the README.

**A measured answer to "how well does it work?"** — which most reconciliation projects cannot give. Because the test data is generated, ground truth exists by construction. The benchmark injects increasing noise and reports precision and recall at each level. The README can therefore say exactly how much mess the matcher survives before it needs a person.

## 3. Thesis, as it appears at the top of the README

> Reconciliation is a logic problem with a messy edge.
>
> Matching payments to invoices is deterministic: amounts, dates, customers and references are facts, and a payment either explains a set of invoices to the cent or it does not. settle solves that part with search and rules, and proves the invariants with property-based tests.
>
> The messy edge is the payment reference, a free-text field that customers fill in however they like. That is the only place a model is used, and it only proposes candidates; the engine still verifies every proposal against the ledger, and anything below the confidence threshold goes to a human.
>
> Because the test data is generated, the truth is known. The benchmark shows how far the matcher goes before it needs you.

## 4. Scope

### In scope
- **Exact matching**: amount, currency, customer, date window
- **Combination matching**: one payment covering several invoices (bounded subset-sum search with deterministic tie-breaking)
- **Partial payments and overpayments**, with explicit residual handling
- **Fee tolerance**: a payment short by a small, configurable percentage or fixed amount is matched and the difference recorded as a fee, never silently absorbed
- **Reference parsing**: normalising invoice identifiers written in many formats (`INV-2026-0042`, `2026/0042`, `szamla 42`, `#42`), with a deterministic normaliser first and an optional model-backed parser for the residue
- **Ambiguity handling**: when more than one allocation explains a payment equally well, the engine returns all candidates ranked and marks the payment for review; it never picks arbitrarily
- **Confidence scoring** with a stated reason for every allocation ("exact amount and reference", "combination of 3 invoices, reference matched 2 of 3", "amount within fee tolerance, no reference")
- **Invariants enforced in code and in tests**: money is conserved, no allocation exceeds its invoice, the same input always yields the same output
- **Inputs**: two CSVs (invoices, bank lines) and a JSON schema for both
- **Outputs**: allocations as JSON and CSV, a review queue, and a run report
- **A CLI** and **a FastAPI endpoint** wrapping the same domain function
- **Synthetic data generator** with controllable noise
- **Benchmark** with a results table and chart: precision, recall, review-queue size, and runtime at each noise level

### Out of scope, and why (recorded in DECISIONS.md)
- Bank or ERP integrations (the CSV contract is the integration point; adapters are a later concern)
- A database (the engine is a pure function over two inputs; persistence belongs to the caller)
- A web UI (the review queue is data, not a screen)
- Multi-currency conversion (currency must match; FX is a different problem with its own invariants)
- Training a custom model (a small hosted or local model behind an interface is enough; the point is where the model sits, not how good it is)

## 5. The logic that makes it hard, explicitly

These are the parts that carry the risk, and the parts the tests target.

1. **Subset-sum with bounds.** Finding which invoices sum to a payment is exponential in general. The engine bounds the search by customer, date window and candidate count, uses a deterministic ordering, and returns the best-scoring combination, or all tied combinations, within a fixed budget. The budget and the ordering are documented.
2. **Tie-breaking is a policy, not an accident.** Two invoices of equal amount on the same day: oldest first? Reference match first? The rule is explicit, configurable and tested. Whatever the rule, the result must be stable across runs.
3. **Fee tolerance without leakage.** A tolerance of 3% must not let a 3% short payment silently close an invoice. The shortfall is recorded as a fee line and the invoice is marked as settled-with-fee, which is a different state from paid.
4. **Partial payment residuals.** Paying 200 of a 300 invoice leaves 100 open, and the next payment must be able to close it. Residuals are first-class.
5. **Reference normalisation.** A deterministic normaliser (strip prefixes, extract digit groups, handle year/number order) handles the common cases. The model-backed parser handles only what the normaliser could not, and its output is only a candidate list that the engine then verifies by amount and customer.
6. **Conservation as a theorem.** For every run: sum of allocations plus fees plus unallocated residual equals the sum of payments, to the cent. This is a Hypothesis property, not an example test.

## 6. Tech stack, and why each item is there

| Tool | Role | Why this and not something else |
|---|---|---|
| Python 3.12 | Language | Modern typing, plus the `Decimal` and `dataclasses` the domain leans on |
| `Decimal` | Money | Floats are disqualifying in this domain |
| Pydantic v2 | Schemas for invoices, bank lines, allocations, API contracts | Validated boundaries; the same schema serves the CLI, the API and the benchmark |
| pure `dataclasses` in the domain | Domain objects | The domain layer stays framework-free |
| Hypothesis | Property-based tests | Money invariants are properties, not examples |
| pytest | Test runner | Standard |
| FastAPI | `POST /reconcile` endpoint | Pydantic-native, so the boundary schema is the one the domain already uses; a thin wrapper |
| Typer | CLI | `settle run invoices.csv bank.csv` |
| rapidfuzz | Fuzzy reference matching, deterministic tier | Fast, no model needed for most cases |
| Anthropic SDK (or a small local model behind the same interface) | Reference parsing for the residue only | Shows where a model belongs; interface-isolated so it can be swapped or disabled |
| structlog | Logging | Structured logs for every allocation decision |
| Docker | Reproducible run | `docker run settle …` |
| GitHub Actions | CI | ruff, mypy strict, pytest with coverage threshold, benchmark smoke run on every PR |
| matplotlib | Benchmark chart | One chart in the README, generated by the benchmark |

Deliberately absent: a database, an ORM, a task queue, a frontend, an agent framework. Each absence is a decision and is written down.

## 7. Repository layout

```
settle/
├── src/settle/
│   ├── domain/
│   │   ├── models.py        # Invoice, Payment, Allocation, Fee, Residual
│   │   ├── normalize.py     # reference normaliser, deterministic
│   │   ├── match.py         # exact, partial, combination matching
│   │   ├── score.py         # confidence and reasons
│   │   └── invariants.py    # conservation and bounds, used by tests and at runtime
│   ├── parse/
│   │   ├── base.py          # ReferenceParser interface
│   │   ├── rules.py         # deterministic parser
│   │   └── model.py         # model-backed parser, optional
│   ├── io/                  # CSV and JSON loaders and writers
│   ├── api/                 # FastAPI app
│   └── cli.py               # Typer app
├── bench/
│   ├── generate.py          # synthetic ledger and statement with controllable noise
│   ├── run.py               # runs settle at each noise level, writes results
│   └── results/             # table and chart, committed
├── tests/
│   ├── unit/
│   ├── property/            # Hypothesis
│   └── integration/         # CLI and API end to end
├── docs/
│   ├── DECISIONS.md
│   ├── EVAL.md
│   └── ARCHITECTURE.md
├── Dockerfile
└── .github/workflows/ci.yml
```

## 8. The benchmark, in detail

The generator creates a ledger of invoices and a bank statement that pays them, with a known mapping. Noise is applied in named, independent dimensions so the results can say which kind of mess hurts most:

- **Reference noise**: references reformatted, truncated, misspelled, or removed
- **Amount noise**: fees deducted, rounding differences, overpayments
- **Structure noise**: payments merged (one line pays several invoices), split (one invoice paid in several lines)
- **Collision noise**: invoices with identical amounts for the same customer in the same window

For each noise level the benchmark reports precision, recall, the share of payments sent to review, and runtime. The table and a chart are committed to the repository and reproduced by `make bench`.

Two honest statements belong in EVAL.md: that the generator's noise is a model of reality and not reality, and that the ceiling for reference parsing is set by how ambiguous the generated references are, which is a parameter, not a finding.

## 9. Milestones and definition of done

| # | Content | Done when |
|---|---|---|
| M1 | Domain models, exact matching, invariants, property tests | Hypothesis proves conservation, no over-allocation, determinism, on generated inputs of arbitrary size |
| M2 | Partial payments, fee tolerance, residuals | Tests cover a 300 invoice paid 200 then 100, and a payment 2.9% short recorded as fee-settled, not paid |
| M3 | Combination matching with bounded search and explicit tie-breaking | A payment covering three invoices is found; ties return all candidates and a review flag; search respects its budget on adversarial inputs |
| M4 | Deterministic reference normaliser and rapidfuzz tier | The listed reference formats all resolve; unknown formats fall through cleanly |
| M5 | Model-backed parser behind the interface, with verification | The model's candidates are always checked against the ledger; disabling the model degrades recall but never breaks correctness |
| M6 | CLI, API, Docker, CI | `settle run` and `POST /reconcile` produce identical output for identical input; CI is green with coverage above the threshold |
| M7 | Generator, benchmark, chart, three docs, README | The chart is in the README, EVAL.md states the two caveats, DECISIONS.md lists every omission with a reason |

**Minimum shippable version if time runs out:** M1 to M4 plus M6 and a reduced M7 (benchmark without the model tier). That is already a complete, defensible engine.

**Where the risk sits:** M3 and M7. Bounded search that stays deterministic under adversarial input, and a benchmark whose numbers survive contact with a re-run.
