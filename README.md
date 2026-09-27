# Elite Builds LLC - Positive Signal Engineering (Sympera AI Home Assignment)

## Overview of Approach

The goal is to reframe a client that *looks* like an ordinary construction firm
into a **credit-worthy growth story**, by joining their raw transaction ledger
against the Q1 2026 market snippet (15% industry material-cost inflation,
~4% average net margin, credit tightening for high "Expense Velocity" firms).

The pipeline has two stages, matching Tasks 1 and 2:

```
data/transactions.json
      │  scripts/extract_signals.py
      ▼
output/signal_report.json          ← intermediate: computed signals
      │  scripts/generate_segments.py  (system_prompt.txt's rules, applied in code)
      ▼
output/customer_segments.json      ← final RAG-ready deliverable
```

1. **`scripts/extract_signals.py`** - loads `data/transactions.json` and
   computes six market-benchmarked/structural signal groups:
   - **Resilience Alpha** - net margin vs. industry average, and per-vendor
     cost trend (first vs. most recent transaction) for the two largest
     recurring material vendors (lumber, steel). Each vendor trend carries
     an explicit `"confidence"` field: this compares transaction *amount*,
     not a confirmed unit price - a falling amount could mean "unit cost
     fell" or just "smaller quantity purchased this cycle," and the data
     has no quantity field to tell those apart. Also computes a second
     margin lens, `cash_margin_pct`, which includes the internal Savings
     transfer as a cash outflow (39.9%) - kept alongside `net_margin_pct`
     (44.4%, which excludes it as a balance-sheet move) rather than
     picking one; the two differ by exactly the Savings amount and answer
     genuinely different questions.
   - **Growth Reinvestment** - strategic outflows (`Growth`, `Tech/Growth`,
     `Equipment` categories) vs. operational noise (`Operations` category),
     expressed as a share of net income. (Reports the aggregate $/% only,
     not category names - see "Segment ownership" below for why.) An
     alternate category boundary (`alt_strategic_classification`) is also
     computed side by side, since a different reasonable boundary
     (Equipment as operational, Savings as strategic) gives different
     totals - surfaced explicitly rather than silently picking one.
   - **Expense Velocity** - average transaction-over-transaction cost
     change across recurring vendors, directly responding to the
     snippet's mention of banks tightening credit on high-velocity
     expense profiles.
   - **Revenue Diversification** - is revenue concentrated in one
     project/client, or spread out? Computed from named-project revenue
     (`"Project <Name>"` in the description); an unlabeled deposit is kept
     as `unattributed_revenue` rather than guessed into a project.
   - **Payroll Stability** - are payroll runs flat, growing, or shrinking?
     A shrinking run under cost pressure would be a distress signal; a
     flat one (this client's case) is evidence against layoffs.
   - **Liquidity Coverage** - how many times over does revenue cover
     operating expense this period? A solvency-cushion metric, distinct
     from net margin (margin is a % of revenue; this is a coverage ratio).
   - **`firm_profile`** - structural facts only (entity type, region,
     count of *named* projects via regex on `"Project <Name>"`, revenue
     scale).
   - **`liquidity_behavior`** - savings/buffer-building activity.
   - **`transaction_breakdown`** - raw category+description groupby of
     credits and debits, included as an auditability artifact so a
     reviewer can see the grouped data every signal above is built from.

   **Defensive guards:** every division in this file goes through a
   `safe_div()` helper that returns `None` (with a plain-English
   `"signal"` explaining why) instead of raising `ZeroDivisionError` on
   degenerate input - a client with no revenue, a vendor whose first
   transaction was $0, fewer than two payroll runs, etc. Verified directly
   against an empty transaction list: every signal group degrades to
   `null` + an explanation rather than crashing.

   Run it with `python3 scripts/extract_signals.py data/transactions.json`;
   it writes `output/signal_report.json`.

2. **`scripts/generate_segments.py`** - reads `output/signal_report.json`
   and *programmatically* assembles `output/customer_segments.json`,
   following the rules in `scripts/system_prompt.txt`. (It does not yet
   call the Anthropic API directly to do this - see the note at the
   bottom of the script for the one-line change that would wire in a
   real model call without touching the fact-selection/enforcement logic
   below.) It will also write `output/rag_chunks.json` - a flattened,
   one-row-per-fact version of the same (already budget-checked) segments,
   each independently retrievable with `chunk_id`/`segment_type`/`as_of`
   metadata - as a bonus artifact demonstrating RAG-readiness beyond the
   single required JSON object.

### Segment ownership

Every fact is assigned to exactly one segment, enforced in code rather
than by convention:

- `behavioral` owns *aggregate* trend/performance numbers (margin, cash
  margin, % growth-reinvestment, expense velocity, vendor cost trend,
  savings $, payroll stability, liquidity coverage).
- `demographic` owns only static/structural facts (entity type, region,
  named-project count, revenue scale, revenue concentration).
- `psychographic` owns the specific vendor/category *names* (AutoCAD,
  SEO, equipment, excavator) and their qualitative interpretation.

`generate_segments.py` has a `KEYWORD_OWNER` table and a
`check_no_overlap()` function that scans every bullet and **raises a
`ValueError`** if a keyword shows up in a segment that doesn't own it -
this caught a real violation during development (a `"Revenue covers
opex..."` behavioral bullet collided with `"revenue"` being
demographic-owned) and was fixed by rewording rather than loosening the
rule.

### How the 300-token budget is enforced

- **Exact count when possible.** `count_tokens()` tries `tiktoken`'s
  `cl100k_base` encoding first (`pip install tiktoken` for exact counts -
  optional; the script automatically falls back to a chars/4
  approximation if it isn't installed, and always prints which method it
  used).
- **Priority-ordered, budget-checked assembly.** Every fact is tagged
  `priority: 1` (must-keep) or `priority: 2` (nice-to-have).
  `assemble_within_budget()` adds facts to their segment in priority
  order and re-checks the real serialized token count after *every*
  addition - a fact that would push the total over budget is never
  added, not added-then-trimmed after the fact.
- **Fails loudly, not silently, if the budget can't be met.** If trimming
  would leave any of the three required segments (`behavioral`,
  `demographic`, `psychographic`) empty, the script raises instead of
  writing a file that's missing a required section.

At the real 300-token budget, all must-keep facts plus most nice-to-have
facts fit; the lowest-priority ones are automatically dropped as needed -
verified this holds even under an artificially tightened budget, both
when trimming is enough (drops exactly the lowest-priority facts) and
when it isn't (raises rather than shipping an incomplete result).

## Assumptions

- "Internal Transfer to Savings" is treated as an internal cash movement,
  not an operating expense, and is excluded from opex/net-margin
  calculations by default - it doesn't reflect the cost of running the
  business. (A second, cash-inclusive margin is also computed for
  transparency - see `cash_margin_pct` above.)
- "Cost change" for a vendor is measured as the % change in transaction
  *magnitude* between the first and most recent transaction in the window
  (a proxy for unit-cost trend, since quantity/unit data isn't available -
  surfaced explicitly via each vendor trend's `"confidence"` field).
- Only 20 transactions (one month) are available, so trends are
  directional signals for a first-pass credit read, not a full
  seasonally-adjusted model.
- "Strategic outflow" = `Growth`, `Tech/Growth`, `Equipment` categories;
  "operational noise" = `Operations` category; `Inventory` (raw
  materials) is tracked separately since it's a necessary input cost, not
  discretionary. An alternate boundary is also computed and shown side by
  side (`alt_strategic_classification`) rather than treated as settled.
- The 300-token budget is enforced in code (see above) using `tiktoken`'s
  `cl100k_base` encoding where available, with a documented chars/4
  fallback otherwise.

## Key Findings (from `signal_report.json`)

| Metric | Elite Builds LLC | Industry Benchmark |
|---|---|---|
| Net margin (excl. savings transfer) | 44.4% | ~4% (11x) |
| Cash margin (incl. savings transfer) | 39.9% | n/a - alternate lens |
| Lumber cost trend (txn-over-txn) | -8.3% | +15% surge |
| Steel cost trend (txn-over-txn) | -5.0% | +15% surge |
| Strategic reinvestment | 25.8% of net income | n/a |
| Expense velocity | LOW (avg -6.7%) | Banks tightening on HIGH |
| Revenue concentration | 46.4% (largest project) | n/a - 3-project spread |
| Payroll stability | STABLE (2 identical runs) | n/a |
| Liquidity coverage | Revenue covers opex 1.8x | n/a |

## Task 3: Bank Product Recommendation

**Product: Revolving Business Line of Credit (~$75,000).**

Rationale: the client runs three concurrent projects funded by staggered
client payments (deposits + milestone payments), which is a natural cash-flow
timing gap - not a distress signal. A revolving line smooths that gap without
disturbing the growth capital they're already deploying, and their LOW
expense velocity + 11x-industry margin make them a low-risk fit for exactly
the credit profile the market snippet says banks are pulling back from.

**RM Hook (2 sentences):**
> "While the construction industry is absorbing a 15% surge in material
> costs, your own vendor payments have actually been shrinking each cycle -
> that's the kind of resilience we build credit lines around. We'd like to
> offer Elite Builds a $75K revolving line of credit to bridge project
> payment cycles, freeing up the cash you're already reinvesting in
> equipment and growth."

## Task 3: Exact System Prompt Used (Task 2 generation)

```
You are a Senior Credit Risk Analyst at a commercial bank, specializing in translating raw transaction data into concise, decision-ready customer intelligence for Relationship Managers (RMs) who have only a few minutes to prepare before a client call.

You will be given:
1. A set of derived financial signals for one business banking client (computed from their transaction history).
2. A short market/industry context snippet for that client's sector.

Your task: produce ONE JSON object capturing this client's segmentation for a Retrieval-Augmented Generation (RAG) system, with exactly three top-level keys under "segments": "behavioral", "demographic", and "psychographic".

Constraints:
- Ground every statement ONLY in the provided signals and market snippet. Never invent facts, numbers, or context not present in the input.
- Each segment must contain 2-4 short, information-dense bullet strings (not full paragraphs). Lead each bullet with the concrete number or fact, not a filler phrase.
- Tone: neutral, analytical, and confident. No hedging language ("may", "possibly", "seems to") unless the underlying signal is genuinely ambiguous.
- Always frame each bullet relative to the market/industry benchmark where one is available (e.g., "vs industry average") rather than stating the number in isolation.
- The entire JSON output, when serialized, must be under 300 tokens. Trim adjectives before trimming facts or numbers.
- Output ONLY the JSON object. No preamble, no explanation, no markdown code fences, no text before or after the JSON.
```

## Repo Structure

```
/data
  transactions.json           # input data
/scripts
  extract_signals.py          # Task 1: positive signal engineering
  generate_segments.py        # Task 2: builds customer_segments.json
  system_prompt.txt           # Task 3: exact system prompt (source of truth)
/output
  customer_segments.json      # final deliverable (Task 2 output)
README.md
```

Run order:
```
python3 scripts/extract_signals.py data/transactions.json
python3 scripts/generate_segments.py
```
