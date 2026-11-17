# onelab-recon: month-end payments reconciliation

Generates October 2026 platform and bank data with four planted gap types (timing, rounding, duplicate, orphan refund), detects each, explains it in plain language, and shows a bridge:
platform total - bank total = timing + rounding + duplicate + orphan refund + unexplained remainder.

## Assumptions
- Period: October 2026, IST. Platform date = `txn_timestamp` date as written (+05:30); bank gives settlement date only.
- Comparison basis: platform total = rows with an IST date in October; bank total = rows with a settlement date in October. Bank file runs to 3 Nov so November settlements are visible.
- Match key: `txn_id` = `reference`. Single currency INR, gross amounts, no fees, only successful transactions, no prior-month carryover.
- Settlement lag: 1 or 2 calendar days.
- Refunds: separate rows, negative amounts, with `original_txn_id` (platform) / `original_reference` (bank).
- Platform amounts have 3 decimals; bank amounts 2 decimals. All amounts are `Decimal`; no floats anywhere.

## Run
```
pip install -r requirements.txt
python generate_data.py      # regenerates data/ (seed 2026; already committed)
streamlit run app.py
```

## Test
```
python -m pytest -q
```

## What would go wrong in production
Real settlement lags vary (weekends, holidays, batch cut-offs), so a fixed 1-2 day assumption and a hard 3 Nov window would misclassify late settlements as missing. Real data has fees, partial refunds, multiple currencies, chargebacks and no clean shared key, so matching on `txn_id = reference` and a 0.005 rounding tolerance would produce false matches and larger unexplained remainders. Timezone handling, prior-month carryover and very large volumes (memory, performance, audit trail) would also need proper treatment before anyone relies on the bridge.
