"""
generate_segments.py

Builds output/customer_segments.json PROGRAMMATICALLY from
output/signal_report.json - closing three gaps from earlier passes:

  1. Nothing actually GENERATED the segmentation (it was hand-typed).
     Fixed: this script deterministically assembles it from
     signal_report.json (a real LLM call is still not wired in - see the
     note at the bottom of this file).

  2. Facts leaked across segments (e.g. "equipment" named in both
     behavioral and psychographic bullets). Fixed: each fact is assigned
     to exactly ONE segment, enforced by KEYWORD_OWNER + check_no_overlap()
     which raises instead of silently shipping a duplicate.

  3. The 300-token budget was "ensured" by hand-counting characters/4 and
     hoping the wording stayed short - no enforcement, and no exact count.
     Fixed:
       - count_tokens() uses a real tokenizer (tiktoken/cl100k_base) when
         it can be loaded, and only falls back to a chars/4 approximation
         if that fails (e.g. no network access to fetch the encoding
         file - which is the case in this assignment's own execution
         sandbox, but not on a normal dev machine or CI runner). Whichever
         method is used is printed, never silently assumed.
       - Every fact is tagged with a priority (1 = must-keep,
         2 = nice-to-have). assemble_within_budget() adds facts in
         priority order and checks the REAL running token count after
         each addition, dropping any fact that would push the total over
         budget. This means the budget holds even if a future data change
         makes some number's string representation longer - nothing has
         to be hand-retuned.
       - If trimming would leave a required segment empty, it raises
         rather than silently shipping a segment with no content (the
         assignment requires all three segments to be present).

Usage:
    python3 generate_segments.py [path_to_signal_report.json]
"""

import json
import re
import sys
from pathlib import Path

TOKEN_BUDGET = 300

# --- Ownership rule: each keyword may only appear in bullets tagged with
# --- its designated segment.
KEYWORD_OWNER = {
    "lumber": "behavioral",
    "steel": "behavioral",
    "net margin": "behavioral",
    "expense velocity": "behavioral",
    "savings": "behavioral",
    "growth-related": "behavioral",
    "payroll": "behavioral",
    "coverage": "behavioral",
    "cash margin": "behavioral",
    "autocad": "psychographic",
    "seo": "psychographic",
    "equipment": "psychographic",
    "excavator": "psychographic",
    "llc": "demographic",
    "utah": "demographic",
    "project": "demographic",   # matches "project"/"projects"
    "revenue": "demographic",
    "concentrat": "demographic",  # matches "concentrated"/"concentration"
}


def check_no_overlap(segments: dict) -> None:
    """Raise if any keyword appears in a segment it doesn't own."""
    for segment_name, bullets in segments.items():
        for bullet in bullets:
            lowered = bullet.lower()
            for keyword, owner in KEYWORD_OWNER.items():
                if re.search(rf"\b{re.escape(keyword)}", lowered) and owner != segment_name:
                    raise ValueError(
                        f"Overlap violation: keyword '{keyword}' (owned by "
                        f"'{owner}') found in '{segment_name}' bullet: {bullet!r}"
                    )


# --- Token counting: exact when possible, honest fallback when not. -------

_TOKENIZER = None
_TOKENIZER_METHOD = None


def _load_tokenizer() -> None:
    """
    Load a real tokenizer once and cache the result (success or failure) so
    count_tokens() doesn't retry a slow/failing network call on every
    candidate check during trimming.
    """
    global _TOKENIZER, _TOKENIZER_METHOD
    if _TOKENIZER_METHOD is not None:
        return
    try:
        import tiktoken
        _TOKENIZER = tiktoken.get_encoding("cl100k_base")
        _TOKENIZER_METHOD = "exact (tiktoken/cl100k_base)"
    except Exception as e:
        _TOKENIZER = None
        _TOKENIZER_METHOD = (
            f"approximate (chars/4 - tiktoken unavailable: {type(e).__name__}: {e})"
        )


def count_tokens(text: str) -> int:
    _load_tokenizer()
    if _TOKENIZER is not None:
        return len(_TOKENIZER.encode(text))
    return round(len(text) / 4)


# --- Fact extraction: each fact tagged with its owning segment and a
# --- priority (1 = must-keep even if budget is tight, 2 = dropped first
# --- if trimming is needed). --------------------------------------------

def extract_facts(report: dict) -> list:
    ra = report["positive_signals"]["resilience_alpha"]
    gr = report["positive_signals"]["growth_reinvestment"]
    ev = report["positive_signals"]["expense_velocity"]
    profile = report["firm_profile"]
    liquidity = report["liquidity_behavior"]

    lumber = ra["vendor_cost_trend"].get("Intermountain Lumber")
    steel = ra["vendor_cost_trend"].get("Steel Supply Co")
    growth_items = {i["description"] for i in gr["strategic_outflows"]}

    facts = []

    # Behavioral - quantitative performance trends only.
    if lumber and steel:
        facts.append({
            "segment": "behavioral", "priority": 1,
            "text": (
                f"Lumber ({lumber['cost_change_pct']}%) and steel ({steel['cost_change_pct']}%) "
                f"fell txn-over-txn vs a {round(lumber['vs_industry_inflation_pct'])}% industry surge."
            ),
        })
    facts.append({
        "segment": "behavioral", "priority": 1,
        "text": (
            f"Net margin {ra['net_margin_pct']}% = {ra['margin_multiple_vs_industry']}x industry "
            f"avg ({round(ra['industry_avg_net_margin_pct'])}%)."
        ),
    })
    if ra.get("cash_margin_pct") is not None:
        facts.append({
            "segment": "behavioral", "priority": 2,
            "text": f"Cash margin {ra['cash_margin_pct']}% (incl. savings transfer as outflow).",
        })
    facts.append({
        "segment": "behavioral", "priority": 1,
        "text": (
            f"{gr['strategic_pct_of_net_income']}% of net income "
            f"(${gr['strategic_outflows_total']:,}) is growth-related, not operational spend."
        ),
    })
    facts.append({
        "segment": "behavioral", "priority": 2,
        "text": (
            f"Expense velocity {ev['assessment']} ({ev['avg_recurring_vendor_cost_change_pct']}% avg) - "
            f"opposite of the profile banks are tightening credit for."
        ),
    })
    if liquidity["savings_total"] > 0:
        facts.append({
            "segment": "behavioral", "priority": 2,
            "text": f"Built ${liquidity['savings_total']:,} savings buffer same period as growth spend.",
        })

    # New signals (payroll stability, liquidity coverage) - behavioral,
    # since both are period-over-period operating trends, same category
    # as margin/velocity above.
    payroll = report["positive_signals"].get("payroll_stability", {})
    if payroll.get("assessment") == "STABLE":
        facts.append({
            "segment": "behavioral", "priority": 2,
            "text": f"Payroll unchanged across {len(payroll['runs'])} runs - no staffing cuts under cost pressure.",
        })

    coverage = report["positive_signals"].get("liquidity_coverage", {})
    if coverage.get("revenue_to_opex_ratio") is not None:
        facts.append({
            "segment": "behavioral", "priority": 1,
            "text": f"Monthly inflows cover opex {coverage['revenue_to_opex_ratio']}x - a solvency cushion.",
        })

    # Confidence caveat on the vendor cost trend - a falling transaction
    # amount could mean unit cost fell OR quantity purchased fell; the
    # data has no unit/quantity field to tell those apart. Surfaced as its
    # own low-priority fact so it's dropped first under a tight budget
    # rather than silently omitted always.
    if lumber and steel:
        facts.append({
            "segment": "behavioral", "priority": 2,
            "text": "Vendor cost trend is directional (amount-based); quantity data unavailable to confirm unit price.",
        })

    # Demographic - static/structural facts only.
    facts.append({
        "segment": "demographic", "priority": 1,
        "text": f"Small residential construction {profile['entity_type']}, {profile['region']}.",
    })
    facts.append({
        "segment": "demographic", "priority": 1,
        "text": f"Revenue ~${round(profile['revenue_only_total'] / 1000)}K/month.",
    })
    facts.append({
        "segment": "demographic", "priority": 2,
        "text": f"{profile['concurrent_projects']} concurrent named projects this period - multi-crew capacity.",
    })
    diversification = report["positive_signals"].get("revenue_diversification", {})
    max_share = diversification.get("largest_project_share_of_named_revenue_pct")
    if max_share is not None:
        facts.append({
            "segment": "demographic", "priority": 2,
            "text": f"Revenue not concentrated - largest project is {max_share}% of attributed revenue.",
        })

    # Psychographic - specific vendor/category names + interpretation.
    if any("AutoCAD" in d for d in growth_items) and any("SEO" in d for d in growth_items):
        facts.append({
            "segment": "psychographic", "priority": 1,
            "text": "Pays for AutoCAD and local SEO - tech-forward, brand-conscious operator.",
        })
    if any("Equipment" in d for d in growth_items) and any("Excavator" in d for d in growth_items):
        facts.append({
            "segment": "psychographic", "priority": 2,
            "text": "Owns equipment (downpayment) alongside leasing (excavator) - values long-term asset control.",
        })

    return facts


def assemble_within_budget(facts, meta):
    """
    Add facts to their segment in priority order, checking the real
    serialized token count after each addition. Any fact that would push
    the total over TOKEN_BUDGET is dropped (not added) rather than added
    and trimmed after the fact - so the returned object is guaranteed to
    fit before it's ever written to disk.
    """
    ordered = sorted(facts, key=lambda f: f["priority"])
    included = {"behavioral": [], "demographic": [], "psychographic": []}
    dropped = []

    for fact in ordered:
        included[fact["segment"]].append(fact["text"])
        candidate = {**meta, "segments": included}
        if count_tokens(json.dumps(candidate)) > TOKEN_BUDGET:
            included[fact["segment"]].pop()
            dropped.append(fact["text"])

    empty = [seg for seg, bullets in included.items() if not bullets]
    if empty:
        raise ValueError(
            f"Token budget ({TOKEN_BUDGET}) too tight to fill required segment(s): "
            f"{empty}. Raise the budget or shorten the meta fields."
        )

    return included, dropped


def build_rag_chunks(meta: dict, segments: dict) -> list:
    """
    A real retrieval system doesn't embed one big JSON blob - it embeds
    independently-retrievable chunks, each carrying enough metadata to be
    used, filtered, and dated on its own. This flattens the same
    (already budget-checked) segments into that shape as a second
    artifact, so a query like "what's this client's spending behavior"
    can retrieve just the behavioral chunks instead of the whole object.
    """
    chunks = []
    for segment_type, bullets in segments.items():
        for i, text in enumerate(bullets):
            chunks.append({
                "chunk_id": f"{meta['customer_id']}__{segment_type}__{i}",
                "customer_id": meta["customer_id"],
                "segment_type": segment_type,
                "as_of": meta["as_of"],
                "text": text,
            })
    return chunks


def main():
    input_path = sys.argv[1] if len(sys.argv) > 1 else str(
        Path(__file__).resolve().parent.parent / "output" / "signal_report.json"
    )
    with open(input_path) as f:
        report = json.load(f)

    facts = extract_facts(report)
    meta = {
        "customer_id": "elite_builds_llc",
        "industry": "residential_construction",
        "region": report["firm_profile"]["region"],
        "period": "2026-03",
        "as_of": report["period"].split(" to ")[-1],  # end date of the txn window
        "source": "signal_report.json",
    }

    segments, dropped = assemble_within_budget(facts, meta)
    check_no_overlap(segments)

    output = {**meta, "segments": segments}

    output_dir = Path(__file__).resolve().parent.parent / "output"
    with open(output_dir / "customer_segments.json", "w") as f:
        json.dump(output, f, indent=2)

    chunks = build_rag_chunks(meta, segments)
    with open(output_dir / "rag_chunks.json", "w") as f:
        json.dump(chunks, f, indent=2)

    print(json.dumps(output, indent=2))

    final_count = count_tokens(json.dumps(output))
    print(f"\n[tokens: {final_count} / {TOKEN_BUDGET} budget - method: {_TOKENIZER_METHOD}]",
          file=sys.stderr)
    if dropped:
        print(f"[dropped {len(dropped)} lower-priority fact(s) to stay in budget:]", file=sys.stderr)
        for text in dropped:
            print(f"  - {text}", file=sys.stderr)
    print(f"[wrote {len(chunks)} retrieval-ready chunks to output/rag_chunks.json]", file=sys.stderr)


# NOTE on the "real LLM call" gap: this script still does NOT call
# api.anthropic.com. It deterministically assembles bullets from
# signal_report.json's numbers using the same rules system_prompt.txt asks
# an LLM to follow. To wire in an actual model call: send system_prompt.txt
# as the system message and json.dumps(report) as the user message to
# /v1/messages, parse the JSON text block back out, and run it through
# check_no_overlap() + a token count of the result before writing - none of
# the enforcement logic above changes, only where the bullet text comes from.
if __name__ == "__main__":
    main()
