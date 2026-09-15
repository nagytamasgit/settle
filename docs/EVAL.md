# Evaluation

What the benchmark measures, how it measures it, and — the part that matters
most — what it does not tell you.

Results live in [`bench/results/`](../bench/results/). Regenerate with
`make bench`; verify the committed numbers still hold with `make bench-check`,
which is what CI runs.

---

## The two caveats, first

These belong at the top, not in a footnote.

### 1. The generator's noise is a model of reality, not reality

Every number here is measured against data this project generated. The noise is
the noise the author thought to implement: references reformatted, truncated,
mistyped or deleted; bank fees, rounding and overpayment; merged and split
payments; colliding amounts. Real statements contain failure modes not in that
list — a customer paying two unrelated companies from one account, a reference
field truncated by an intermediary bank at a different length than modelled,
credit notes, currency conversion in the middle.

So: the shape of the curve is informative, the absolute numbers are not a
promise about your ledger. What the benchmark honestly supports is a *relative*
claim — how the engine degrades as a known quantity of a known kind of mess is
added, and which kind hurts most.

### 2. The ceiling on reference parsing is a parameter, not a finding

When reference noise deletes an identifier entirely, no parser can recover it —
deterministic, model-backed, or human. The share of references the generator
destroys is a dial (`NoiseProfile.reference`), so "the matcher achieves X% at
noise level Y" is partly a statement about where that dial was set.

This particularly limits what the benchmark can say about the model tier. A
generated "hard" reference is hard in the way the generator's `_corrupt_reference`
makes it hard, which is not the same as the way a human is unpredictable.
Measuring the model tier properly needs real references, which needs real data,
which is the thing this project does not have.

---

## What is measured

For each configuration the benchmark generates a ledger and a statement with a
known mapping, runs `reconcile()` with default policy, and compares.

A **pair** is one `(payment, invoice)` allocation. Ground truth is the set of
pairs the generator built; a prediction is a pair the engine actually applied.

| Metric | Definition |
|---|---|
| precision | correct pairs / predicted pairs |
| recall | correct pairs / true pairs |
| F1 | harmonic mean of the two |
| sent to review | payments needing a human / all payments |
| runtime | wall-clock for `reconcile()` alone |

Each row is the mean of 5 seeded runs over 400 invoices. The code is
deterministic, so re-running reproduces to within 0.001.

### A payment sent to review is not a wrong answer

This is the one scoring decision that shapes everything else.

Candidates that were ranked but not applied are **excluded** from predictions.
When the engine declines, it costs recall and appears in the review column, but
it never costs precision — because the engine did not claim anything.

The alternative rules are both worse. Scoring a review item as an error punishes
exactly the behaviour the engine was built to have. Ignoring review items
entirely lets a system that declines 90% of its input report perfect precision.
Splitting the cost — recall and the review rate — puts it where a finance team
actually feels it: work still on someone's desk.

### Invariants stay on during the benchmark

`verify_invariants` is not disabled to make runtime look better. The numbers are
for the engine as it actually runs, safety net included.

---

## Results summary

Full tables: [`bench/results/results.md`](../bench/results/results.md).

**Precision barely moves; recall carries the cost.** From clean data to 0.5
noise on all four dimensions, precision goes 100.0% → 97.1% while recall goes
100.0% → 70.2% and the review queue goes 0% → 19.7%. That is the designed
behaviour, stated as a measurement: the engine converts uncertainty into human
work rather than into wrong allocations.

**No single kind of mess matters much on its own.** Each dimension alone at 0.5
scores 99.1% recall or better. The signals are redundant — destroy the
references and exact amounts still identify the payment; collide the amounts and
the references still do.

**Reference noise is the multiplier.** Pair it with any second dimension and
recall falls to 80.5%–89.8%. Once the identifier is gone, the amount is the only
thing left to match on, and the second dimension is precisely what makes the
amount unreliable. The worst pairing is reference + structure (80.5%): a
reference-less payment covering several invoices has to be found by subset-sum
alone, and subset-sum finds candidates without being able to confirm them.

This is the practical finding, and it is actionable in a way an aggregate score
is not: **the highest-value thing a finance team can do to automate
reconciliation is get customers to put a usable reference on the payment.** It
is worth more than any improvement to the matcher.

---

## What the benchmark found that testing did not

The benchmark's first run scored 0.94 recall on data with *no noise at all*.
Clean references, exact amounts, one payment per invoice — and one in sixteen
payments was going to review.

The cause: sequential invoice numbers differ by a single digit, so
`INV-2026-0042` scores 90 against `INV-2026-0142` on the fuzzy comparison —
above the threshold. Every clean reference was matching several invoices, and
the resulting ties went to a human exactly as designed. Every unit test passed,
because each one used a handful of invoices with dissimilar numbers.

The fix (exact matching wins outright; fuzzy runs only when nothing matched
exactly) is in `_reference_evidence`, with a regression test named after the
cause. This is the argument for having a benchmark at all: it exercises the
engine at a scale and density where emergent problems exist, and a test suite
written by the same person who wrote the bug will not find that class of bug.

---

## Known limitations of the harness

- **One currency.** Generated cases are EUR only. Cross-currency rejection is
  covered by unit tests and a property, not by the benchmark.
- **Customers are always resolvable.** Every generated payment carries a
  counterparty name matching a ledger customer. Real statements have unresolvable
  payers, which would raise the review rate across the board.
- **Runtime is not a performance claim.** It is wall-clock on one machine,
  reported to show the bounded search stays bounded. It is excluded from the
  reproducibility check for that reason.
- **The model tier is not benchmarked.** It needs an API key and network, so CI
  never exercises it and no committed number depends on it. Its contract — propose
  only, always verified, degrade to the rules tier on failure — is covered by
  tests with a fake client.
