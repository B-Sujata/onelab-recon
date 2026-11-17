"""Generate the October 2026 platform and bank files for reconciliation.

Amounts are integer thousandths of a rupee until they are written as Decimal
strings. The random stream uses Random(2026) and only randint / sample.
"""

from __future__ import annotations

import csv
import json
import random
import re
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

SEED = 2026
IST = timezone(timedelta(hours=5, minutes=30))
PERIOD_START = date(2026, 10, 1)
PERIOD_END = date(2026, 10, 31)
BANK_WINDOW_END = date(2026, 11, 3)
ORPHAN_REFERENCE = "RFD-0021"
ORPHAN_ORIGINAL = "TXN-900001"
ORPHAN_AMOUNT_THOUSANDTHS = -1_499_000
ORPHAN_SETTLEMENT = date(2026, 10, 14)
DECOY_THOUSANDTHS = 1_250_000
FORCED_REFUND_ID = "RFD-0020"
FORCED_REFUND_AT = datetime(2026, 10, 31, 22, 15, 0, tzinfo=IST)

PLATFORM_COLUMNS = [
    "record_id",
    "txn_id",
    "txn_type",
    "amount",
    "currency",
    "txn_timestamp",
    "original_txn_id",
]
BANK_COLUMNS = [
    "settlement_id",
    "reference",
    "amount",
    "currency",
    "settlement_date",
    "original_reference",
]
TRUTH_COLUMNS = ["gap_type", "txn_id", "amount", "bridge_effect", "note"]

PLATFORM_AMOUNT = re.compile(r"^-?\d+\.\d{3}$")
BANK_AMOUNT = re.compile(r"^-?\d+\.\d{2}$")

DATA_DIR = Path(__file__).resolve().parent / "data"


@dataclass
class Sale:
    timestamp: datetime
    base_thousandths: int
    forced: bool
    txn_id: str = ""
    lag: int = 0
    is_rounding: bool = False
    is_duplicate: bool = False

    @property
    def txn_date(self) -> date:
        return self.timestamp.date()


@dataclass
class Refund:
    refund_id: str
    original: Sale
    timestamp: datetime
    lag: int

    @property
    def txn_date(self) -> date:
        return self.timestamp.date()

    @property
    def amount_thousandths(self) -> int:
        return -self.original.base_thousandths


@dataclass
class PlatformRec:
    txn_id: str
    txn_type: str
    amount_thousandths: int
    timestamp: datetime
    original_txn_id: str
    is_dup_copy: bool
    record_id: str = ""


@dataclass
class BankRec:
    reference: str
    amount_thousandths: int
    settlement_date: date
    original_reference: str
    settlement_id: str = ""


@dataclass
class TruthRec:
    gap_type: str
    txn_id: str
    amount_thousandths: int
    amount_places: int
    effect_thousandths: int
    note: str


@dataclass
class BuiltData:
    platform_rows: list[dict[str, str]]
    bank_rows: list[dict[str, str]]
    ground_truth_rows: list[dict[str, str]]
    expected_totals: dict[str, str]


def format_thousandths(amount: int, places: int) -> str:
    if places not in (2, 3):
        raise ValueError(f"places must be 2 or 3, got {places}")
    if places == 2 and amount % 10 != 0:
        raise ValueError(f"amount {amount} is not an exact 2-decimal value")
    sign = "-" if amount < 0 else ""
    digits = abs(amount)
    if places == 3:
        whole, frac = divmod(digits, 1000)
        return f"{sign}{whole}.{frac:03d}"
    whole, frac = divmod(digits // 10, 100)
    return f"{sign}{whole}.{frac:02d}"


def parse_decimal(text: str) -> Decimal:
    return Decimal(text)


def _clock(rng: random.Random) -> tuple[int, int, int]:
    return rng.randint(0, 23), rng.randint(0, 59), rng.randint(0, 59)


def _make_dt(day: int, hour: int, minute: int, second: int) -> datetime:
    return datetime(2026, 10, day, hour, minute, second, tzinfo=IST)


def _random_amount(rng: random.Random) -> int:
    """Whole paise from 50.00 to 5000.00, stored as thousandths."""
    paise = rng.randint(5000, 500000)
    return paise * 10


def _format_timestamp(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%S") + "+05:30"


def _sale_platform_thousandths(sale: Sale) -> int:
    if sale.is_rounding:
        return sale.base_thousandths + 5
    return sale.base_thousandths


def _settlement(txn_day: date, lag: int) -> date:
    return txn_day + timedelta(days=lag)


def _sorted_pool(sales: list[Sale], first_day: int, last_day: int, blocked: set[str]) -> list[Sale]:
    pool = [
        sale
        for sale in sales
        if first_day <= sale.txn_date.day <= last_day and sale.txn_id not in blocked and not sale.forced
    ]
    return sorted(pool, key=lambda sale: sale.txn_id)


def _build_sales(rng: random.Random) -> list[Sale]:
    used: set[datetime] = set()
    early = Sale(_make_dt(1, 0, 7, 0), _random_amount(rng), forced=True)
    late = Sale(_make_dt(31, 23, 58, 0), _random_amount(rng), forced=True)
    used.add(early.timestamp)
    used.add(late.timestamp)

    decoy_day = rng.randint(1, 31)
    decoys: list[Sale] = []
    while len(decoys) < 2:
        hour, minute, second = _clock(rng)
        moment = _make_dt(decoy_day, hour, minute, second)
        if moment in used:
            continue
        used.add(moment)
        decoys.append(Sale(moment, DECOY_THOUSANDTHS, forced=True))

    random_sales: list[Sale] = []
    while len(random_sales) < 496:
        day = rng.randint(1, 31)
        hour, minute, second = _clock(rng)
        moment = _make_dt(day, hour, minute, second)
        if moment in used:
            continue
        used.add(moment)
        random_sales.append(Sale(moment, _random_amount(rng), forced=False))

    sales = [early, late, *decoys, *random_sales]
    sales.sort(key=lambda sale: sale.timestamp)
    for index, sale in enumerate(sales, start=1):
        sale.txn_id = f"TXN-{index:06d}"
    for sale in sales:
        sale.lag = rng.randint(1, 2)
    return sales


def _build_refunds(rng: random.Random, picked: list[Sale]) -> list[Refund]:
    refunds: list[Refund] = []
    for index, original in enumerate(sorted(picked, key=lambda sale: sale.txn_id), start=1):
        refund_id = f"RFD-{index:04d}"
        if refund_id == FORCED_REFUND_ID:
            moment = FORCED_REFUND_AT
        else:
            offset = rng.randint(1, 4)
            refund_day = original.txn_date + timedelta(days=offset)
            while True:
                hour, minute, second = _clock(rng)
                moment = datetime(
                    refund_day.year,
                    refund_day.month,
                    refund_day.day,
                    hour,
                    minute,
                    second,
                    tzinfo=IST,
                )
                if moment.date() != original.txn_date or moment > original.timestamp:
                    break
        lag = rng.randint(1, 2)
        refunds.append(Refund(refund_id, original, moment, lag))
    return refunds


def _assemble(
    sales: list[Sale], refunds: list[Refund], duplicate: Sale
) -> tuple[list[PlatformRec], list[BankRec], list[TruthRec]]:
    platform: list[PlatformRec] = []
    for sale in sales:
        platform.append(
            PlatformRec(
                txn_id=sale.txn_id,
                txn_type="SALE",
                amount_thousandths=_sale_platform_thousandths(sale),
                timestamp=sale.timestamp,
                original_txn_id="",
                is_dup_copy=False,
            )
        )
    for refund in refunds:
        platform.append(
            PlatformRec(
                txn_id=refund.refund_id,
                txn_type="REFUND",
                amount_thousandths=refund.amount_thousandths,
                timestamp=refund.timestamp,
                original_txn_id=refund.original.txn_id,
                is_dup_copy=False,
            )
        )
    platform.append(
        PlatformRec(
            txn_id=duplicate.txn_id,
            txn_type="SALE",
            amount_thousandths=_sale_platform_thousandths(duplicate),
            timestamp=duplicate.timestamp,
            original_txn_id="",
            is_dup_copy=True,
        )
    )
    platform.sort(key=lambda row: (row.timestamp, row.is_dup_copy, row.txn_id))
    for index, row in enumerate(platform, start=1):
        row.record_id = f"REC-{index:06d}"

    bank: list[BankRec] = []
    for sale in sales:
        bank.append(
            BankRec(
                reference=sale.txn_id,
                amount_thousandths=sale.base_thousandths,
                settlement_date=_settlement(sale.txn_date, sale.lag),
                original_reference="",
            )
        )
    for refund in refunds:
        bank.append(
            BankRec(
                reference=refund.refund_id,
                amount_thousandths=refund.amount_thousandths,
                settlement_date=_settlement(refund.txn_date, refund.lag),
                original_reference=refund.original.txn_id,
            )
        )
    bank.append(
        BankRec(
            reference=ORPHAN_REFERENCE,
            amount_thousandths=ORPHAN_AMOUNT_THOUSANDTHS,
            settlement_date=ORPHAN_SETTLEMENT,
            original_reference=ORPHAN_ORIGINAL,
        )
    )
    bank.sort(key=lambda row: (row.settlement_date.isoformat(), row.reference))
    for index, row in enumerate(bank, start=1):
        row.settlement_id = f"STL-{index:06d}"

    by_txn = {sale.txn_id: sale for sale in sales}
    truth: list[TruthRec] = []
    for sale in sales:
        settled = _settlement(sale.txn_date, sale.lag)
        if settled.month == 11:
            amount = _sale_platform_thousandths(sale)
            truth.append(
                TruthRec("TIMING", sale.txn_id, amount, 3, amount, f"settles {settled.isoformat()}")
            )
    for refund in refunds:
        settled = _settlement(refund.txn_date, refund.lag)
        if settled.month == 11:
            amount = refund.amount_thousandths
            truth.append(
                TruthRec(
                    "TIMING",
                    refund.refund_id,
                    amount,
                    3,
                    amount,
                    f"settles {settled.isoformat()}",
                )
            )
    for sale in sales:
        if not sale.is_rounding:
            continue
        amount = _sale_platform_thousandths(sale)
        truth.append(
            TruthRec(
                "ROUNDING",
                sale.txn_id,
                amount,
                3,
                5,
                f"bank settles {format_thousandths(sale.base_thousandths, 2)}",
            )
        )
    copies = [row for row in platform if row.txn_id == duplicate.txn_id]
    original_row = next(row for row in copies if not row.is_dup_copy)
    extra_row = next(row for row in copies if row.is_dup_copy)
    dup_amount = _sale_platform_thousandths(duplicate)
    truth.append(
        TruthRec(
            "DUPLICATE",
            duplicate.txn_id,
            dup_amount,
            3,
            dup_amount,
            f"extra row {extra_row.record_id} duplicates {original_row.record_id}",
        )
    )
    truth.append(
        TruthRec(
            "ORPHAN_REFUND",
            ORPHAN_REFERENCE,
            ORPHAN_AMOUNT_THOUSANDTHS,
            2,
            -ORPHAN_AMOUNT_THOUSANDTHS,
            f"bank-only; original {ORPHAN_ORIGINAL} not in platform",
        )
    )
    truth.sort(key=lambda row: (row.gap_type, row.txn_id))
    # by_txn is retained so a dropped sale cannot silently disappear from timing rows
    if len(by_txn) != len(sales):
        raise AssertionError("sale txn_id collision")
    return platform, bank, truth


def _rows_from_platform(platform: list[PlatformRec]) -> list[dict[str, str]]:
    return [
        {
            "record_id": row.record_id,
            "txn_id": row.txn_id,
            "txn_type": row.txn_type,
            "amount": format_thousandths(row.amount_thousandths, 3),
            "currency": "INR",
            "txn_timestamp": _format_timestamp(row.timestamp),
            "original_txn_id": row.original_txn_id,
        }
        for row in platform
    ]


def _rows_from_bank(bank: list[BankRec]) -> list[dict[str, str]]:
    return [
        {
            "settlement_id": row.settlement_id,
            "reference": row.reference,
            "amount": format_thousandths(row.amount_thousandths, 2),
            "currency": "INR",
            "settlement_date": row.settlement_date.isoformat(),
            "original_reference": row.original_reference,
        }
        for row in bank
    ]


def _rows_from_truth(truth: list[TruthRec]) -> list[dict[str, str]]:
    return [
        {
            "gap_type": row.gap_type,
            "txn_id": row.txn_id,
            "amount": format_thousandths(row.amount_thousandths, row.amount_places),
            "bridge_effect": format_thousandths(row.effect_thousandths, 3),
            "note": row.note,
        }
        for row in truth
    ]


def _expected_totals(platform_rows: list[dict[str, str]], bank_rows: list[dict[str, str]]) -> dict[str, str]:
    platform_total = sum((parse_decimal(row["amount"]) for row in platform_rows), Decimal("0"))
    bank_total = sum(
        (
            parse_decimal(row["amount"])
            for row in bank_rows
            if row["settlement_date"].startswith("2026-10")
        ),
        Decimal("0"),
    )
    difference = platform_total - bank_total
    return {
        "period": "2026-10",
        "currency": "INR",
        "platform_total_oct": f"{platform_total.quantize(Decimal('0.001')):.3f}",
        "bank_total_oct": f"{bank_total.quantize(Decimal('0.01')):.2f}",
        "difference": f"{difference.quantize(Decimal('0.001')):.3f}",
        "bank_window_end": BANK_WINDOW_END.isoformat(),
    }


def _assert_construction(
    sales: list[Sale],
    refunds: list[Refund],
    duplicate: Sale,
    platform: list[PlatformRec],
    bank: list[BankRec],
    truth: list[TruthRec],
    platform_rows: list[dict[str, str]],
    bank_rows: list[dict[str, str]],
    truth_rows: list[dict[str, str]],
    totals: dict[str, str],
) -> None:
    forced = [sale for sale in sales if sale.forced]
    rounding = [sale for sale in sales if sale.is_rounding]
    duplicates = [sale for sale in sales if sale.is_duplicate]
    assert len(sales) == 500
    assert len(refunds) == 20
    assert len(rounding) == 60
    assert len(duplicates) == 1
    assert len(forced) == 4
    assert sum(1 for row in bank if row.reference == ORPHAN_REFERENCE) == 1
    assert {sale.txn_id for sale in sales} == {f"TXN-{index:06d}" for index in range(1, 501)}
    assert [refund.refund_id for refund in sorted(refunds, key=lambda item: item.refund_id)] == [
        f"RFD-{index:04d}" for index in range(1, 21)
    ]

    early = next(sale for sale in sales if sale.timestamp == _make_dt(1, 0, 7, 0))
    late = next(sale for sale in sales if sale.timestamp == _make_dt(31, 23, 58, 0))
    decoys = [sale for sale in forced if sale.base_thousandths == DECOY_THOUSANDTHS]
    assert len(decoys) == 2
    assert decoys[0].txn_date == decoys[1].txn_date
    assert early.forced and late.forced
    special_ids = {sale.txn_id for sale in rounding} | {duplicate.txn_id} | {
        refund.original.txn_id for refund in refunds
    }
    assert not special_ids & {sale.txn_id for sale in forced}

    for row in platform:
        assert PERIOD_START <= row.timestamp.date() <= PERIOD_END
        assert row.timestamp.tzinfo == IST
    assert all(PLATFORM_AMOUNT.fullmatch(row["amount"]) for row in platform_rows)
    assert all(row["txn_timestamp"].endswith("+05:30") for row in platform_rows)
    assert all(BANK_AMOUNT.fullmatch(row["amount"]) for row in bank_rows)

    for sale in rounding:
        assert PERIOD_START <= sale.txn_date <= date(2026, 10, 28)
        assert _sale_platform_thousandths(sale) % 10 == 5
        assert sale.base_thousandths % 10 == 0
        assert not sale.forced and not sale.is_duplicate
        assert sale.txn_id not in {refund.original.txn_id for refund in refunds}
    assert sum(5 for _ in rounding) == 300

    bank_by_ref = {row.reference: row for row in bank}
    assert len(bank_by_ref) == len(bank)
    for row in platform:
        bank_row = bank_by_ref[row.txn_id]
        if row.amount_thousandths % 10 == 5:
            assert row.amount_thousandths - bank_row.amount_thousandths == 5
        else:
            assert str(format_thousandths(row.amount_thousandths, 3)).endswith("0")
            assert row.amount_thousandths == bank_row.amount_thousandths

    platform_counts = Counter(row.txn_id for row in platform)
    repeated = [txn_id for txn_id, count in platform_counts.items() if count > 1]
    assert repeated == [duplicate.txn_id]
    assert platform_counts[duplicate.txn_id] == 2

    for refund in refunds:
        assert refund.original.txn_id.startswith("TXN-")
        assert refund.timestamp > refund.original.timestamp
        assert refund.amount_thousandths == -refund.original.base_thousandths
        assert refund.original in sales
        assert not refund.original.is_rounding
        if refund.refund_id == FORCED_REFUND_ID:
            assert refund.timestamp == FORCED_REFUND_AT
        else:
            delta = (refund.txn_date - refund.original.txn_date).days
            assert 1 <= delta <= 4
        assert refund.lag in (1, 2)
        assert _settlement(refund.txn_date, refund.lag) <= BANK_WINDOW_END

    for sale in sales:
        assert sale.lag in (1, 2)
        assert _settlement(sale.txn_date, sale.lag) <= BANK_WINDOW_END

    orphan = next(row for row in bank if row.reference == ORPHAN_REFERENCE)
    platform_ids = {row.txn_id for row in platform}
    refund_originals = {refund.original.txn_id for refund in refunds}
    assert orphan.original_reference == ORPHAN_ORIGINAL
    assert orphan.original_reference not in platform_ids
    assert orphan.reference not in platform_ids
    assert orphan.original_reference not in refund_originals
    assert orphan.settlement_date == ORPHAN_SETTLEMENT
    assert PERIOD_START <= orphan.settlement_date <= PERIOD_END
    assert orphan.amount_thousandths == ORPHAN_AMOUNT_THOUSANDTHS

    timing_sales = [
        sale for sale in sales if _settlement(sale.txn_date, sale.lag).month == 11
    ]
    timing_refunds = [
        refund for refund in refunds if _settlement(refund.txn_date, refund.lag).month == 11
    ]
    assert timing_sales
    assert timing_refunds
    assert late in timing_sales
    assert early not in timing_sales
    assert any(refund.refund_id == FORCED_REFUND_ID for refund in timing_refunds)

    for row in bank:
        assert date(2026, 10, 2) <= row.settlement_date <= BANK_WINDOW_END

    truth_ids = [row.txn_id for row in truth]
    assert len(truth_ids) == len(set(truth_ids))
    assert Counter(row.gap_type for row in truth)["ROUNDING"] == 60
    assert Counter(row.gap_type for row in truth)["DUPLICATE"] == 1
    assert Counter(row.gap_type for row in truth)["ORPHAN_REFUND"] == 1
    rounding_effect = sum(row.effect_thousandths for row in truth if row.gap_type == "ROUNDING")
    assert rounding_effect == 300

    platform_total = sum(row.amount_thousandths for row in platform)
    bank_total = sum(
        row.amount_thousandths for row in bank if PERIOD_START <= row.settlement_date <= PERIOD_END
    )
    difference = platform_total - bank_total
    assert sum(row.effect_thousandths for row in truth) == difference

    file_difference = parse_decimal(totals["difference"])
    file_effects = sum((parse_decimal(row["bridge_effect"]) for row in truth_rows), Decimal("0"))
    assert file_effects == file_difference
    assert parse_decimal(totals["platform_total_oct"]) == sum(
        (parse_decimal(row["amount"]) for row in platform_rows), Decimal("0")
    )
    assert parse_decimal(totals["bank_total_oct"]) == sum(
        (
            parse_decimal(row["amount"])
            for row in bank_rows
            if row["settlement_date"].startswith("2026-10")
        ),
        Decimal("0"),
    )
    assert file_difference == parse_decimal(totals["platform_total_oct"]) - parse_decimal(
        totals["bank_total_oct"]
    )
    assert totals["period"] == "2026-10"
    assert totals["currency"] == "INR"
    assert totals["bank_window_end"] == "2026-11-03"
    assert sum(
        (parse_decimal(row["bridge_effect"]) for row in truth_rows if row["gap_type"] == "ROUNDING"),
        Decimal("0"),
    ) == Decimal("0.300")


def build(seed: int = SEED) -> BuiltData:
    rng = random.Random(seed)
    sales = _build_sales(rng)
    blocked = {sale.txn_id for sale in sales if sale.forced}
    refund_pool = _sorted_pool(sales, 1, 26, blocked)
    picked_refunds = rng.sample(refund_pool, 20)
    blocked.update(sale.txn_id for sale in picked_refunds)
    duplicate = rng.sample(_sorted_pool(sales, 1, 28, blocked), 1)[0]
    duplicate.is_duplicate = True
    blocked.add(duplicate.txn_id)
    for sale in rng.sample(_sorted_pool(sales, 1, 28, blocked), 60):
        sale.is_rounding = True

    refunds = _build_refunds(rng, picked_refunds)
    platform, bank, truth = _assemble(sales, refunds, duplicate)
    platform_rows = _rows_from_platform(platform)
    bank_rows = _rows_from_bank(bank)
    truth_rows = _rows_from_truth(truth)
    totals = _expected_totals(platform_rows, bank_rows)
    _assert_construction(
        sales,
        refunds,
        duplicate,
        platform,
        bank,
        truth,
        platform_rows,
        bank_rows,
        truth_rows,
        totals,
    )
    return BuiltData(platform_rows, bank_rows, truth_rows, totals)


def write_data(built: BuiltData, directory: Path = DATA_DIR) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    _write_csv(directory / "platform.csv", PLATFORM_COLUMNS, built.platform_rows)
    _write_csv(directory / "bank.csv", BANK_COLUMNS, built.bank_rows)
    _write_csv(directory / "ground_truth.csv", TRUTH_COLUMNS, built.ground_truth_rows)
    payload = json.dumps(built.expected_totals, indent=2) + "\n"
    (directory / "expected_totals.json").write_text(payload, encoding="utf-8")


def _write_csv(path: Path, columns: list[str], rows: list[dict[str, str]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    write_data(build())


if __name__ == "__main__":
    main()
