# ledger-ar-auditor

A zero-dependency Python 3.10+ CLI tool for **accounts receivable (AR) aging schedules, invoice-to-payment reconciliation, variance detection, and automated financial audit reporting**.

Built for business operations, finance tooling, and accounting data pipelines, `ledger-ar-auditor` uses exact `Decimal` arithmetic (`ROUND_HALF_UP`) rather than floating-point numbers to avoid rounding drift across invoices, partial payments, and credit memos.

## Key Capabilities

- **Exact Decimal Accounting:** Uses `decimal.Decimal` with two-decimal precision (`0.01`) for all balances, partial payments, and credit memos.
- **Standard AR Aging Buckets:** Categorizes open balances as of a configurable `--as-of` cutoff date into `Current (Not Due)`, `1-30`, `31-60`, `61-90`, and `>90` days past due.
- **Reconciliation & Exception Flagging:** Detects duplicate invoice IDs (`DUP-INV`), unapplied/orphan payments (`REC-001`), overpayments/credit variances (`REC-002`), invalid dates (`LED-005`/`DAT-001`), and malformed records (`LED-001`–`LED-007`).
- **Dual Output & CI Exit Codes:** Generates `ar_audit_report.json` and `ar_audit_summary.md`, returning exit code `1` on hard `ERROR` records.

## Quickstart

```bash
git clone [https://github.com/B5IVE/ledger-ar-auditor.git](https://github.com/B5IVE/ledger-ar-auditor.git)
cd ledger-ar-auditor

python3 audit_ledger.py sample_ledger.jsonl --as-of 2026-11-24
