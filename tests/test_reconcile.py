"""Reconciler tests. Ground truth / expected totals are read here, never in reconcile.py."""
from __future__ import annotations

import csv
import json
from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path

import reconcile as rc

DATA = Path(__file__).resolve().parent.parent / "data"
D = Decimal


def _run(platform=None, bank=None):
    platform = platform if platform is not None else rc.load_platform(DATA / "platform.csv")
    bank = bank if bank is not None else rc.load_bank(DATA / "bank.csv")
    return rc.reconcile(platform, bank)


def _real():
    return rc.load_platform(DATA / "platform.csv"), rc.load_bank(DATA / "bank.csv")


def _ordinary_ids(platform, bank, res):
    flagged = {g.txn_id for g in res.gaps}
    bank_ids = {b.reference for b in bank}
    return [p for p in platform if p.txn_type == "SALE" and p.txn_id not in flagged
            and p.txn_id in bank_ids and in_oct(p)]


def in_oct(p):
    return rc.in_october(p.txn_date)


def test_detected_gaps_equal_ground_truth() -> None:
    res = _run()
    found = {(g.gap_type, g.txn_id, g.effect) for g in res.gaps}
    with open(DATA / "ground_truth.csv", newline="") as fh:
        truth = {(r["gap_type"], r["txn_id"], D(r["bridge_effect"])) for r in csv.DictReader(fh)}
    assert found == truth
    assert len(res.gaps) == len(truth)  # no extras / no double counting


def test_rounding_total_is_exactly_0_300() -> None:
    res = _run()
    rounding = [g for g in res.gaps if g.gap_type == rc.ROUNDING]
    assert len(rounding) == 60
    assert sum((g.effect for g in rounding), D("0")) == D("0.300")


def test_bridge_matches_expected_totals_and_remainder_zero() -> None:
    br = _run().bridge
    exp = json.loads((DATA / "expected_totals.json").read_text())
    assert br.platform_total == D(exp["platform_total_oct"])
    assert br.bank_total == D(exp["bank_total_oct"])
    assert br.difference == D(exp["difference"])
    assert br.remainder == 0
    assert br.integrity_ok
    assert br.timing + br.rounding + br.duplicate + br.orphan_refund + br.remainder == br.difference


def test_exactly_one_duplicate_and_decoys_not_flagged() -> None:
    platform, _ = _real()
    res = _run()
    dups = [g for g in res.gaps if g.gap_type == rc.DUPLICATE]
    assert len(dups) == 1
    decoys = [p for p in platform if p.txn_type == "SALE" and p.amount == D("1250.000")]
    assert len(decoys) >= 2
    assert all(p.txn_id != dups[0].txn_id for p in decoys)
    assert not any(g.txn_id in {p.txn_id for p in decoys} and g.gap_type == rc.DUPLICATE for g in res.gaps)


def test_month_boundary_rows() -> None:
    platform, _ = _real()
    res = _run()
    first = next(p for p in platform if p.txn_timestamp.startswith("2026-10-01T00:07"))
    last = next(p for p in platform if p.txn_timestamp.startswith("2026-10-31T23:58"))
    assert rc.in_october(first.txn_date)
    assert first.txn_id not in {g.txn_id for g in res.gaps if g.gap_type == rc.TIMING}
    assert rc.in_october(last.txn_date)  # counted in the platform total
    assert (rc.TIMING, last.txn_id) in {(g.gap_type, g.txn_id) for g in res.gaps}


def test_at_least_one_timing_refund() -> None:
    res = _run()
    assert any(g.gap_type == rc.TIMING and g.txn_id.startswith("RFD-") for g in res.gaps)


def _pair(diff: str):
    p = rc.PlatformRow("REC-1", "TXN-1", "SALE", D("100.000") + D(diff), "INR",
                       "2026-10-05T10:00:00+05:30", date(2026, 10, 5), "")
    b = rc.BankRow("STL-1", "TXN-1", D("100.00"), "INR", date(2026, 10, 6), "")
    return [p], [b]


def test_tolerance_edge() -> None:
    res = _run(*_pair("0.005"))
    assert [g.gap_type for g in res.gaps] == [rc.ROUNDING]
    res = _run(*_pair("0.006"))
    assert [g.gap_type for g in res.gaps] == [rc.AMOUNT_MISMATCH]
    assert res.bridge.remainder == D("0.006")
    assert res.bridge.integrity_ok


def test_unplanted_anomalies() -> None:
    platform, bank = _real()
    res0 = _run(platform, bank)
    ordinary = _ordinary_ids(platform, bank, res0)
    # keep only ones that settle in October on the bank side
    bank_by = {b.reference: b for b in bank}
    ordinary = [p for p in ordinary if rc.in_october(bank_by[p.txn_id].settlement_date)]
    a, c = ordinary[0], ordinary[1]
    platform2 = [replace(p, amount=p.amount + D("10.000")) if p is a else p for p in platform]
    bank2 = [b for b in bank if b.reference != c.txn_id]
    res = _run(platform2, bank2)
    types = {g.txn_id: g.gap_type for g in res.gaps}
    assert types[a.txn_id] == rc.AMOUNT_MISMATCH
    assert types[c.txn_id] == rc.MISSING_IN_BANK
    assert res.bridge.remainder == D("10.000") + c.amount
    assert res.bridge.integrity_ok


def test_clean_data_has_no_gaps() -> None:
    platform, bank = [], []
    for i in range(1, 6):
        platform.append(rc.PlatformRow(f"REC-{i}", f"TXN-{i}", "SALE", D(f"{100+i}.000"), "INR",
                                       f"2026-10-0{i}T10:00:00+05:30", date(2026, 10, i), ""))
        bank.append(rc.BankRow(f"STL-{i}", f"TXN-{i}", D(f"{100+i}.00"), "INR", date(2026, 10, i + 1), ""))
    res = _run(platform, bank)
    assert res.gaps == ()
    assert res.bridge.remainder == 0
    assert res.bridge.integrity_ok


def test_source_independence() -> None:
    src = (Path(__file__).resolve().parent.parent / "reconcile.py").read_text()
    assert "ground_truth" not in src
    assert "expected_totals" not in src
