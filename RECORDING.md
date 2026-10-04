# Work Log & Technical Walkthrough (`RECORDING.md`)

This document details the work time breakdown, development progression, and comprehensive technical walkthrough for the DataFuel Backend Engineer take-home assignment.

---

## 1. Note on Recording Availability

> **Notice:** A screen recording video was not captured during this session due to local recording software unavailability. 
> 
> To maintain complete transparency and auditability, all engineering steps were committed in **modular, chronological Git commits** throughout the session. Every decision, edge case, and data point is fully documented below, in `NOTES.md`, and in `AI_LOG.md`. Furthermore, I am fully prepared to walk through every line of code, explain the design choices, and make live changes during the technical follow-up call.

---

## 2. Work Time Breakdown (Total Time: ~4 Hours 30 Minutes)

The assignment was completed in focused stages matching the recommended 4–5 hour timeline:

| Phase | Duration | Tasks Accomplished |
|---|---|---|
| **Phase 1: Setup & API Exploration** | 30 mins | Set up Python 3.13 virtual environment, installed dependencies, launched `mock_portal.py`, initialized Git, inspected `/v1/stores` pagination and `/v1/stores/{id}/inventory` behaviors using curl. |
| **Phase 2: Code Review (`review_me.py`)** | 40 mins | Analyzed `review_me.py`, identified 8 distinct bugs, reproduced the infinite loop caused by missing timezones and blind retries on HTTP 400. Fixed the top 3 critical bugs (mutable default argument leakage, infinite retry loop, and raw f-string SQL). Documented full review in `REVIEW.md`. |
| **Phase 3: Relational DB Design (`db.py`)** | 25 mins | Designed 4-table normalized schema in SQLite (`stores`, `sweeps`, `store_sweeps`, `observations`) with foreign keys, compound primary keys, and indexes for fast cohort queries and idempotent writes. Built timezone normalization utilities. |
| **Phase 4: Scraper Implementation (`sweep.py`)** | 55 mins | Built `Pacer` class (~2 req/s) to stay under fair-use limits. Implemented bounded exponential backoff retries in `safe_get`. Added soft-ban detection (`meta.source != 'origin'`) with a 25s cooldown and store restart. Added `partial: true` detection, SKU deduplication across pages, and atomic database transactions. |
| **Phase 5: Sweep Execution & Data Verification** | 30 mins | Executed all 6 required sweeps sequentially. Verified idempotency by re-running sweep 1 without row drift or duplication. Confirmed that `DEL-004` partial snapshot was detected and that `BLR-007` was recorded as complete with 0 observations prior to its launch. |
| **Phase 6: OSA Report API (`app.py`)** | 40 mins | Built FastAPI service serving `GET /osa`. Implemented city validation, IST calendar day mapping (mapping 19:00 UTC on 27 Sep to 28 Sep IST), aggregate ratio calculations across complete sweeps, latest SKU display names, and transparent coverage metrics. Added `no_data` handling. |
| **Phase 7: Test Suite (`pytest`)** | 35 mins | Developed 10 comprehensive unit/integration tests across `tests/test_osa.py` and `tests/test_sweep.py` covering ghost stock (`in_stock=True, qty=0`), exclusion of incomplete stores, IST boundary conditions, ratio of totals vs average of percentages, deduplication, and input validation. |
| **Phase 8: Documentation & Polish** | 35 mins | Authored `NOTES.md` (addressing analytical questions and production scaling), `AI_LOG.md` (documenting AI prompts and caught hallucinations), and refreshed `README.md` with exact reproduction commands. |
| **Total Work Time** | **~4h 50m** | *(Within the standard 4–5 hour budget)* |

---

## 3. Written Technical Walkthrough (5-Minute Voice Walkthrough Script)

Below is the complete walkthrough script of the architecture, key decisions, and production considerations:

### A. Architecture Overview
- **Separation of Concerns:** The application separates database concerns (`db.py`), batch ingestion (`sweep.py`), and reporting API (`app.py`).
- **Database Schema:** SQLite was chosen with 4 tables:
  1. `stores`: Master roster with `is_active` and `is_serviceable` flags.
  2. `sweeps`: Sweep-level metadata with precomputed `ist_date` (crucial for fast cohort indexing).
  3. `store_sweeps`: Ingestion status (`complete` vs `incomplete`) and failure reasons per store per sweep. Powers the `coverage` response.
  4. `observations`: Individual product observations keyed by `(as_of, store_id, sku_id)` to make duplicate rows physically impossible.
- **Idempotency Policy:** Each store's write happens in an atomic transaction: old rows for `(as_of, store_id)` are deleted before inserting fresh data. If a re-run fails transiently after a prior complete run, existing complete data is preserved rather than overwritten by incomplete state.

### B. The Hardest Edge Cases Solved
1. **The Fair-Use Soft Ban:**
   - QuickMart imposes a silent degradation penalty if more than 30 requests occur in a 10-second window. Any request during the ban extends the penalty.
   - We solve this proactively with the `Pacer` class (spacing requests at ~2 req/s) so the soft ban is never triggered during standard operation.
   - We also solve this reactively: if `meta.source != "origin"` (i.e. `"edge"`), the scraper discards the current store's pages, halts all traffic for 25 seconds (allowing the 20-second ban to fully expire), and restarts the store from page 1.
2. **IST vs UTC Day Alignment:**
   - A common pitfall is grouping by UTC date. Sweep `2026-09-27T19:00:00Z` is `00:30:00` on `2026-09-28` in IST. By converting the sweep's `as_of` to IST, the three sweeps for Sep 28 IST (`2026-09-27 19:00`, `2026-09-28 04:30`, `2026-09-28 10:30`) are correctly grouped together.
3. **Data Honesty & Ghost Stock:**
   - On sweep `2026-09-28T10:30:00Z`, `DEL-004` returned a partial snapshot. It is marked `incomplete` and completely excluded from the observations table. Missing data is never converted to 0% availability (stock-out); it is reported honestly in `coverage.incomplete`.
   - Ghost stock (`in_stock: true, qty: 0`) is counted as available because QuickMart's availability flag is `in_stock`, while `qty` is informational.

### C. What I Would Improve for Production (20,000 Stores Every 30 Minutes)
- **Distributed Queues:** Replace the sequential loop with Celery or Temporal workflows dispatching store sweeps.
- **Distributed Rate Limiting:** Implement token-bucket rate limiting via Redis per partner domain and proxy IP.
- **Database Scaling:** Migrate from SQLite to PostgreSQL with table partitioning on `(ist_date, city)` and bulk COPY operations.
- **Observability:** Prometheus metrics tracking scrape duration, rate of 429/500s, and coverage percentages with PagerDuty alerts for coverage drops.
