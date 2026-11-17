"""Streamlit page: month-end payments reconciliation, October 2026."""
from decimal import Decimal
from pathlib import Path

import pandas as pd
import streamlit as st

import reconcile as rc

DATA = Path(__file__).resolve().parent / "data"

ASSUMPTIONS = """
- **Period:** October 2026, IST. Platform date = `txn_timestamp` date as written (+05:30); the bank gives settlement date only.
- **Comparison basis:** platform total = rows with an IST date in October; bank total = rows with a settlement date in October. The bank file runs to 3 Nov so November settlements are visible.
- **Match key:** `txn_id` = `reference`. Single currency INR, gross amounts, no fees, only successful transactions, no prior-month carryover.
- **Settlement lag:** 1 or 2 calendar days.
- **Refunds:** separate rows with negative amounts and `original_txn_id` (platform) / `original_reference` (bank).
- **Precision:** platform amounts have 3 decimals, bank amounts 2 decimals. All amounts are `Decimal`, never float.
"""

st.set_page_config(page_title="Month-end reconciliation", layout="wide")
st.title("Month-end payments reconciliation: October 2026")

platform = rc.load_platform(DATA / "platform.csv")
bank = rc.load_bank(DATA / "bank.csv")
res = rc.reconcile(platform, bank)
br = res.bridge


def money(x: Decimal) -> str:
    return f"{x:,}"


c1, c2, c3 = st.columns(3)
c1.metric("Platform total (Oct)", money(br.platform_total))
c2.metric("Bank total (Oct)", money(br.bank_total))
c3.metric("Difference", money(br.difference))

st.header("Bridge")
st.caption("Platform total - bank total = timing + rounding + duplicate + orphan refund + unexplained remainder")
bridge_rows = [
    ("Difference (platform - bank)", br.difference),
    ("Timing", br.timing),
    ("Rounding", br.rounding),
    ("Duplicate", br.duplicate),
    ("Orphan refund", br.orphan_refund),
    ("Explained total", br.explained),
    ("Unexplained remainder", br.remainder),
]
bdf = pd.DataFrame({"Item": [r[0] for r in bridge_rows], "Amount": [money(r[1]) for r in bridge_rows]})


def _red(row):
    if row["Item"] == "Unexplained remainder" and br.remainder != 0:
        return ["color: red; font-weight: bold"] * len(row)
    return [""] * len(row)


st.table(bdf.style.apply(_red, axis=1))
if br.integrity_ok:
    st.success(br.integrity_message)
else:
    st.error(f"Bridge integrity error: {br.integrity_message}")

gdf = pd.DataFrame(
    [{"type": g.gap_type, "txn_id": g.txn_id, "amount": g.amount, "effect": g.effect, "reason": g.reason}
     for g in res.gaps]
)

st.header("Gaps by type")
if gdf.empty:
    st.info("No gaps found.")
else:
    summ = gdf.groupby("type").agg(count=("txn_id", "count"), total_effect=("effect", "sum")).reset_index()
    summ["total_effect"] = summ["total_effect"].map(money)
    st.table(summ)

    st.header("Gap detail")
    pick = st.selectbox("Filter by type", ["All"] + sorted(gdf["type"].unique()))
    view = gdf if pick == "All" else gdf[gdf["type"] == pick]
    st.dataframe(view.astype({"amount": str, "effect": str}), hide_index=True)

    st.header("Rounding lines")
    rdf = gdf[gdf["type"] == rc.ROUNDING]
    st.write(f"{len(rdf)} lines, total effect **{money(sum(rdf['effect'], Decimal('0')))}**")
    st.dataframe(rdf.astype({"amount": str, "effect": str}), hide_index=True)

st.header("Assumptions")
st.markdown(ASSUMPTIONS)
