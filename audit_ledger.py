#!/usr/bin/env python3
"""
ledger-ar-auditor: Accounts Receivable Aging & Ledger Reconciliation CLI.

Processes invoice and payment/credit records from a JSONL stream as of a
target reporting date, calculates AR aging buckets (Current, 1-30, 31-60,
61-90, 90+ days past due), flags unapplied/overpaid/mismatched balances,
and outputs a JSON audit report and Markdown summary.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any

TWO_DP = Decimal("0.01")


@dataclass
class AuditException:
    line_number: int
    invoice_id: str
    customer: str
    severity: str  # ERROR, WARN
    code: str
    message: str


@dataclass
class CustomerSummary:
    customer: str
    open_balance: str
    current_not_due: str
    days_1_30: str
    days_31_60: str
    days_61_90: str
    days_over_90: str
    open_invoices: int


def fmt_money(val: Decimal) -> str:
    return f"{val.quantize(TWO_DP, rounding=ROUND_HALF_UP):.2f}"


def parse_iso_date(raw: str) -> date:
    return date.fromisoformat(raw.strip()[:10])


def aging_bucket(days_past_due: int) -> str:
    if days_past_due <= 0:
        return "current_not_due"
    if days_past_due <= 30:
        return "days_1_30"
    if days_past_due <= 60:
        return "days_31_60"
    if days_past_due <= 90:
        return "days_61_90"
    return "days_over_90"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Audit AR invoices and payments for aging buckets, variances, and anomalies."
    )
    parser.add_argument("ledger_file", type=Path, help="Path to ledger .jsonl file")
    parser.add_argument(
        "--as-of",
        type=str,
        default=date.today().isoformat(),
        help="Reporting/aging cutoff date in YYYY-MM-DD format (default: today)",
    )
    parser.add_argument(
        "--json-out",
        type=Path,
        default=Path("ar_audit_report.json"),
        help="Output JSON report path (default: ar_audit_report.json)",
    )
    parser.add_argument(
        "--md-out",
        type=Path,
        default=Path("ar_audit_summary.md"),
        help="Output Markdown summary path (default: ar_audit_summary.md)",
    )
    args = parser.parse_args()

    if not args.ledger_file.is_file():
        print(f"Error: Ledger file not found: {args.ledger_file}", file=sys.stderr)
        return 2

    try:
        as_of_date = parse_iso_date(args.as_of)
    except ValueError as exc:
        print(f"Error: Invalid --as-of date '{args.as_of}': {exc}", file=sys.stderr)
        return 2

    invoices: dict[str, dict[str, Any]] = {}
    invoice_line: dict[str, int] = {}
    payments_by_invoice: dict[str, Decimal] = defaultdict(lambda: Decimal("0.00"))
    exceptions: list[AuditException] = []

    with args.ledger_file.open("r", encoding="utf-8") as handle:
        for line_num, raw_line in enumerate(handle, start=1):
            stripped = raw_line.strip()
            if not stripped:
                continue

            try:
                rec = json.loads(stripped)
            except json.JSONDecodeError as exc:
                exceptions.append(
                    AuditException(line_num, "N/A", "unknown", "ERROR", "LED-001", f"Malformed JSON: {exc}")
                )
                continue

            if not isinstance(rec, dict):
                exceptions.append(
                    AuditException(line_num, "N/A", "unknown", "ERROR", "LED-002", "Record must be a JSON object.")
                )
                continue

            rec_type = str(rec.get("type", "")).strip().lower()
            inv_id = str(rec.get("invoice_id", "")).strip()
            customer = str(rec.get("customer", "unknown")).strip() or "unknown"

            if not inv_id:
                exceptions.append(
                    AuditException(line_num, "missing_id", customer, "ERROR", "LED-003", "Missing invoice_id.")
                )
                continue

            try:
                amount = Decimal(str(rec["amount"])).quantize(TWO_DP, rounding=ROUND_HALF_UP)
            except (KeyError, InvalidOperation):
                exceptions.append(
                    AuditException(line_num, inv_id, customer, "ERROR", "LED-004", "Invalid or missing monetary amount.")
                )
                continue

            if rec_type == "invoice":
                if inv_id in invoices:
                    exceptions.append(
                        AuditException(
                            line_num,
                            inv_id,
                            customer,
                            "WARN",
                            "DUP-INV",
                            f"Duplicate invoice_id (first seen on line {invoice_line[inv_id]}).",
                        )
                    )
                    continue

                try:
                    inv_date = parse_iso_date(str(rec["invoice_date"]))
                    due_date = parse_iso_date(str(rec.get("due_date", rec["invoice_date"])))
                except (KeyError, ValueError) as exc:
                    exceptions.append(
                        AuditException(line_num, inv_id, customer, "ERROR", "LED-005", f"Invalid date field: {exc}")
                    )
                    continue

                if amount <= Decimal("0.00"):
                    exceptions.append(
                        AuditException(
                            line_num, inv_id, customer, "WARN", "AMT-001", f"Non-positive invoice amount: {amount}."
                        )
                    )

                if due_date < inv_date:
                    exceptions.append(
                        AuditException(
                            line_num,
                            inv_id,
                            customer,
                            "WARN",
                            "DAT-001",
                            f"Due date ({due_date}) precedes invoice date ({inv_date}).",
                        )
                    )

                invoices[inv_id] = {
                    "customer": customer,
                    "invoice_date": inv_date,
                    "due_date": due_date,
                    "amount": amount,
                }
                invoice_line[inv_id] = line_num

            elif rec_type in {"payment", "credit_memo"}:
                try:
                    tx_date = parse_iso_date(str(rec.get("date", args.as_of)))
                except ValueError as exc:
                    exceptions.append(
                        AuditException(line_num, inv_id, customer, "ERROR", "LED-006", f"Invalid payment date: {exc}")
                    )
                    continue

                if tx_date <= as_of_date:
                    payments_by_invoice[inv_id] += amount
            else:
                exceptions.append(
                    AuditException(
                        line_num, inv_id, customer, "ERROR", "LED-007", f"Unsupported record type {rec_type!r}."
                    )
                )

    # Check payments referencing unknown invoices
    for inv_id, paid_amt in payments_by_invoice.items():
        if inv_id not in invoices:
            exceptions.append(
                AuditException(
                    0,
                    inv_id,
                    "unknown",
                    "WARN",
                    "REC-001",
                    f"Unmatched payment/credit total of {fmt_money(paid_amt)} references unknown invoice_id.",
                )
            )

    # Compute aging per customer
    cust_buckets: dict[str, dict[str, Decimal]] = defaultdict(
        lambda: {
            "current_not_due": Decimal("0.00"),
            "days_1_30": Decimal("0.00"),
            "days_31_60": Decimal("0.00"),
            "days_61_90": Decimal("0.00"),
            "days_over_90": Decimal("0.00"),
            "open_balance": Decimal("0.00"),
            "open_invoices": Decimal("0"),
        }
    )

    total_receivables = Decimal("0.00")
    total_overdue = Decimal("0.00")

    for inv_id, inv in invoices.items():
        paid = payments_by_invoice.get(inv_id, Decimal("0.00"))
        balance = (inv["amount"] - paid).quantize(TWO_DP, rounding=ROUND_HALF_UP)

        if balance < Decimal("0.00"):
            exceptions.append(
                AuditException(
                    invoice_line[inv_id],
                    inv_id,
                    inv["customer"],
                    "WARN",
                    "REC-002",
                    f"Overpayment / credit variance of {fmt_money(abs(balance))} on invoice {inv_id}.",
                )
            )

        if balance != Decimal("0.00"):
            days_past_due = (as_of_date - inv["due_date"]).days
            bucket_key = aging_bucket(days_past_due)
            c_state = cust_buckets[inv["customer"]]
            c_state[bucket_key] += balance
            c_state["open_balance"] += balance
            c_state["open_invoices"] += Decimal("1")
            total_receivables += balance
            if days_past_due > 0:
                total_overdue += balance

    customer_summaries: list[CustomerSummary] = []
    for cust_name in sorted(cust_buckets.keys()):
        st = cust_buckets[cust_name]
        customer_summaries.append(
            CustomerSummary(
                customer=cust_name,
                open_balance=fmt_money(st["open_balance"]),
                current_not_due=fmt_money(st["current_not_due"]),
                days_1_30=fmt_money(st["days_1_30"]),
                days_31_60=fmt_money(st["days_31_60"]),
                days_61_90=fmt_money(st["days_61_90"]),
                days_over_90=fmt_money(st["days_over_90"]),
                open_invoices=int(st["open_invoices"]),
            )
        )

    err_count = sum(1 for e in exceptions if e.severity == "ERROR")
    warn_count = sum(1 for e in exceptions if e.severity == "WARN")

    report_obj = {
        "metadata": {
            "source_file": str(args.ledger_file),
            "as_of_date": as_of_date.isoformat(),
            "generated_utc": datetime.now(timezone.utc).isoformat(),
            "total_receivables": fmt_money(total_receivables),
            "total_overdue": fmt_money(total_overdue),
            "error_count": err_count,
            "warning_count": warn_count,
        },
        "customer_aging": [asdict(c) for c in customer_summaries],
        "exceptions": [asdict(e) for e in exceptions],
    }

    args.json_out.write_text(json.dumps(report_obj, indent=2), encoding="utf-8")

    md_lines = [
        "# Accounts Receivable Aging & Ledger Audit Report",
        "",
        f"- **Source File:** `{args.ledger_file.name}` | **As-Of Date:** `{as_of_date.isoformat()}`",
        f"- **Total Open Receivables:** `{fmt_money(total_receivables)}` | **Total Overdue:** `{fmt_money(total_overdue)}`",
        f"- **Audit Exceptions:** `{err_count}` errors, `{warn_count}` warnings",
        "",
        "## Customer Aging Schedule",
        "",
        "| Customer | Open Items | Current | 1-30d | 31-60d | 61-90d | >90d | Open Balance |",
        "| :--- | :---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for c in customer_summaries:
        md_lines.append(
            f"| `{c.customer}` | {c.open_invoices} | {c.current_not_due} | {c.days_1_30} | "
            f"{c.days_31_60} | {c.days_61_90} | {c.days_over_90} | **{c.open_balance}** |"
        )

    md_lines.extend(["", "## Reconciliation & Validation Exceptions", ""])
    if not exceptions:
        md_lines.append("_No ledger or reconciliation exceptions detected._")
    else:
        md_lines.extend(
            [
                "| Line | Invoice ID | Customer | Severity | Code | Detail |",
                "| :---: | :--- | :--- | :---: | :--- | :--- |",
            ]
        )
        for ex in exceptions:
            md_lines.append(
                f"| {ex.line_number} | `{ex.invoice_id}` | `{ex.customer}` | **{ex.severity}** | `{ex.code}` | {ex.message} |"
            )

    args.md_out.write_text("\n".join(md_lines).strip() + "\n", encoding="utf-8")

    print(
        f"[+] AR Audit ({as_of_date.isoformat()}): Open={fmt_money(total_receivables)}, "
        f"Overdue={fmt_money(total_overdue)}, Errors={err_count}, Warnings={warn_count}."
    )
    print(f"[+] Wrote {args.json_out} and {args.md_out}")

    return 1 if err_count > 0 else 0


if __name__ == "__main__":
    sys.exit(main())
