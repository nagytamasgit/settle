"""The matcher: a pure function from (ledger, statement, policy) to allocations.

No I/O, no framework, no global state. The CLI, the API and the benchmark all
call :func:`reconcile` and serialise whatever comes back, which is why they
cannot drift apart.

Payments are processed in a deterministic order against a running ledger of
open balances. That is a greedy pass, not a globally optimal assignment:
optimal assignment across a whole statement is NP-hard, and a finance team
cannot audit an answer that changes because an unrelated invoice moved. The
greedy pass is stable, explainable, and safe by construction, because the
invariants in :mod:`settle.domain.invariants` hold whatever it picks.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from typing import Any

import structlog

from . import invariants
from .config import (
    DEFAULT_CONFIG,
    MIN_COMBINATION_SIZE,
    AmbiguityPolicy,
    MatchConfig,
    TieBreak,
)
from .errors import DuplicateIdError
from .models import (
    Allocation,
    Candidate,
    Fee,
    Invoice,
    InvoiceState,
    InvoiceStatus,
    Payment,
    PaymentResult,
    PaymentStatus,
    ReconciliationResult,
    Residual,
    ReviewItem,
    ReviewReason,
    RunReport,
    Strategy,
)
from .money import ZERO
from .normalize import RefToken, canonical, normalize_invoice_number, similarity
from .ports import ParseOutcome, ReferenceParser, default_parse
from .score import Evidence, score
from .search import apportion, find_subsets

ENGINE_VERSION = "0.1.0"

log = structlog.get_logger("settle.match")


@dataclass(slots=True)
class _Ledger:
    """Mutable open balances for one run. Never escapes :func:`reconcile`."""

    invoices: dict[str, Invoice]
    balance: dict[str, Decimal]
    fee_written: dict[str, Decimal]
    by_seq: dict[int, list[str]]
    ref_token: dict[str, RefToken | None]
    customer_by_name: dict[str, str | None]

    @classmethod
    def build(cls, invoices: Sequence[Invoice]) -> _Ledger:
        by_seq: dict[int, list[str]] = {}
        ref_token: dict[str, RefToken | None] = {}
        customer_by_name: dict[str, str | None] = {}
        for invoice in invoices:
            token = normalize_invoice_number(invoice.number)
            ref_token[invoice.id] = token
            if token is not None:
                by_seq.setdefault(token.seq, []).append(invoice.id)
            if invoice.customer_name:
                key = canonical(invoice.customer_name)
                if key:
                    # A name shared by two customers identifies neither.
                    existing = customer_by_name.get(key, invoice.customer_id)
                    customer_by_name[key] = (
                        invoice.customer_id if existing == invoice.customer_id else None
                    )
        return cls(
            invoices={inv.id: inv for inv in invoices},
            balance={inv.id: inv.amount for inv in invoices},
            fee_written={inv.id: ZERO for inv in invoices},
            by_seq=by_seq,
            ref_token=ref_token,
            customer_by_name=customer_by_name,
        )


def reconcile(
    invoices: Iterable[Invoice],
    payments: Iterable[Payment],
    *,
    config: MatchConfig = DEFAULT_CONFIG,
    parser: ReferenceParser | None = None,
) -> ReconciliationResult:
    """Allocate every payment to invoices, or route it to a human.

    Args:
        invoices: the open ledger. Ids must be unique.
        payments: incoming bank lines. Ids must be unique.
        config: the policy for this run. See :class:`MatchConfig`.
        parser: optional reference parser. Defaults to the deterministic rules;
            inject :class:`settle.parse.model.ModelReferenceParser` to add the
            model tier. Whatever a parser proposes is verified against the
            ledger before it can move money.

    Returns:
        A complete, serialisable :class:`ReconciliationResult`.

    Raises:
        DuplicateIdError: if an id repeats.
        InvariantViolation: if the result would break a money law and
            ``config.verify_invariants`` is set.
    """
    invoice_list = tuple(invoices)
    payment_list = tuple(payments)
    _reject_duplicate_ids(invoice_list, payment_list)

    ledger = _Ledger.build(invoice_list)
    allocations: list[Allocation] = []
    fees: list[Fee] = []
    residuals: list[Residual] = []
    results: list[PaymentResult] = []
    review: list[ReviewItem] = []
    warnings: list[str] = []
    model_calls = 0
    truncations = 0

    for payment in sorted(payment_list, key=lambda p: (p.date, p.id)):
        outcome = (
            parser.parse(payment.reference)
            if parser is not None
            else default_parse(payment.reference)
        )
        model_calls += outcome.model_calls
        customer_id = _resolve_customer(payment, ledger, config)
        eligible = _eligible_invoices(payment, customer_id, ledger, config, outcome)
        ref_hits = _reference_evidence(eligible, ledger, payment, outcome, config)

        candidates, truncated = _build_candidates(
            payment, eligible, ref_hits, ledger, config, customer_id is not None
        )
        if truncated:
            truncations += 1

        order_key = {
            invoice_id: _tie_break_key(ledger.invoices[invoice_id], config)
            for invoice_id in eligible
        }
        result, item = _decide(payment, candidates, truncated, config, order_key)
        results.append(result)
        if item is not None:
            review.append(item)

        if result.chosen is not None:
            _apply(result.chosen, ledger, allocations, fees)

        applied = result.chosen.allocations if result.chosen else ()
        allocated = sum((a.amount for a in applied), ZERO)
        if allocated < payment.amount:
            residuals.append(Residual(payment_id=payment.id, amount=payment.amount - allocated))

        log.info(
            "payment_decided",
            payment_id=payment.id,
            status=result.status.value,
            strategy=result.chosen.strategy.value if result.chosen else None,
            confidence=str(result.chosen.confidence) if result.chosen else None,
            invoices=list(result.chosen.invoice_ids) if result.chosen else [],
            candidates=len(result.ranked_candidates),
            review_reason=result.review_reason.value if result.review_reason else None,
            parser=outcome.source,
        )

    if truncations:
        warnings.append(
            f"{truncations} payment(s) hit the combination search budget "
            f"({config.search_node_budget} nodes); results for those are not proven unique"
        )

    invoice_states = _invoice_states(ledger, allocations)
    report = _build_report(
        invoice_list,
        payment_list,
        results,
        allocations,
        fees,
        residuals,
        invoice_states,
        truncations,
        model_calls,
    )
    reconciliation = ReconciliationResult(
        payments=tuple(sorted(results, key=lambda r: r.payment_id)),
        allocations=tuple(allocations),
        fees=tuple(fees),
        residuals=tuple(residuals),
        invoice_states=tuple(invoice_states),
        review_queue=tuple(review),
        report=report,
        warnings=tuple(warnings),
    )
    if config.verify_invariants:
        invariants.check_result(invoice_list, payment_list, reconciliation)
    return reconciliation


def _reject_duplicate_ids(invoices: Sequence[Invoice], payments: Sequence[Payment]) -> None:
    """Identity is what makes a run idempotent; a repeated id is a broken export."""
    for label, ids in (
        ("invoice", [i.id for i in invoices]),
        ("payment", [p.id for p in payments]),
    ):
        duplicates = sorted(value for value, count in Counter(ids).items() if count > 1)
        if duplicates:
            raise DuplicateIdError(f"duplicate {label} id(s): {duplicates}")


def _resolve_customer(payment: Payment, ledger: _Ledger, config: MatchConfig) -> str | None:
    """Tie a bank line to a customer by id, then by exact name, then fuzzily."""
    if payment.customer_id and payment.customer_id in {
        inv.customer_id for inv in ledger.invoices.values()
    }:
        return payment.customer_id
    if not payment.counterparty:
        return None
    key = canonical(payment.counterparty)
    if not key:
        return None
    exact = ledger.customer_by_name.get(key)
    if exact is not None:
        return exact
    best_id: str | None = None
    best_score = config.fuzzy_ref_threshold
    for name_key, customer_id in sorted(ledger.customer_by_name.items()):
        if customer_id is None:
            continue
        value = similarity(name_key, key)
        if value > best_score:
            best_score, best_id = value, customer_id
    return best_id


def _eligible_invoices(
    payment: Payment,
    customer_id: str | None,
    ledger: _Ledger,
    config: MatchConfig,
    outcome: ParseOutcome,
) -> list[str]:
    """Open invoices this payment is allowed to touch, in deterministic order."""
    earliest = payment.date - timedelta(days=config.date_window_days_after)
    latest = payment.date + timedelta(days=config.date_window_days_before)

    def in_window(invoice: Invoice) -> bool:
        return earliest <= invoice.issue_date <= latest

    def open_and_same_currency(invoice: Invoice) -> bool:
        return ledger.balance[invoice.id] > ZERO and invoice.currency == payment.currency

    if customer_id is not None and config.require_customer_match:
        pool = [
            inv
            for inv in ledger.invoices.values()
            if inv.customer_id == customer_id and open_and_same_currency(inv) and in_window(inv)
        ]
    elif customer_id is None and config.require_customer_match:
        # No customer, so a reference is the only acceptable identification.
        pool = [
            ledger.invoices[invoice_id]
            for invoice_id in _invoices_named_by(outcome.tokens, ledger)
            if open_and_same_currency(ledger.invoices[invoice_id])
            and in_window(ledger.invoices[invoice_id])
        ]
    else:
        pool = [
            inv
            for inv in ledger.invoices.values()
            if open_and_same_currency(inv) and in_window(inv)
        ]
    return [inv.id for inv in sorted(pool, key=lambda inv: _tie_break_key(inv, config))]


def _invoices_named_by(tokens: Sequence[RefToken], ledger: _Ledger) -> list[str]:
    named: list[str] = []
    for token in tokens:
        for invoice_id in ledger.by_seq.get(token.seq, ()):
            invoice_token = ledger.ref_token[invoice_id]
            if invoice_token is not None and token.matches(invoice_token):
                named.append(invoice_id)
    return sorted(set(named))


def _reference_evidence(
    eligible: list[str],
    ledger: _Ledger,
    payment: Payment,
    outcome: ParseOutcome,
    config: MatchConfig,
) -> dict[str, bool]:
    """Which eligible invoices the payment reference names.

    Exact token matching wins outright: when the reference names *any* invoice
    exactly, near-misses are not considered at all.

    That precedence is not a nicety, it is required. Invoice numbers in a
    sequence differ by a single digit, so a fuzzy comparison of
    ``INV-2026-0042`` against ``INV-2026-0142`` scores 90 — comfortably over any
    threshold loose enough to survive a typo. Letting both tiers contribute
    turns a perfectly clear reference into a tie between several invoices and
    sends it to a human. The fuzzy tier only earns its keep on references that
    matched nothing exactly, which is precisely the mistyped case it is for.
    """
    exact = {invoice_id: _token_match(invoice_id, ledger, outcome) for invoice_id in eligible}
    if any(exact.values()):
        return exact
    return {
        invoice_id: similarity(ledger.invoices[invoice_id].number, payment.reference)
        >= config.fuzzy_ref_threshold
        for invoice_id in eligible
    }


def _token_match(invoice_id: str, ledger: _Ledger, outcome: ParseOutcome) -> bool:
    invoice_token = ledger.ref_token[invoice_id]
    if invoice_token is None:
        return False
    return any(token.matches(invoice_token) for token in outcome.tokens)


def _tie_break_key(invoice: Invoice, config: MatchConfig) -> tuple[Any, ...]:
    """Deterministic ordering of invoices under the configured policy.

    Ordering is not choosing: it makes output stable and puts the policy's
    preferred invoice first. Whether a tie may be *applied* is
    :class:`AmbiguityPolicy`.
    """
    match config.tie_break:
        case TieBreak.OLDEST_FIRST:
            return (invoice.issue_date.toordinal(), invoice.id)
        case TieBreak.NEWEST_FIRST:
            return (-invoice.issue_date.toordinal(), invoice.id)
        case TieBreak.LARGEST_FIRST:
            return (-invoice.amount, invoice.id)
        case TieBreak.SMALLEST_FIRST:
            return (invoice.amount, invoice.id)


def _build_candidates(
    payment: Payment,
    eligible: list[str],
    ref_hits: dict[str, bool],
    ledger: _Ledger,
    config: MatchConfig,
    customer_resolved: bool,
) -> tuple[list[Candidate], bool]:
    """Every way this payment could be explained, scored but not yet chosen."""
    if not eligible:
        return [], False

    balance = ledger.balance
    sole = len(eligible) == 1
    exact_count = sum(1 for i in eligible if balance[i] == payment.amount)
    tolerance_count = sum(
        1
        for i in eligible
        if balance[i] > payment.amount
        and balance[i] - payment.amount <= config.fee_tolerance_for(balance[i])
    )

    candidates: list[Candidate] = []
    for invoice_id in eligible:
        open_balance = balance[invoice_id]
        hit = ref_hits[invoice_id]
        if open_balance == payment.amount:
            candidates.append(
                _single(
                    Strategy.EXACT_SINGLE,
                    payment,
                    invoice_id,
                    payment.amount,
                    ZERO,
                    hit,
                    customer_resolved,
                    exact_count == 1,
                    sole,
                    ZERO,
                    config,
                )
            )
            continue
        if open_balance > payment.amount:
            shortfall = open_balance - payment.amount
            tolerance = config.fee_tolerance_for(open_balance)
            if shortfall <= tolerance:
                # The tolerance is the boundary between "a bank took a fee" and
                # "they paid part of it". Generating both would make every
                # short payment permanently ambiguous, so it is one or the other.
                candidates.append(
                    _single(
                        Strategy.FEE_TOLERANCE,
                        payment,
                        invoice_id,
                        payment.amount,
                        ZERO,
                        hit,
                        customer_resolved,
                        tolerance_count == 1,
                        sole,
                        shortfall / tolerance if tolerance > ZERO else ZERO,
                        config,
                        fee=shortfall,
                    )
                )
            elif config.allow_partial and (hit or sole):
                candidates.append(
                    _single(
                        Strategy.PARTIAL,
                        payment,
                        invoice_id,
                        payment.amount,
                        ZERO,
                        hit,
                        customer_resolved,
                        False,
                        sole,
                        ZERO,
                        config,
                    )
                )
            continue
        if config.allow_overpayment and (hit or sole):
            candidates.append(
                _single(
                    Strategy.OVERPAYMENT,
                    payment,
                    invoice_id,
                    open_balance,
                    payment.amount - open_balance,
                    hit,
                    customer_resolved,
                    False,
                    sole,
                    ZERO,
                    config,
                )
            )

    truncated = False
    if config.allow_combinations and len(eligible) >= MIN_COMBINATION_SIZE:
        combos, truncated = _combination_candidates(
            payment, eligible, ref_hits, ledger, config, customer_resolved
        )
        candidates.extend(combos)
    return candidates, truncated


def _single(
    strategy: Strategy,
    payment: Payment,
    invoice_id: str,
    allocate: Decimal,
    residual: Decimal,
    ref_hit: bool,
    customer_resolved: bool,
    unique: bool,
    sole: bool,
    fee_consumption: Decimal,
    config: MatchConfig,
    *,
    fee: Decimal = ZERO,
) -> Candidate:
    evidence = Evidence(
        strategy=strategy,
        invoice_count=1,
        ref_coverage=Decimal(1) if ref_hit else ZERO,
        customer_resolved=customer_resolved,
        unique_explanation=unique,
        sole_open_invoice=sole,
        fee_consumption=fee_consumption,
    )
    confidence, reasons = score(evidence)
    return Candidate(
        strategy=strategy,
        allocations=(Allocation(payment.id, invoice_id, allocate),),
        fees=((Fee(payment.id, invoice_id, fee),) if fee > ZERO else ()),
        residual=residual,
        confidence=confidence,
        reasons=reasons,
        ref_matched_invoice_ids=frozenset({invoice_id} if ref_hit else ()),
    )


def _combination_candidates(
    payment: Payment,
    eligible: list[str],
    ref_hits: dict[str, bool],
    ledger: _Ledger,
    config: MatchConfig,
    customer_resolved: bool,
) -> tuple[list[Candidate], bool]:
    """Bounded subset-sum over the eligible invoices.

    Referenced invoices are moved to the front before the width cap is applied,
    so narrowing the search never drops the invoices the customer actually
    named.
    """
    position = {invoice_id: index for index, invoice_id in enumerate(eligible)}
    ordered = sorted(eligible, key=lambda i: (not ref_hits[i], position[i]))
    ordered = ordered[: config.max_combination_candidates]
    amounts = [ledger.balance[i] for i in ordered]

    exact = find_subsets(
        amounts,
        payment.amount,
        payment.amount,
        min_size=2,
        max_size=config.max_combination_size,
        node_budget=config.search_node_budget,
        max_results=config.max_combinations_returned,
    )
    subsets = exact.subsets
    truncated = exact.truncated

    if not subsets:
        # Only widen to the fee tolerance when nothing adds up exactly. The
        # upper bound is derived from the tolerance rule, not guessed:
        # S - amount <= max(pct*S, abs)  =>  S <= max(amount/(1-pct), amount+abs)
        upper = max(
            payment.amount / (Decimal(1) - config.fee_tolerance_pct),
            payment.amount + config.fee_tolerance_abs,
        )
        if config.fee_tolerance_cap is not None:
            upper = min(upper, payment.amount + config.fee_tolerance_cap)
        relaxed = find_subsets(
            amounts,
            payment.amount,
            upper,
            min_size=2,
            max_size=config.max_combination_size,
            node_budget=config.search_node_budget,
            max_results=config.max_combinations_returned,
        )
        subsets = relaxed.subsets
        truncated = truncated or relaxed.truncated

    candidates: list[Candidate] = []
    for subset in subsets:
        invoice_ids = [ordered[index] for index in subset]
        candidate = _combination_candidate(
            payment,
            invoice_ids,
            ref_hits,
            ledger,
            config,
            customer_resolved,
            unique=len(subsets) == 1,
            truncated=truncated,
        )
        if candidate is not None:
            candidates.append(candidate)
    return candidates, truncated


def _combination_candidate(
    payment: Payment,
    invoice_ids: list[str],
    ref_hits: dict[str, bool],
    ledger: _Ledger,
    config: MatchConfig,
    customer_resolved: bool,
    *,
    unique: bool,
    truncated: bool,
) -> Candidate | None:
    balances = [ledger.balance[i] for i in invoice_ids]
    subtotal = sum(balances, ZERO)
    shortfall = subtotal - payment.amount
    fee_consumption = ZERO
    fees: tuple[Fee, ...] = ()

    if shortfall > ZERO:
        tolerance = config.fee_tolerance_for(subtotal)
        if shortfall > tolerance:
            return None
        shares = apportion(shortfall, balances)
        if any(balance - share <= ZERO for balance, share in zip(balances, shares, strict=True)):
            # A fee that swallows an invoice whole is not a fee.
            return None
        fees = tuple(
            Fee(payment.id, invoice_id, share)
            for invoice_id, share in zip(invoice_ids, shares, strict=True)
            if share > ZERO
        )
        balances = [balance - share for balance, share in zip(balances, shares, strict=True)]
        fee_consumption = shortfall / tolerance if tolerance > ZERO else ZERO

    matched = frozenset(i for i in invoice_ids if ref_hits[i])
    evidence = Evidence(
        strategy=Strategy.COMBINATION,
        invoice_count=len(invoice_ids),
        ref_coverage=Decimal(len(matched)) / Decimal(len(invoice_ids)),
        customer_resolved=customer_resolved,
        unique_explanation=unique,
        sole_open_invoice=False,
        fee_consumption=fee_consumption,
        search_truncated=truncated,
    )
    confidence, reasons = score(evidence)
    return Candidate(
        strategy=Strategy.COMBINATION,
        allocations=tuple(
            Allocation(payment.id, invoice_id, amount)
            for invoice_id, amount in zip(invoice_ids, balances, strict=True)
        ),
        fees=fees,
        residual=ZERO,
        confidence=confidence,
        reasons=reasons,
        ref_matched_invoice_ids=matched,
    )


def _decide(
    payment: Payment,
    candidates: list[Candidate],
    truncated: bool,
    config: MatchConfig,
    order_key: dict[str, tuple[Any, ...]],
) -> tuple[PaymentResult, ReviewItem | None]:
    """Rank, then either apply the winner or hand the whole list to a human."""
    if not candidates:
        reason = ReviewReason.SEARCH_TRUNCATED if truncated else ReviewReason.NO_CANDIDATE
        message = (
            "combination search hit its budget before finding an explanation"
            if truncated
            else "no open invoice matches this payment"
        )
        return (
            PaymentResult(
                payment_id=payment.id,
                status=PaymentStatus.UNMATCHED,
                review_reason=reason,
                search_truncated=truncated,
            ),
            ReviewItem(payment.id, reason, message),
        )

    ranked = tuple(
        sorted(candidates, key=lambda c: (-c.confidence, _candidate_order(c, order_key)))
    )
    top = ranked[0]
    tied = [c for c in ranked if c.confidence == top.confidence]

    if len(tied) > 1 and config.ambiguity_policy is AmbiguityPolicy.REVIEW:
        message = (
            f"{len(tied)} allocations explain this payment equally well "
            f"(confidence {top.confidence}); the engine will not choose between them"
        )
        return (
            PaymentResult(
                payment_id=payment.id,
                status=PaymentStatus.REVIEW,
                ranked_candidates=ranked,
                review_reason=ReviewReason.AMBIGUOUS,
                search_truncated=truncated,
            ),
            ReviewItem(payment.id, ReviewReason.AMBIGUOUS, message, tuple(tied)),
        )

    if top.confidence < config.confidence_threshold:
        message = (
            f"best explanation scored {top.confidence}, below the "
            f"{config.confidence_threshold} threshold: {top.reasons[0]}"
        )
        return (
            PaymentResult(
                payment_id=payment.id,
                status=PaymentStatus.REVIEW,
                ranked_candidates=ranked,
                review_reason=ReviewReason.BELOW_THRESHOLD,
                search_truncated=truncated,
            ),
            ReviewItem(payment.id, ReviewReason.BELOW_THRESHOLD, message, ranked[:3]),
        )

    status = PaymentStatus.MATCHED if top.residual == ZERO else PaymentStatus.PARTIALLY_MATCHED
    return (
        PaymentResult(
            payment_id=payment.id,
            status=status,
            chosen=top,
            ranked_candidates=ranked,
            search_truncated=truncated,
        ),
        None,
    )


def _candidate_order(
    candidate: Candidate, order_key: dict[str, tuple[Any, ...]]
) -> tuple[Any, ...]:
    """Ordering for equally-confident candidates, under the configured policy.

    Fewer invoices first (a simpler explanation beats an elaborate one of equal
    confidence), then the tie-break policy applied to the earliest-ranked
    invoice in the candidate, then ids so the order is total.
    """
    ranked = sorted(order_key[invoice_id] for invoice_id in candidate.invoice_ids)
    return (len(candidate.allocations), ranked[0], candidate.invoice_ids)


def _apply(
    candidate: Candidate,
    ledger: _Ledger,
    allocations: list[Allocation],
    fees: list[Fee],
) -> None:
    for allocation in candidate.allocations:
        ledger.balance[allocation.invoice_id] -= allocation.amount
        allocations.append(allocation)
    for fee in candidate.fees:
        ledger.balance[fee.invoice_id] -= fee.amount
        ledger.fee_written[fee.invoice_id] += fee.amount
        fees.append(fee)


def _invoice_states(ledger: _Ledger, allocations: Sequence[Allocation]) -> list[InvoiceState]:
    allocated: dict[str, Decimal] = dict.fromkeys(ledger.invoices, ZERO)
    for allocation in allocations:
        allocated[allocation.invoice_id] += allocation.amount

    states: list[InvoiceState] = []
    for invoice_id in sorted(ledger.invoices):
        invoice = ledger.invoices[invoice_id]
        paid = allocated[invoice_id]
        fee = ledger.fee_written[invoice_id]
        open_balance = invoice.amount - paid - fee
        if open_balance == ZERO and fee > ZERO:
            status = InvoiceStatus.SETTLED_WITH_FEE
        elif open_balance == ZERO:
            status = InvoiceStatus.PAID
        elif paid > ZERO:
            status = InvoiceStatus.PARTIALLY_PAID
        else:
            status = InvoiceStatus.OPEN
        states.append(
            InvoiceState(
                invoice_id=invoice_id,
                status=status,
                face_value=invoice.amount,
                allocated=paid,
                fee_written_off=fee,
                open_balance=open_balance,
            )
        )
    return states


def _build_report(
    invoices: Sequence[Invoice],
    payments: Sequence[Payment],
    results: Sequence[PaymentResult],
    allocations: Sequence[Allocation],
    fees: Sequence[Fee],
    residuals: Sequence[Residual],
    states: Sequence[InvoiceState],
    truncations: int,
    model_calls: int,
) -> RunReport:
    def count_payments(status: PaymentStatus) -> int:
        return sum(1 for r in results if r.status is status)

    def count_invoices(status: InvoiceStatus) -> int:
        return sum(1 for s in states if s.status is status)

    return RunReport(
        engine_version=ENGINE_VERSION,
        payments_total=len(payments),
        payments_matched=count_payments(PaymentStatus.MATCHED),
        payments_partially_matched=count_payments(PaymentStatus.PARTIALLY_MATCHED),
        payments_in_review=count_payments(PaymentStatus.REVIEW),
        payments_unmatched=count_payments(PaymentStatus.UNMATCHED),
        invoices_total=len(invoices),
        invoices_paid=count_invoices(InvoiceStatus.PAID),
        invoices_settled_with_fee=count_invoices(InvoiceStatus.SETTLED_WITH_FEE),
        invoices_partially_paid=count_invoices(InvoiceStatus.PARTIALLY_PAID),
        invoices_open=count_invoices(InvoiceStatus.OPEN),
        money_in=sum((p.amount for p in payments), ZERO),
        money_allocated=sum((a.amount for a in allocations), ZERO),
        money_residual=sum((r.amount for r in residuals), ZERO),
        money_fees=sum((f.amount for f in fees), ZERO),
        search_truncations=truncations,
        model_parser_calls=model_calls,
    )
