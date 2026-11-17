"""Match platform transactions to bank settlements and explain the October gap.

Totals use every October row. Duplicate rows are removed only from matching.
Amounts stay Decimal. This module does not open the answer files.
"""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path

PERIOD_START = date(2026, 10, 1)
PERIOD_END = date(2026, 10, 31)
BANK_WINDOW_END = date(2026, 11, 3)
ROUNDING_TOLERANCE = Decimal("0.005")

TIMING = "TIMING"
ROUNDING = "ROUNDING"
DUPLICATE = "DUPLICATE"
ORPHAN_REFUND = "ORPHAN_REFUND"
AMOUNT_MISMATCH = "AMOUNT_MISMATCH"
MISSING_IN_BANK = "MISSING_IN_BANK"
BANK_ONLY = "BANK_ONLY"

EXPLAINED_TYPES = frozenset({TIMING, ROUNDING, DUPLICATE, ORPHAN_REFUND})

_PLATFORM_AMOUNT = re.compile(r"^-?\d+\.\d{3}$")
_BANK_AMOUNT = re.compile(r"^-?\d+\.\d{2}$")


@dataclass(frozen=True)
class PlatformRow:
    record_id: str
    txn_id: str
    txn_type: str
    amount: Decimal
    currency: str
    txn_timestamp: str
    txn_date: date
    original_txn_id: str


@dataclass(frozen=True)
class BankRow:
    settlement_id: str
    reference: str
    amount: Decimal
    currency: str
    settlement_date: date
    original_reference: str


@dataclass(frozen=True)
class Gap:
    gap_type: str
    txn_id: str
    amount: Decimal
    effect: Decimal
    reason: str


@dataclass(frozen=True)
class Bridge:
    platform_total: Decimal
    bank_total: Decimal
    difference: Decimal
    timing: Decimal
    rounding: Decimal
    duplicate: Decimal
    orphan_refund: Decimal
    explained: Decimal
    remainder: Decimal
    unexplained: Decimal
    integrity_ok: bool
    integrity_message: str


@dataclass(frozen=True)
class Reconciliation:
    gaps: tuple[Gap, ...]
    bridge: Bridge


def in_october(value: date) -> bool:
    return PERIOD_START <= value <= PERIOD_END


def in_november(value: date) -> bool:
    return value.year == 2026 and value.month == 11


def load_platform(path: Path) -> list[PlatformRow]:
    rows: list[PlatformRow] = []
    for raw in _read_rows(path):
        timestamp = raw["txn_timestamp"].strip()
        amount_text = raw["amount"].strip()
        if not _PLATFORM_AMOUNT.fullmatch(amount_text):
            raise ValueError(f"platform amount must have 3 decimals: {amount_text}")
        rows.append(
            PlatformRow(
                record_id=raw["record_id"].strip(),
                txn_id=raw["txn_id"].strip(),
                txn_type=raw["txn_type"].strip(),
                amount=Decimal(amount_text),
                currency=raw["currency"].strip(),
                txn_timestamp=timestamp,
                txn_date=_platform_date(timestamp),
                original_txn_id=raw.get("original_txn_id", "").strip(),
            )
        )
    return rows


def load_bank(path: Path) -> list[BankRow]:
    rows: list[BankRow] = []
    for raw in _read_rows(path):
        amount_text = raw["amount"].strip()
        if not _BANK_AMOUNT.fullmatch(amount_text):
            raise ValueError(f"bank amount must have 2 decimals: {amount_text}")
        rows.append(
            BankRow(
                settlement_id=raw["settlement_id"].strip(),
                reference=raw["reference"].strip(),
                amount=Decimal(amount_text),
                currency=raw["currency"].strip(),
                settlement_date=date.fromisoformat(raw["settlement_date"].strip()),
                original_reference=raw.get("original_reference", "").strip(),
            )
        )
    return rows


def reconcile(platform: list[PlatformRow], bank: list[BankRow]) -> Reconciliation:
    sale_ids = {row.txn_id for row in platform if row.txn_type == "SALE"}
    platform_kept, platform_gaps = _duplicate_gaps(
        platform,
        key=lambda row: row.txn_id,
        amount_of=lambda row: row.amount,
        when=lambda row: row.txn_date,
        side="platform",
    )
    bank_kept, bank_gaps = _duplicate_gaps(
        bank,
        key=lambda row: row.reference,
        amount_of=lambda row: row.amount,
        when=lambda row: row.settlement_date,
        side="bank",
    )
    bank_by_id = {row.reference: row for row in bank_kept}
    used_bank: set[str] = set()
    gaps: list[Gap] = [*platform_gaps, *bank_gaps]
    handled_platform = {id(row) for row in platform if row not in platform_kept}
    handled_bank = {id(row) for row in bank if row not in bank_kept}

    for row in platform_kept:
        if not in_october(row.txn_date):
            continue
        bank_row = bank_by_id.get(row.txn_id)
        if row.txn_type == "REFUND" and row.original_txn_id not in sale_ids:
            gaps.append(_orphan_gap(row, bank_row, "platform"))
            handled_platform.add(id(row))
            if bank_row is not None:
                used_bank.add(bank_row.reference)
                handled_bank.add(id(bank_row))
            continue
        if bank_row is None:
            gaps.append(
                Gap(
                    gap_type=MISSING_IN_BANK,
                    txn_id=row.txn_id,
                    amount=row.amount,
                    effect=row.amount,
                    reason=(
                        f"Platform transaction {row.txn_id} is dated {row.txn_date.isoformat()} "
                        "and has no bank settlement with the same id, so its amount is only in "
                        "the October platform total."
                    ),
                )
            )
            handled_platform.add(id(row))
            continue
        used_bank.add(bank_row.reference)
        handled_bank.add(id(bank_row))
        if not in_october(bank_row.settlement_date):
            gaps.append(
                Gap(
                    gap_type=TIMING,
                    txn_id=row.txn_id,
                    amount=row.amount,
                    effect=row.amount,
                    reason=_timing_reason(row, bank_row),
                )
            )
            handled_platform.add(id(row))
            continue
        difference = row.amount - bank_row.amount
        if difference == 0:
            continue
        if abs(difference) <= ROUNDING_TOLERANCE:
            gaps.append(
                Gap(
                    gap_type=ROUNDING,
                    txn_id=row.txn_id,
                    amount=row.amount,
                    effect=difference,
                    reason=(
                        f"Platform amount {row.amount} and bank amount {bank_row.amount} differ "
                        f"by {difference}. The difference is within 0.005: the platform keeps a "
                        "third decimal and the bank stores 2 decimals."
                    ),
                )
            )
        else:
            gaps.append(
                Gap(
                    gap_type=AMOUNT_MISMATCH,
                    txn_id=row.txn_id,
                    amount=row.amount,
                    effect=difference,
                    reason=(
                        f"Platform amount {row.amount} and bank amount {bank_row.amount} differ "
                        f"by {difference}. That is more than 0.005, so it is not the half-paise "
                        "the bank drops."
                    ),
                )
            )
        handled_platform.add(id(row))

    for row in bank_kept:
        if row.reference in used_bank or not in_october(row.settlement_date):
            continue
        if _is_bank_refund(row) and row.original_reference not in sale_ids:
            gaps.append(_orphan_gap(None, row, "bank"))
        else:
            gaps.append(
                Gap(
                    gap_type=BANK_ONLY,
                    txn_id=row.reference,
                    amount=row.amount,
                    effect=-row.amount,
                    reason=(
                        f"Bank reference {row.reference} on {row.settlement_date.isoformat()} has "
                        "no matching platform transaction, so its amount is only in the October "
                        "bank total."
                    ),
                )
            )
        handled_bank.add(id(row))

    gaps.extend(_remaining_orphan_refunds(platform_kept, bank_kept, sale_ids, handled_platform, handled_bank))
    gaps.sort(key=lambda gap: (gap.gap_type, gap.txn_id, gap.amount, gap.effect))
    return Reconciliation(gaps=tuple(gaps), bridge=_bridge(platform, bank, gaps))


def _duplicate_gaps(rows, key, amount_of, when, side: str) -> tuple[list, list[Gap]]:
    seen: set[str] = set()
    kept: list = []
    gaps: list[Gap] = []
    for row in rows:
        txn_id = key(row)
        if txn_id not in seen:
            seen.add(txn_id)
            kept.append(row)
            continue
        amount = amount_of(row)
        if side == "platform" and in_october(when(row)):
            effect = amount
            reason = (
                f"Platform id {txn_id} appears more than once. This extra row is in the October "
                f"platform total for {amount} and is not an extra bank settlement."
            )
        elif side == "bank" and in_october(when(row)):
            effect = -amount
            reason = (
                f"Bank reference {txn_id} appears more than once. This extra row is in the October "
                f"bank total for {amount} and is not an extra platform row."
            )
        else:
            effect = Decimal("0")
            reason = f"{txn_id} is a repeated id outside the October totals."
        gaps.append(
            Gap(gap_type=DUPLICATE, txn_id=txn_id, amount=amount, effect=effect, reason=reason)
        )
    return kept, gaps


def _orphan_gap(platform_row: PlatformRow | None, bank_row: BankRow | None, side: str) -> Gap:
    effect = Decimal("0")
    if platform_row is not None and in_october(platform_row.txn_date):
        effect += platform_row.amount
    if bank_row is not None and in_october(bank_row.settlement_date):
        effect -= bank_row.amount
    if platform_row is not None:
        txn_id = platform_row.txn_id
        amount = platform_row.amount
        original = platform_row.original_txn_id
    else:
        assert bank_row is not None
        txn_id = bank_row.reference
        amount = bank_row.amount
        original = bank_row.original_reference
    if side == "bank":
        reason = (
            f"Bank refund {txn_id} points at original reference {original or '(blank)'}, which is "
            "not a platform sale. The refund is in the October bank total and not on the platform."
        )
    else:
        reason = (
            f"Refund {txn_id} points at original transaction {original or '(blank)'}, which is "
            "not a platform sale."
        )
    return Gap(gap_type=ORPHAN_REFUND, txn_id=txn_id, amount=amount, effect=effect, reason=reason)


def _remaining_orphan_refunds(platform_kept, bank_kept, sale_ids, handled_platform, handled_bank) -> list[Gap]:
    """Flag a refund with a broken original that the October pass did not already take."""
    found: list[Gap] = []
    for row in platform_kept:
        if id(row) in handled_platform or row.txn_type != "REFUND":
            continue
        if row.original_txn_id in sale_ids:
            continue
        found.append(_orphan_gap(row, None, "platform"))
        handled_platform.add(id(row))
    for row in bank_kept:
        if id(row) in handled_bank or not _is_bank_refund(row):
            continue
        if row.original_reference in sale_ids:
            continue
        found.append(_orphan_gap(None, row, "bank"))
        handled_bank.add(id(row))
    return found


def _timing_reason(platform_row: PlatformRow, bank_row: BankRow) -> str:
    where = "November" if in_november(bank_row.settlement_date) else "outside October"
    return (
        f"The platform recorded {platform_row.txn_id} on {platform_row.txn_date.isoformat()} "
        f"and the bank settled it on {bank_row.settlement_date.isoformat()} ({where}). "
        "It is in the October platform total and not in the October bank total."
    )


def _bridge(platform: list[PlatformRow], bank: list[BankRow], gaps: list[Gap]) -> Bridge:
    platform_total = sum((row.amount for row in platform if in_october(row.txn_date)), Decimal("0"))
    bank_total = sum((row.amount for row in bank if in_october(row.settlement_date)), Decimal("0"))
    difference = platform_total - bank_total
    timing = _sum_effect(gaps, TIMING)
    rounding = _sum_effect(gaps, ROUNDING)
    duplicate = _sum_effect(gaps, DUPLICATE)
    orphan_refund = _sum_effect(gaps, ORPHAN_REFUND)
    explained = timing + rounding + duplicate + orphan_refund
    remainder = difference - explained
    unexplained = sum((gap.effect for gap in gaps if gap.gap_type not in EXPLAINED_TYPES), Decimal("0"))
    if remainder == unexplained:
        integrity_ok = True
        integrity_message = "Bridge integrity check passed."
    else:
        integrity_ok = False
        integrity_message = (
            f"bridge integrity error: remainder {remainder} does not equal "
            f"the sum of unexplained effects {unexplained}."
        )
    return Bridge(
        platform_total=platform_total,
        bank_total=bank_total,
        difference=difference,
        timing=timing,
        rounding=rounding,
        duplicate=duplicate,
        orphan_refund=orphan_refund,
        explained=explained,
        remainder=remainder,
        unexplained=unexplained,
        integrity_ok=integrity_ok,
        integrity_message=integrity_message,
    )


def _sum_effect(gaps: list[Gap], gap_type: str) -> Decimal:
    return sum((gap.effect for gap in gaps if gap.gap_type == gap_type), Decimal("0"))


def _is_bank_refund(row: BankRow) -> bool:
    return bool(row.original_reference) or row.reference.startswith("RFD-")


def _platform_date(timestamp: str) -> date:
    if not timestamp.endswith("+05:30") or len(timestamp) < 10:
        raise ValueError(f"platform timestamp must keep the +05:30 offset: {timestamp}")
    return date.fromisoformat(timestamp[:10])


def _read_rows(path: Path) -> list[dict[str, str]]:
    with Path(path).open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))
