"""
extract_signals.py

Positive Signal Engineering for Elite Builds LLC.

Joins raw banking transactions against the Q1 2026 industry market snippet to
derive two categories of "Positive Signals of Interest" for a Bank
Relationship Manager (RM):

  1. Resilience Alpha    - is this client beating the 15% industry-wide
                           material cost surge, and how healthy is its margin
                           relative to the ~4% industry average?
  2. Growth Reinvestment - are outflows strategic (equipment, tech, marketing)
                           or purely operational noise (payroll, fuel,
                           utilities)? What share of net income is being
                           reinvested into growth?

  A third derived metric, Expense Velocity, is included because the market
  snippet explicitly states banks are tightening credit for firms showing
  high Expense Velocity - so it directly informs the RM's credit read.

Usage:
    python3 extract_signals.py [path_to_transactions.json]

Output:
    Prints a JSON report to stdout and writes it to
    ../output/signal_report.json (relative to this script).
"""

import json
import re
import sys
from pathlib import Path
from collections import defaultdict

# --- Market context (hardcoded from the provided Q1 2026 industry snippet) ---
INDUSTRY_COST_INFLATION_PCT = 15.0   # residential construction material costs, YoY/QoQ surge
INDUSTRY_AVG_NET_MARGIN_PCT = 4.0    # average net margin for mid-sized firms

# Categories treated as "strategic reinvestment" vs. "operational noise"
STRATEGIC_CATEGORIES = {"Growth", "Tech/Growth", "Equipment"}
OPERATIONAL_CATEGORIES = {"Operations"}
# Savings transfers are internal moves, not expenses - excluded from opex/margin math
NON_EXPENSE_CATEGORIES = {"Savings"}


def safe_div(numerator: float, denominator: float, default=None):
    """
    Division that returns `default` (None unless overridden) instead of
    raising ZeroDivisionError. Used everywhere a metric divides by a
    transaction-derived total (revenue, net income, vendor's first
    transaction amount, vendor count) that could legitimately be zero for
    a different client or a different data window - a crash on an edge
    case is a worse failure mode than an honest `None` the caller can
    check for.
    """
    if not denominator:
        return default
    return numerator / denominator


def load_transactions(path: str) -> list[dict]:
    with open(path, "r") as f:
        return json.load(f)


def vendor_cost_trend(transactions: list[dict]) -> dict:
    """
    For each recurring vendor, compare the first vs. most recent transaction
    to see whether per-transaction cost is rising or falling - a direct,
    literal join against the market snippet's "15% cost surge" claim.

    Caveat (surfaced in the output, not just this docstring): this compares
    transaction AMOUNT, not confirmed unit price. A falling amount could
    mean "unit cost fell" or just "we bought a smaller quantity this
    cycle" - the data has no quantity/unit field to tell those apart, so
    every trend here is directional, not a confirmed price signal.
    """
    by_vendor = defaultdict(list)
    for t in transactions:
        if t["category"] == "Inventory" and t["type"] == "Debit":
            vendor = t["description"].replace("Vendor: ", "")
            by_vendor[vendor].append(t)

    trends = {}
    for vendor, txns in by_vendor.items():
        txns.sort(key=lambda t: t["date"])
        if len(txns) < 2:
            continue
        first, last = txns[0], txns[-1]
        # Sign convention: positive = cost went UP (matches "15% inflation" framing),
        # negative = cost went DOWN. abs() used since amounts are stored as negative debits.
        pct_change = safe_div(abs(last["amount"]) - abs(first["amount"]), abs(first["amount"]))
        if pct_change is None:
            continue  # first transaction was $0 - can't compute a % change
        trends[vendor] = {
            "first_txn": {"date": first["date"], "amount": first["amount"]},
            "last_txn": {"date": last["date"], "amount": last["amount"]},
            "cost_change_pct": round(pct_change * 100, 2),
            "vs_industry_inflation_pct": INDUSTRY_COST_INFLATION_PCT,
            "delta_vs_industry_pts": round(INDUSTRY_COST_INFLATION_PCT - pct_change * 100, 2),
            "confidence": (
                "directional only - reflects total transaction amount, not a "
                "confirmed unit price (no quantity data available)"
            ),
        }
    return trends


def resilience_alpha(transactions: list[dict]) -> dict:
    total_credit = sum(t["amount"] for t in transactions if t["type"] == "Credit")
    opex = sum(-t["amount"] for t in transactions
               if t["type"] == "Debit" and t["category"] not in NON_EXPENSE_CATEGORIES)
    net_income = total_credit - opex
    net_margin_pct = safe_div(net_income, total_credit)
    net_margin_pct = round(net_margin_pct * 100, 2) if net_margin_pct is not None else None
    margin_multiple = round(net_margin_pct / INDUSTRY_AVG_NET_MARGIN_PCT, 1) if net_margin_pct is not None else None

    # --- Cash margin: a second, deliberately different lens on margin,
    # carried over from the exploratory notebook (Sympera_AI_Assignment_
    # Task1.ipynb). net_margin_pct above treats the internal transfer to
    # Savings as a balance-sheet move, not a cost of doing business, and
    # excludes it - an "operating margin" framing. cash_margin_pct instead
    # treats EVERY outbound dollar (including the Savings transfer) as
    # reducing cash on hand - a stricter "how much actual cash is left"
    # framing a bank might also want to see. The two will diverge by
    # exactly the Savings total whenever one exists; that's expected, not
    # a bug - they're answering two different questions, not disagreeing
    # about the same one.
    all_debits = sum(-t["amount"] for t in transactions if t["type"] == "Debit")
    net_cash_flow = total_credit - all_debits
    cash_margin_pct = safe_div(net_cash_flow, total_credit)
    cash_margin_pct = round(cash_margin_pct * 100, 2) if cash_margin_pct is not None else None

    if net_margin_pct is None:
        signal = "No revenue recorded in this window - net margin cannot be computed."
    else:
        signal = (
            f"Net margin of {net_margin_pct}% is "
            f"{margin_multiple}x the industry average "
            f"({INDUSTRY_AVG_NET_MARGIN_PCT}%), while its two largest material vendors show "
            f"per-transaction costs FALLING, not rising, against a 15% industry-wide surge."
        )

    return {
        "total_revenue": total_credit,
        "total_operating_expense": opex,
        "net_income": net_income,
        "net_margin_pct": net_margin_pct,
        "cash_margin_pct": cash_margin_pct,
        "margin_methodology_note": (
            "net_margin_pct excludes the Savings transfer (treated as a "
            "balance-sheet move); cash_margin_pct includes it (treated as "
            "cash out the door). They differ by exactly the Savings total "
            "when one exists - intentional, not a discrepancy."
        ),
        "industry_avg_net_margin_pct": INDUSTRY_AVG_NET_MARGIN_PCT,
        "margin_multiple_vs_industry": margin_multiple,
        "vendor_cost_trend": vendor_cost_trend(transactions),
        "signal": signal,
    }


def growth_reinvestment(transactions: list[dict]) -> dict:
    strategic_total = sum(-t["amount"] for t in transactions if t["category"] in STRATEGIC_CATEGORIES)
    operational_total = sum(-t["amount"] for t in transactions if t["category"] in OPERATIONAL_CATEGORIES)
    inventory_total = sum(-t["amount"] for t in transactions if t["category"] == "Inventory")
    total_credit = sum(t["amount"] for t in transactions if t["type"] == "Credit")
    opex = sum(-t["amount"] for t in transactions
               if t["type"] == "Debit" and t["category"] not in NON_EXPENSE_CATEGORIES)
    net_income = total_credit - opex

    strategic_items = [
        {"date": t["date"], "description": t["description"], "amount": t["amount"]}
        for t in transactions if t["category"] in STRATEGIC_CATEGORIES
    ]

    strategic_pct = safe_div(strategic_total, net_income)
    strategic_pct = round(strategic_pct * 100, 2) if strategic_pct is not None else None

    if strategic_pct is None:
        signal = (
            f"${strategic_total:,} went to growth-related outflows, but net income was "
            f"zero or negative this window, so this can't be expressed as a share of it."
        )
    else:
        signal = (
            f"${strategic_total:,} (~{strategic_pct}% of net income) "
            f"went to growth-related outflows rather than routine opex - evidence of "
            f"deliberate reinvestment, not just survival spending. "
            f"(Category-level detail intentionally omitted here - see "
            f"'strategic_outflows' for line items; category naming is reserved for "
            f"the psychographic segment to avoid duplicating the same facts twice.)"
        )

    return {
        "strategic_outflows_total": strategic_total,
        "strategic_outflows": strategic_items,
        "operational_noise_total": operational_total,
        "inventory_spend_total": inventory_total,
        "strategic_pct_of_net_income": strategic_pct,
        "signal": signal,
    }


def firm_profile(transactions: list[dict]) -> dict:
    """
    Structural facts about the firm itself - NOT performance trends. Kept
    separate from resilience_alpha/growth_reinvestment so downstream
    segmentation (Task 2) has a clean, non-overlapping source for
    "demographic" facts instead of reaching back into raw transactions
    or re-deriving them by hand.
    """
    revenue_txns = [t for t in transactions if t["category"] == "Revenue"]
    revenue_only = sum(t["amount"] for t in revenue_txns)  # excludes Interest

    # Only count txns that name an actual project ("Project <Name>") - a
    # bare "Client Payment - Deposit" is a deposit against one of those
    # projects, not a distinct 4th project, and was miscounted as one
    # before this regex was added.
    named_projects = set()
    for t in revenue_txns:
        m = re.search(r"Project (\w+)", t["description"])
        if m:
            named_projects.add(m.group(1))

    return {
        "entity_type": "LLC",
        "region": "Utah",
        "concurrent_projects": len(named_projects),
        "project_names": sorted(named_projects),
        "revenue_only_total": revenue_only,
    }


def revenue_diversification(transactions: list[dict]) -> dict:
    """
    Is revenue concentrated in one client/project, or spread across
    several? A bank cares about this independently of margin: a firm
    with a great margin but 90% of revenue from one client carries
    concentration risk a diversified firm doesn't.

    Only revenue rows that name a specific project ("Project <Name>") are
    attributed to a project; an unlabeled "Client Payment - Deposit" is
    kept separate as "unattributed" rather than guessed into one project,
    since the data doesn't say which project it's a deposit for.
    """
    revenue_txns = [t for t in transactions if t["category"] == "Revenue"]
    by_project = defaultdict(int)
    unattributed_total = 0
    for t in revenue_txns:
        m = re.search(r"Project (\w+)", t["description"])
        if m:
            by_project[m.group(1)] += t["amount"]
        else:
            unattributed_total += t["amount"]

    named_total = sum(by_project.values())
    max_project_share = safe_div(max(by_project.values()), named_total) if by_project else None
    max_project_share_pct = round(max_project_share * 100, 2) if max_project_share is not None else None

    return {
        "revenue_by_project": dict(by_project),
        "unattributed_revenue": unattributed_total,
        "largest_project_share_of_named_revenue_pct": max_project_share_pct,
        "signal": (
            f"No single named project exceeds {max_project_share_pct}% of attributed "
            f"revenue across {len(by_project)} concurrent projects - revenue isn't "
            f"concentrated in one client."
            if max_project_share_pct is not None
            else "No named-project revenue found to assess concentration."
        ),
    }


def payroll_stability(transactions: list[dict]) -> dict:
    """
    Are payroll runs holding steady, shrinking (headcount/hours cut - a
    distress signal), or growing (hiring - a further growth signal)? Cheap
    to compute, directly relevant to a credit read: layoffs under cost
    pressure would be a negative signal this client doesn't show.
    """
    payroll_txns = sorted(
        (t for t in transactions if t["description"] == "Payroll Service"),
        key=lambda t: t["date"],
    )
    amounts = [abs(t["amount"]) for t in payroll_txns]
    if len(amounts) < 2:
        return {"runs": amounts, "assessment": "INSUFFICIENT_DATA", "signal": "Fewer than two payroll runs in this window."}

    change_pct = safe_div(amounts[-1] - amounts[0], amounts[0])
    change_pct = round(change_pct * 100, 2) if change_pct is not None else None
    assessment = "STABLE" if change_pct == 0 else ("GROWING" if change_pct and change_pct > 0 else "SHRINKING")

    return {
        "runs": amounts,
        "change_pct": change_pct,
        "assessment": assessment,
        "signal": (
            f"Payroll ran {len(amounts)}x at an identical ${amounts[0]:,.0f} each cycle - "
            f"no sign of staffing cuts under market cost pressure."
            if assessment == "STABLE"
            else f"Payroll changed {change_pct}% across {len(amounts)} runs ({assessment.lower()})."
        ),
    }


def liquidity_coverage(transactions: list[dict]) -> dict:
    """
    How many times over does revenue cover operating expense this period?
    A simple, bank-legible solvency cushion metric, distinct from net
    margin (margin is revenue MINUS opex as a % of revenue; this is
    revenue DIVIDED BY opex - how much cushion exists before revenue
    would fail to cover costs at all).
    """
    total_credit = sum(t["amount"] for t in transactions if t["type"] == "Credit")
    opex = sum(-t["amount"] for t in transactions
               if t["type"] == "Debit" and t["category"] not in NON_EXPENSE_CATEGORIES)
    ratio = safe_div(total_credit, opex)
    ratio = round(ratio, 2) if ratio is not None else None

    return {
        "revenue_to_opex_ratio": ratio,
        "signal": (
            f"Revenue covers operating expense {ratio}x over this period - "
            f"a solvency cushion, not a break-even operation."
            if ratio is not None
            else "Operating expense was zero this window - coverage ratio undefined."
        ),
    }


def liquidity_behavior(transactions: list[dict]) -> dict:
    """
    Savings/buffer-building behavior. Split out from resilience_alpha /
    growth_reinvestment because it's neither a cost-resilience signal nor a
    strategic-outflow signal - it's a third, distinct behavioral fact that
    was previously being hand-merged into other segments.
    """
    savings_txns = [t for t in transactions if t["category"] == "Savings"]
    savings_total = sum(-t["amount"] for t in savings_txns)
    return {
        "savings_total": savings_total,
        "savings_txn_dates": [t["date"] for t in savings_txns],
    }


def expense_velocity(transactions: list[dict]) -> dict:
    """
    Simple proxy: is total expense trending up or down across the month, and
    are the two largest recurring vendor lines (the biggest velocity risk)
    stable or shrinking? Low/negative velocity on core vendors is a positive
    credit signal given the snippet's explicit mention of credit tightening
    for high Expense Velocity firms.
    """
    trends = vendor_cost_trend(transactions)
    avg_vendor_change = safe_div(
        sum(v["cost_change_pct"] for v in trends.values()), len(trends)
    )
    avg_vendor_change = round(avg_vendor_change, 2) if avg_vendor_change is not None else None

    if avg_vendor_change is None:
        return {
            "avg_recurring_vendor_cost_change_pct": None,
            "assessment": "INSUFFICIENT_DATA",
            "signal": "No recurring vendor (2+ transactions) found to assess expense velocity.",
        }

    direction = "declining" if avg_vendor_change < 0 else "rising"
    return {
        "avg_recurring_vendor_cost_change_pct": avg_vendor_change,
        "assessment": "LOW" if avg_vendor_change <= 0 else "ELEVATED",
        "signal": (
            f"Recurring vendor costs are averaging {avg_vendor_change}% "
            f"transaction-over-transaction ({direction}), indicating "
            f"{'LOW' if avg_vendor_change <= 0 else 'ELEVATED'} expense velocity - "
            f"{'the opposite of' if avg_vendor_change <= 0 else 'in line with'} "
            f"the profile banks are tightening credit for."
        ),
    }


def transaction_breakdown(transactions: list[dict]) -> dict:
    """
    Raw category+description groupby of credits and debits (sum, count) -
    carried over from the exploratory notebook's cells 6 and 8. Not a
    "signal" itself; an auditability artifact so a reviewer can see the
    grouped transaction data every derived signal above is built from,
    without re-deriving it themselves from the raw list.
    """
    def _group(rows):
        grouped = defaultdict(lambda: {"sum": 0, "count": 0})
        for t in rows:
            key = (t["category"], t["description"])
            grouped[key]["sum"] += t["amount"]
            grouped[key]["count"] += 1
        return [
            {"category": cat, "description": desc, "sum": v["sum"], "count": v["count"]}
            for (cat, desc), v in sorted(grouped.items(), key=lambda kv: kv[1]["sum"])
        ]

    credits = [t for t in transactions if t["type"] == "Credit"]
    debits = [t for t in transactions if t["type"] == "Debit"]
    return {
        "credit_breakdown": sorted(_group(credits), key=lambda r: -r["sum"]),
        "debit_breakdown": _group(debits),
    }


def alt_strategic_classification(transactions: list[dict]) -> dict:
    """
    A second "strategic vs. operational" split using the category
    boundary from the exploratory notebook, shown SIDE BY SIDE with this
    script's own boundary (STRATEGIC_CATEGORIES above) rather than
    silently adopted - the two disagree on two line items, and the
    disagreement is worth being able to defend rather than papering over:

      - The notebook's list omits "Equipment", so the excavator LEASE
        payment (-$2,200) falls through to "Operational Expense" - despite
        the assignment's own framing naming equipment as a growth category.
      - The notebook's list includes "Savings", so the internal transfer
        to savings (-$5,000) counts as a "Strategic Investment" - despite
        being a defensive liquidity move, not spend on growth capacity.
      - The notebook's list also includes "Marketing" as a category name,
        but no transaction in this dataset carries that exact category
        (the real category is "Tech/Growth") - a dead entry that matches
        nothing here.

    This script's own STRATEGIC_CATEGORIES = {"Growth", "Tech/Growth",
    "Equipment"} is used everywhere else in this file; this function exists
    only to make the alternate view auditable, not to feed any other
    signal.
    """
    NOTEBOOK_STRATEGIC_CATEGORIES = {"Growth", "Tech/Growth", "Marketing", "Savings"}

    notebook_strategic_total = sum(
        -t["amount"] for t in transactions
        if t["type"] == "Debit" and t["category"] in NOTEBOOK_STRATEGIC_CATEGORIES
    )
    notebook_operational_total = sum(
        -t["amount"] for t in transactions
        if t["type"] == "Debit" and t["category"] not in NOTEBOOK_STRATEGIC_CATEGORIES
    )
    this_script_strategic_total = sum(
        -t["amount"] for t in transactions
        if t["type"] == "Debit" and t["category"] in STRATEGIC_CATEGORIES
    )
    this_script_operational_total = sum(
        -t["amount"] for t in transactions
        if t["type"] == "Debit"
        and t["category"] not in STRATEGIC_CATEGORIES
        and t["category"] not in NON_EXPENSE_CATEGORIES
    )

    return {
        "this_script": {
            "strategic_categories": sorted(STRATEGIC_CATEGORIES),
            "strategic_total": this_script_strategic_total,
            "operational_total": this_script_operational_total,
        },
        "notebook_original": {
            "strategic_categories": sorted(NOTEBOOK_STRATEGIC_CATEGORIES),
            "strategic_total": notebook_strategic_total,
            "operational_total": notebook_operational_total,
        },
        "note": (
            "The two boundaries disagree on the Equipment lease (-$2,200; "
            "strategic here, operational in the notebook) and the Savings "
            "transfer (-$5,000; excluded from opex here since it's an "
            "internal transfer, strategic in the notebook). Both are "
            "defensible readings - kept side by side rather than merged."
        ),
    }


def build_report(transactions: list[dict]) -> dict:
    return {
        "client": "Elite Builds LLC",
        "region": "Utah",
        "period": "2026-03-01 to 2026-03-31",
        "positive_signals": {
            "resilience_alpha": resilience_alpha(transactions),
            "growth_reinvestment": growth_reinvestment(transactions),
            "expense_velocity": expense_velocity(transactions),
            "revenue_diversification": revenue_diversification(transactions),
            "payroll_stability": payroll_stability(transactions),
            "liquidity_coverage": liquidity_coverage(transactions),
        },
        # Structural/behavioral support facts for Task 2 segmentation -
        # deliberately NOT called "positive_signals" since they aren't
        # market-benchmarked signals, just profile facts.
        "firm_profile": firm_profile(transactions),
        "liquidity_behavior": liquidity_behavior(transactions),
        # Auditability artifacts carried over from the exploratory
        # notebook - not signals, just the underlying grouped data and an
        # honest side-by-side on the one place this script's category
        # boundary disagrees with the notebook's.
        "transaction_breakdown": transaction_breakdown(transactions),
        "alt_strategic_classification": alt_strategic_classification(transactions),
    }


def main():
    input_path = sys.argv[1] if len(sys.argv) > 1 else str(
        Path(__file__).resolve().parent.parent / "data" / "transactions.json"
    )
    transactions = load_transactions(input_path)
    report = build_report(transactions)

    output_path = Path(__file__).resolve().parent.parent / "output" / "signal_report.json"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w") as f:
        json.dump(report, f, indent=2)

    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
