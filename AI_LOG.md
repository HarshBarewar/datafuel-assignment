# AI Usage Log (`AI_LOG.md`)

This log documents the usage of AI coding assistants during the implementation of the DataFuel backend assignment, highlighting key prompts, critical verification steps, and instances where AI suggestions were flawed.

---

## 1. Tools Used & Purpose

| Tool | Purpose |
|---|---|
| Claude 3.5 Sonnet / Antigravity Agent | Scaffolding initial boilerplate for SQLite schema, FastAPI endpoints, and test fixtures. |
| GitHub Copilot | Autocomplete for repetitive boilerplate and SQL query parameters. |

---

## 2. Key Prompts That Mattered

### Prompt 1: Rate Limiting & Soft-Ban Mitigation Strategy
> *"In mock_portal.py, the server tracks request history per API key over a 10-second window. If more than 30 requests occur, it degrades responses for 20 seconds, returning HTTP 200 with meta.source='edge' and truncated items. Making requests during the ban extends the penalty. Design a client-side pacing and recovery mechanism."*

- **Outcome:** Led to the implementation of the `Pacer` class running at ~2.0 req/s (20 requests per 10s window) to proactively avoid the soft-ban threshold, coupled with an explicit 25-second cooldown and store restart upon detecting `meta.source != 'origin'`.

### Prompt 2: IST Date Cohort Grouping
> *"Given UTC timestamps such as '2026-09-27T19:00:00Z', convert them to the Indian Standard Time (IST = UTC + 5:30) calendar date and explain which date '2026-09-27T19:00:00Z' belongs to."*

- **Outcome:** Generated the conversion logic proving that `19:00:00Z` on 27 Sep equals `00:30:00` on 28 Sep in IST, confirming that 3 sweeps belong to `2026-09-28`.

---

## 3. Instances Where AI Was Wrong & How It Was Caught

### Mistake 1: Suggesting High-Concurrency Asynchronous Scraping
- **What the AI proposed:** The AI initially recommended using `asyncio` with `httpx` or `ThreadPoolExecutor(max_workers=10)` to scrape all 26 stores in parallel in "under 5 seconds".
- **Why it was wrong:** Parallel requests would instantly breach the 8 req/s burst limit (triggering HTTP 429) and would immediately overwhelm the 30 requests per 10 seconds fair-use ceiling, causing QuickMart to enter a soft ban. Under a soft ban, the portal returns HTTP 200 with empty or truncated listings from CDN edge. High-concurrency scraping would have quietly poisoned the database with false stock-outs.
- **How it was caught:** By reading the soft-ban logic in `mock_portal.py` (`SOFT_LIMIT = 30`, `SOFT_WINDOW = 10.0`), it was clear that sustained serial pacing (2 req/s) was essential for data accuracy.

### Mistake 2: Timezone Slicing via `substr(observed_at, 1, 10)`
- **What the AI proposed:** In `review_me.py` and initial draft queries, the AI suggested filtering date cohorts using `substr(observed_at, 1, 10) = ?`.
- **Why it was wrong:**
  1. QuickMart stores emit mixed timezone formats: Delhi emits IST (`+05:30`), while Mumbai and Bengaluru emit UTC (`Z`).
  2. For sweeps conducted at 19:00:00Z, the first 10 characters are `2026-09-27`, but in IST the observation belongs to `2026-09-28`. String slicing silently dropped observations from early-morning IST cohorts.
- **How it was caught:** Comparing observation counts against expected cohort sizes revealed missing rows for Delhi when using string slicing. Replaced with explicit `to_utc_iso()` and `ist_calendar_date()` datetime conversions.

### Mistake 3: Inverting Database Foreign Key Insertion Order
- **What the AI proposed:** In `sweep.py`, the AI wrote the database save block with `INSERT INTO observations ...` followed by `INSERT INTO store_sweeps ...`.
- **Why it was wrong:** `observations` defines `FOREIGN KEY (as_of, store_id) REFERENCES store_sweeps(as_of, store_id)`. With SQLite foreign key constraints enabled (`PRAGMA foreign_keys = ON`), inserting child rows into `observations` before inserting the parent record in `store_sweeps` raises `sqlite3.IntegrityError: FOREIGN KEY constraint failed`.
- **How it was caught:** Immediate failure upon running the first sweep test in Python. Swapped the insertion sequence to insert `store_sweeps` before `observations`.
