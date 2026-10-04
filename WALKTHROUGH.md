# Technical Explanation & Architecture Walkthrough (5-Minute Overview)

**Candidate:** Harsh Barewar  
**Email:** harshbarewar112@gmail.com  
**Role:** Backend Engineer (Fresher)  
**Company:** DataFuel  
**Repository:** [https://github.com/HarshBarewar/datafuel-assignment](https://github.com/HarshBarewar/datafuel-assignment)  

---

## 1. Executive Summary & System Architecture

This project implements an end-to-end data pipeline that scrapes store inventories from the adversarial **QuickMart Partner Portal** mock server, stores observations with atomic idempotency in SQLite, and serves a high-performance **On-Shelf Availability (OSA)** report API using FastAPI.

The solution is divided into three decoupled layers:

```
┌────────────────────────────────┐     ┌────────────────────────────────┐     ┌────────────────────────────────┐
│             db.py              │     │            sweep.py            │     │             app.py             │
│  Relational SQLite Schema &    │ ──> │   Ingestion Scraper with Rate  │ ──> │   FastAPI Service Serving      │
│  Timezone Normalization (IST)  │     │   Pacing & Soft-Ban Mitigation │     │   GET /osa Reporting Endpoint  │
└────────────────────────────────┘     └────────────────────────────────┘     └────────────────────────────────┘
```

1. **`db.py` (Relational Schema & Helpers):** Manages SQLite connection and schemas across four normalized tables (`stores`, `sweeps`, `store_sweeps`, and `observations`) with compound primary keys and foreign keys to prevent duplicate records.
2. **`sweep.py` (Scraper):** Batch scraper that safely reads paginated inventory data while handling rate limits (429), random errors (500/503), slow delays (8s), silent degradation (soft bans), and partial snapshots.
3. **`app.py` (Reporting API):** Serves `GET /osa?city=<City>&date=<YYYY-MM-DD>`, validating city/date, grouping observations by IST calendar day, computing OSA from aggregate ratios, and reporting transparent coverage metrics.

---

## 2. Handling Server Misbehavior in `sweep.py`

QuickMart simulates the unpredictable behavior of real-world partner APIs. Here is how each failure mode is mitigated:

### 2.1 The Fair-Use Soft Ban (Silent Degradation)
- **Problem:** Exceeding 30 requests in a 10-second window triggers a silent 20-second degradation where the server returns HTTP 200, but data comes from CDN `edge` with empty or truncated listings. Requests during the ban extend it by 5 seconds.
- **Proactive Mitigation:** Implemented a client-side `Pacer` class running at ~2.0 requests per second (`0.5s` delay between requests), ensuring request volume remains well under the 30-request ceiling.
- **Reactive Recovery:** `fetch_store_inventory` inspects `meta.source`. If `meta.source != "origin"`, the attempt is recognized as degraded: all collected data for the store is discarded, the scraper halts all traffic for a **25-second cooldown** to allow the 20-second ban to fully expire, and restarts the store from `cursor = "0"`.

### 2.2 Burst Rate Limits (HTTP 429) & Bounded Retries
- Bursts over 8 req/s return HTTP 429 with `Retry-After: 2`.
- `safe_get` extracts `Retry-After`, adds a 200ms buffer, sleeps, and retries.
- For transient server failures (HTTP 500, 503, timeouts), exponential backoff (`1.0 * 1.5^(attempt-1)`) is applied up to 5 attempts. Fatal client errors (400, 401, 404) fail fast without retrying.
- **Store `MUM-007`** intentionally fails with HTTP 500 on attempts 1 and 2, but succeeds on attempt 3. Our 5-attempt threshold guarantees complete data.

### 2.3 Slow Requests (8-Second Sleeps)
- QuickMart injects random 8-second sleeps on 3% of calls.
- Enforced a strict **5.0-second HTTP timeout** on `requests.get`. Slow calls time out quickly and succeed on retry instead of hanging the process.

### 2.4 Duplicate Items Across Pages
- QuickMart duplicates the last item of the previous page when `cursor > 0`.
- The scraper collects items in an in-memory dictionary keyed by `sku_id` (`items_by_sku[it["sku_id"]] = it`), collapsing page boundary duplicates before database insertion.

---

## 3. Data Integrity & The Golden Rule

> *"A wrong number is worse than no number. Never turn missing data into 'out of stock'."*

### 3.1 Partial Snapshot Handling (`DEL-004`)
- On sweep `2026-09-28T10:30:00Z`, store `DEL-004` returns `partial: true` with an empty item list.
- The scraper immediately halts pagination, marks the store-sweep as `incomplete` with reason `'portal returned partial snapshot'`, and writes **zero observations** to the database.
- It is excluded from the OSA denominator so it does not pull availability down to 0%; instead, it is surfaced transparently in `coverage.incomplete`.

### 3.2 Ghost Stock Handling
- QuickMart contains ghost stock where `in_stock = True` but `qty = 0` (e.g. central fulfillment).
- The API contract specifies `in_stock` as the source of truth, so products with `in_stock = True, qty = 0` are correctly counted as in stock.

### 3.3 Indian Standard Time (IST) Calendar Day Mapping
- The report API parameter `date` represents an **IST calendar day (UTC + 5:30)**.
- Sweep `2026-09-27T19:00:00Z` is `00:30:00` on `2026-09-28` in IST.
- Converting timestamps using `ist_calendar_date` ensures all 3 sweeps (`2026-09-27 19:00`, `2026-09-28 04:30`, and `2026-09-28 10:30`) are correctly grouped under date `2026-09-28`.

### 3.4 Store Selection Policy (`is_active` vs `is_serviceable`)
- Out of 30 stores, 4 are inactive (`is_active = false`), of which two claim `is_serviceable = true`.
- We track stores where **`is_active == true`** (26 stores: 9 Mumbai, 9 Delhi, 8 Bengaluru). Inactive stores claiming serviceability are decommissioned ghost entities, whereas active stores that are temporarily unserviceable (rain, maintenance) still carry real physical inventory.

---

## 4. Verification & Automated Test Suite

### 4.1 Pytest Suite (`pytest -v`)
All 10 tests run in isolation using in-memory SQLite databases:

```text
tests/test_osa.py::test_incomplete_store_is_excluded_from_osa_and_in_coverage PASSED [ 10%]
tests/test_osa.py::test_ghost_stock_counted_as_in_stock PASSED                     [ 20%]
tests/test_osa.py::test_ist_calendar_day_mapping PASSED                            [ 30%]
tests/test_osa.py::test_no_data_returns_null_never_zero PASSED                     [ 40%]
tests/test_osa.py::test_city_osa_ratio_of_totals PASSED                            [ 50%]
tests/test_osa.py::test_renamed_sku_displays_latest_name PASSED                    [ 60%]
tests/test_osa.py::test_api_validation_errors PASSED                               [ 70%]
tests/test_sweep.py::test_timestamp_conversions PASSED                             [ 80%]
tests/test_sweep.py::test_duplicate_items_deduplicated PASSED                      [ 90%]
tests/test_sweep.py::test_pacer_pacing_interval PASSED                             [100%]
============================== 10 passed in 1.23s ==============================
```

### 4.2 Verified Results for `date=2026-09-28`

| City | Expected Stores | Complete Stores | Incomplete Stores | Total Observations | Overall OSA % |
|---|---|---|---|---|---|
| **Mumbai** | 9 | 9 | 0 | 753 | **86.06%** |
| **Delhi** | 9 | 8 | 1 (`DEL-004` partial snapshot) | 761 | **79.63%** |
| **Bengaluru** | 8 | 8 | 0 | 690 | **89.57%** |

*(Stored in `results_2026-09-28.json` on GitHub).*

---

## 5. Code Review of `review_me.py`

Identified and ranked 8 distinct defects in `REVIEW.md`. Patched the top 3 critical bugs directly in `review_me.py`:
1. **Mutable default argument (`results=[]`):** Caused `MUM-002` to inherit all products from `MUM-001`. Fixed by defaulting to `None` and initializing inside the function.
2. **Infinite retry loop & missing timezone:** `datetime.utcnow().isoformat()` lacks timezone, triggering HTTP 400. The blind `except Exception: continue` loop fired 10 req/s indefinitely. Fixed by providing UTC timezone-aware ISO strings, adding timeouts, and aborting on fatal 4xx errors.
3. **SQL injection & quote syntax errors:** Raw f-strings broke on product names containing apostrophes (e.g. *"Lay's"*). Fixed using SQLite parameterized queries `(?, ?, ...)`.

---

## 6. Scaling to 20,000 Stores Every 30 Minutes

To ingest 20,000 stores every 30 minutes (~40,000 pages / 1800s ≈ 22.2 req/s):
1. **Distributed Task Queue:** Replace the serial loop with Celery or Temporal workflows dispatching independent store tasks.
2. **Distributed Rate Limiting:** Implement sliding-window rate limiters in Redis to enforce pacing across distributed worker nodes per portal endpoint.
3. **Egress Proxy Pools:** Distribute outbound requests across a rotating proxy pool so fair-use ceilings are not bottlenecked on a single IP address.
4. **PostgreSQL Partitioning:** Migrate from SQLite to PostgreSQL with table partitioning on `(ist_date, city)` and bulk COPY operations.
5. **Observability:** Prometheus/Grafana tracking scrape durations, coverage rates, and automated alerting on drops in data coverage.

---

## 7. Conclusion

The repository is completely clean, documented, and tested. I look forward to walking through the architecture and performing live code modifications during the technical interview!
