"""Generator determinism, counts, and the construction self-checks."""

from __future__ import annotations

import csv
import json
import re
from collections import Counter
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from generate_data import BANK_WINDOW_END, build, write_data

PLATFORM_AMOUNT = re.compile(r"^-?\d+\.\d{3}$")
BANK_AMOUNT = re.compile(r"^-?\d+\.\d{2}$")
ROOT = Path(__file__).resolve().parents[1]


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def test_two_builds_are_identical() -> None:
    first = build()
    second = build()
    assert first.platform_rows == second.platform_rows
    assert first.bank_rows == second.bank_rows
    assert first.ground_truth_rows == second.ground_truth_rows
    assert first.expected_totals == second.expected_totals


def test_written_files_round_trip(tmp_path: Path) -> None:
    built = build()
    write_data(built, tmp_path)
    assert _read_csv(tmp_path / "platform.csv") == built.platform_rows
    assert _read_csv(tmp_path / "bank.csv") == built.bank_rows
    assert _read_csv(tmp_path / "ground_truth.csv") == built.ground_truth_rows
    saved = json.loads((tmp_path / "expected_totals.json").read_text(encoding="utf-8"))
    assert saved == built.expected_totals


def test_counts_and_self_checks() -> None:
    built = build()
    platform = built.platform_rows
    bank = built.bank_rows
    truth = built.ground_truth_rows
    totals = built.expected_totals

    sales = [row for row in platform if row["txn_type"] == "SALE"]
    refunds = [row for row in platform if row["txn_type"] == "REFUND"]
    sale_ids = {row["txn_id"] for row in sales}
    assert len(sale_ids) == 500
    assert len(sales) == 501
    assert len(refunds) == 20
    assert len(platform) == 521
    assert len(bank) == 521
    assert sale_ids == {f"TXN-{index:06d}" for index in range(1, 501)}
    assert {row["txn_id"] for row in refunds} == {f"RFD-{index:04d}" for index in range(1, 21)}

    gap_counts = Counter(row["gap_type"] for row in truth)
    assert gap_counts["ROUNDING"] == 60
    assert gap_counts["DUPLICATE"] == 1
    assert gap_counts["ORPHAN_REFUND"] == 1
    assert gap_counts["TIMING"] >= 2
    timing_ids = {row["txn_id"] for row in truth if row["gap_type"] == "TIMING"}
    assert any(txn_id.startswith("TXN-") for txn_id in timing_ids)
    assert any(txn_id.startswith("RFD-") for txn_id in timing_ids)

    for row in platform:
        assert row["txn_timestamp"].endswith("+05:30")
        txn_day = date.fromisoformat(row["txn_timestamp"][:10])
        assert txn_day.year == 2026 and txn_day.month == 10
        assert PLATFORM_AMOUNT.fullmatch(row["amount"])
        assert row["currency"] == "INR"
    for row in bank:
        assert BANK_AMOUNT.fullmatch(row["amount"])
        assert row["currency"] == "INR"
        settled = date.fromisoformat(row["settlement_date"])
        assert date(2026, 10, 2) <= settled <= BANK_WINDOW_END

    counts = Counter(row["txn_id"] for row in platform)
    repeated = [txn_id for txn_id, count in counts.items() if count > 1]
    assert len(repeated) == 1
    assert counts[repeated[0]] == 2
    assert all(count == 1 for count in Counter(row["reference"] for row in bank).values())

    bank_by_ref = {row["reference"]: row for row in bank}
    rounding_rows = [row for row in truth if row["gap_type"] == "ROUNDING"]
    rounding_ids = {row["txn_id"] for row in rounding_rows}
    assert sum((Decimal(row["bridge_effect"]) for row in rounding_rows), Decimal("0")) == Decimal("0.300")
    platform_by_id = {}
    for row in platform:
        platform_by_id.setdefault(row["txn_id"], []).append(row)
    for row in rounding_rows:
        assert Decimal(row["bridge_effect"]) == Decimal("0.005")
        platform_row = platform_by_id[row["txn_id"]][0]
        txn_day = date.fromisoformat(platform_row["txn_timestamp"][:10])
        assert date(2026, 10, 1) <= txn_day <= date(2026, 10, 28)
        assert platform_row["amount"].endswith("5")
        assert Decimal(platform_row["amount"]) - Decimal(bank_by_ref[row["txn_id"]]["amount"]) == Decimal(
            "0.005"
        )

    duplicate_id = next(row["txn_id"] for row in truth if row["gap_type"] == "DUPLICATE")
    refund_originals = {row["original_txn_id"] for row in refunds}
    forced_early = _only(platform, "2026-10-01T00:07:00+05:30")
    forced_late = _only(platform, "2026-10-31T23:58:00+05:30")
    decoy_ids = _decoy_ids(platform)
    excluded = {forced_early["txn_id"], forced_late["txn_id"], *decoy_ids}
    assert duplicate_id not in excluded
    assert not rounding_ids & excluded
    assert not refund_originals & excluded

    for row in platform:
        if row["txn_id"] in rounding_ids:
            continue
        assert row["amount"].endswith("0")
        assert Decimal(row["amount"]) == Decimal(bank_by_ref[row["txn_id"]]["amount"])

    sales_by_id = {row["txn_id"]: row for row in sales}
    for refund in refunds:
        original = sales_by_id[refund["original_txn_id"]]
        assert original["txn_type"] == "SALE"
        assert refund["txn_timestamp"] > original["txn_timestamp"]
        assert Decimal(refund["amount"]) == -Decimal(original["amount"])
        origin_day = date.fromisoformat(original["txn_timestamp"][:10])
        refund_day = date.fromisoformat(refund["txn_timestamp"][:10])
        assert date(2026, 10, 1) <= origin_day <= date(2026, 10, 26)
        if refund["txn_id"] == "RFD-0020":
            assert refund["txn_timestamp"] == "2026-10-31T22:15:00+05:30"
        else:
            assert 1 <= (refund_day - origin_day).days <= 4
        settled = date.fromisoformat(bank_by_ref[refund["txn_id"]]["settlement_date"])
        assert settled - refund_day in (timedelta(days=1), timedelta(days=2))
        assert bank_by_ref[refund["txn_id"]]["original_reference"] == refund["original_txn_id"]

    for sale in sales:
        settled = date.fromisoformat(bank_by_ref[sale["txn_id"]]["settlement_date"])
        txn_day = date.fromisoformat(sale["txn_timestamp"][:10])
        assert settled - txn_day in (timedelta(days=1), timedelta(days=2))

    orphan_rows = [row for row in bank if row["reference"] == "RFD-0021"]
    assert len(orphan_rows) == 1
    orphan = orphan_rows[0]
    assert orphan["amount"] == "-1499.00"
    assert orphan["settlement_date"] == "2026-10-14"
    assert orphan["original_reference"] == "TXN-900001"
    assert "TXN-900001" not in {row["txn_id"] for row in platform}
    assert "RFD-0021" not in {row["txn_id"] for row in platform}
    assert "TXN-900001" not in refund_originals

    platform_total = sum((Decimal(row["amount"]) for row in platform), Decimal("0"))
    bank_total = sum(
        (Decimal(row["amount"]) for row in bank if row["settlement_date"].startswith("2026-10")),
        Decimal("0"),
    )
    difference = platform_total - bank_total
    assert sum((Decimal(row["bridge_effect"]) for row in truth), Decimal("0")) == difference
    assert Decimal(totals["platform_total_oct"]) == platform_total
    assert Decimal(totals["bank_total_oct"]) == bank_total
    assert Decimal(totals["difference"]) == difference
    assert totals["period"] == "2026-10"
    assert totals["currency"] == "INR"
    assert totals["bank_window_end"] == "2026-11-03"

    late_day = date.fromisoformat(forced_late["txn_timestamp"][:10])
    late_settled = date.fromisoformat(bank_by_ref[forced_late["txn_id"]]["settlement_date"])
    early_settled = date.fromisoformat(bank_by_ref[forced_early["txn_id"]]["settlement_date"])
    assert late_day.month == 10 and late_settled.month == 11
    assert early_settled.month == 10
    assert forced_late["txn_id"] in timing_ids
    assert forced_early["txn_id"] not in timing_ids


def test_committed_data_matches_generator() -> None:
    built = build()
    data_dir = ROOT / "data"
    assert _read_csv(data_dir / "platform.csv") == built.platform_rows
    assert _read_csv(data_dir / "bank.csv") == built.bank_rows
    assert _read_csv(data_dir / "ground_truth.csv") == built.ground_truth_rows
    saved = json.loads((data_dir / "expected_totals.json").read_text(encoding="utf-8"))
    assert saved == built.expected_totals


def _only(platform: list[dict[str, str]], timestamp: str) -> dict[str, str]:
    matches = [row for row in platform if row["txn_timestamp"] == timestamp]
    assert len(matches) == 1
    return matches[0]


def _decoy_ids(platform: list[dict[str, str]]) -> set[str]:
    by_day: dict[str, list[str]] = {}
    for row in platform:
        if row["txn_type"] == "SALE" and Decimal(row["amount"]) == Decimal("1250.000"):
            by_day.setdefault(row["txn_timestamp"][:10], []).append(row["txn_id"])
    pairs = [ids for ids in by_day.values() if len(set(ids)) >= 2]
    assert pairs
    same_day = max(pairs, key=len)
    once = [txn_id for txn_id in set(same_day) if sum(txn_id == row["txn_id"] for row in platform) == 1]
    assert len(once) >= 2
    return set(once)
