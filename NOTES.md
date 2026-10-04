# Engineering Notes & Design Decisions

This document outlines key engineering decisions, edge-case mitigations, database architecture, answers to analytical questions, and scalability considerations for the DataFuel QuickMart integration.

---

## 1. Main Decisions & Problem Handling

### 1.1 Store Tracking Rule (`is_active` vs `is_serviceable`)
- **Observation:** The portal exposes 30 stores (10 per city: Mumbai, Delhi, Bengaluru). However, store flags exhibit contradictions:
  - `MUM-009` and `BLR-004` are `is_active: false` but `is_serviceable: true`.
  - `DEL-010` and `BLR-010` are `is_active: false` and `is_serviceable: false`.
  - `DEL-006` is `is_active: true` but `is_serviceable: false`.
  - All other 25 stores are `is_active: true` and `is_serviceable: true`.
- **Decision:** We track stores strictly where **`is_active == true`** (total 26 stores: 9 Mumbai, 9 Delhi, 8 Bengaluru).
- **Rationale:**
  - `is_active` represents network membership—whether a store is an operational dark store within QuickMart's system. Inactive stores that report `is_serviceable: true` are decommissioned entities with stale flag artifacts. Tracking them would ingest ghost data from dead nodes.
  - Conversely, `is_serviceable` indicates whether a store is accepting customer orders *at this exact moment*. Dark stores temporarily flip serviceability during heavy rain, temporary staffing shortages, or system maintenance, but their inventory remains real, on the shelf, and countable. Skipping active unserviceable stores would falsely hide valid stock data.

### 1.2 Rate Limiting (Burst 429) & Bounded Retries
- **Observation:** Bursts exceeding 8 req/s trigger HTTP 429 with `Retry-After: 2`.
- **Mitigation:**
  - `safe_get` inspects HTTP 429, extracts `float(headers.get("Retry-After", 2))`, adds a 200ms buffer, and sleeps before retrying.
  - Pacing ensures requests never exceed ~2 req/s under normal conditions, preventing burst limits entirely.

### 1.3 Random Failures (500, 503) & MUM-007
- **Observation:** ~7% of requests return transient 503 errors. Store `MUM-007` deliberately returns HTTP 500 for the first 2 attempts on every inventory sweep, succeeding only on attempt 3.
- **Mitigation:**
  - Implemented bounded retries (up to 5 attempts) with exponential backoff (`1.0 * 1.5^(attempt-1)`).
  - Fatal client errors (400, 401, 404) are never retried.

### 1.4 Slow Server Delays (8 Seconds)
- **Observation:** ~3% of requests sleep for 8 seconds.
- **Mitigation:**
  - Set a strict 5.0-second HTTP timeout on `requests.get`. Slow requests time out quickly and are retried, finishing on the subsequent attempt without blocking the pipeline.

### 1.5 Fair-Use Soft Ban Detection & Recovery
- **Observation:** Exceeding 30 requests in a 10-second window triggers a silent degradation lasting 20 seconds. Degraded responses return HTTP 200, but items are empty or truncated, `next_cursor` is null, and `meta.source` is `"edge"` rather than `"origin"`. Any request sent during the ban extends the penalty by 5 seconds (up to 60 seconds).
- **Mitigation:**
  - **Prevention (Pacer):** The `Pacer` class enforces an inter-request delay (`gap = 0.5s`), keeping request volume at ~2 req/s (20 requests per 10s window), well below the 30-request threshold.
  - **Detection:** `fetch_store_inventory` verifies `meta.source == "origin"`. If `meta.source != "origin"` (typically `"edge"`), a `SoftBanError` is recognized.
  - **Recovery:** Any partial data for that store attempt is discarded. The scraper halts all outbound traffic for 25 seconds to guarantee full expiration of the 20-second soft-ban window without penalty extension, then restarts the store from `cursor = "0"`.

### 1.6 Partial Snapshot Handling (DEL-004)
- **Observation:** On sweep `2026-09-28T10:30:00Z`, `DEL-004` returns `partial: true` with an empty item list.
- **Mitigation:**
  - When `body.get("partial") is True`, pagination stops immediately.
  - The store-sweep is marked `incomplete` with reason `"portal returned partial snapshot"`.
  - Zero observations are saved for this store in this sweep. Incomplete data is **never** turned into 0% availability (stock-out). It is reported transparently in `coverage.incomplete`.

### 1.7 Duplicate Items Across Pagination Pages
- **Observation:** When `cursor > 0`, the portal duplicates the last item from the previous page in ~35% of responses.
- **Mitigation:** Items are keyed by `sku_id` in an in-memory dictionary (`items_by_sku[it["sku_id"]] = it`) before database insertion. The database also enforces `PRIMARY KEY (as_of, store_id, sku_id)` as an invariant constraint.

### 1.8 Unlaunched Store (`BLR-007`)
- **Observation:** Store `BLR-007` officially launched on `2026-09-27 12:00:00 UTC`. For earlier sweeps (`04:30:00Z` and `10:30:00Z`), the portal legitimately returns 0 products with `meta.source: "origin"` and `partial: false`.
- **Mitigation:** The scraper records the store-sweep as `status: 'complete'` with `item_count: 0`. It contributes 0 observations (not 0% OSA), accurately reflecting reality.

### 1.9 Mixed Timezone Formats & Product Price Types
- **Observation:**
  - Delhi stores emit `observed_at` in IST offset (`+05:30`), while Mumbai and Bengaluru emit in UTC (`Z`).
  - Bengaluru emits `price` as formatted strings (`"443.00"`), while Mumbai/Delhi emit numbers (`443.0`).
- **Mitigation:**
  - Timestamps are parsed with timezone awareness and normalized to canonical UTC ISO strings (`%Y-%m-%dT%H:%M:%SZ`).
  - Prices are cast via `float(it["price"])`.

### 1.10 Product Rename (`SKU-0001`)
- **Observation:** On `2026-09-28 00:00:00 UTC`, `SKU-0001` is renamed from `"Amul Taaza Toned Milk 500ml"` to `"Amul Taaza Toned Milk 500 ml Pouch"`.
- **Mitigation:** Inventory records and queries group strictly by `sku_id`. For display in the `/osa` endpoint, the latest known name is queried and returned.

### 1.11 Mapping Sweeps to IST Days
- **Observation:** The API parameter `date` represents an Indian Standard Time (IST = UTC + 5:30) calendar day.
- **Mapping:**
  - `2026-09-27T04:30:00Z` -> IST `10:00:00 27 Sep` -> `2026-09-27`
  - `2026-09-27T10:30:00Z` -> IST `16:00:00 27 Sep` -> `2026-09-27`
  - `2026-09-27T19:00:00Z` -> IST `00:30:00 28 Sep` -> **`2026-09-28`**
  - `2026-09-28T04:30:00Z` -> IST `10:00:00 28 Sep` -> `2026-09-28`
  - `2026-09-28T10:30:00Z` -> IST `16:00:00 28 Sep` -> `2026-09-28`
  - `2026-09-28T18:40:00Z` -> IST `00:10:00 29 Sep` -> **`2026-09-29`**
- **Decision:** Sweeps are mapped to IST dates using the sweep's target `as_of` timestamp. This preserves cohort consistency so that all stores captured during the same sweep belong to the same business day.

### 1.12 Re-run Idempotency Policy
- **Decision:** Running `sweep.py` twice for the same `--as-of` produces identical database state.
- **Worse Re-run Rule:** If a store was marked `complete` on run #1, but encounters a transient issue on run #2, the scraper preserves the existing complete data rather than overwriting valid observations with an incomplete state. If run #1 was incomplete and run #2 succeeds, the data is updated to complete.

---

## 2. Answers to Analytical Questions

### Question 1: Delhi availability fell from 92% to 41% yesterday. What do you check first?
Before responding to the brand, check data pipeline health and data coverage:
1. **Coverage & Completeness:** Did stores return partial snapshots, or were stores hit by soft bans/degraded edge responses that returned empty item lists? Check `coverage.incomplete` and `store_sweeps.reason`.
2. **Total Observation Volume:** Compare total observations for Delhi yesterday versus normal days. If observation counts plummeted, the denominator is compromised.
3. **Store Roster Changes:** Verify if active stores were dropped or if decommissioned stores were erroneously ingested.
4. **Timezone Alignment:** Confirm that sweeps were correctly grouped by the IST calendar day (e.g., verifying Delhi's `+05:30` timestamps did not cause offset shifts).
5. **Only after ruling out data pipeline degradation**, inspect legitimate stock-outs (e.g. city-wide warehouse outages or supply chain stock-outs for key SKUs).

### Question 2: Dashboard says ₹4.20 lakh, store-level sum says ₹4.61 lakh. Which number do you show?
- **Response:** Show the **store-level sum (₹4.61 lakh) alongside an explicit reconciliation note explaining the delta**, rather than hiding the discrepancy.
- **Rationale:**
  - Store-level numbers represent bottom-up, auditable, granular transaction facts that can be verified against individual store receipts, inventory movements, and timestamps.
  - High-level dashboard figures often include top-down adjustments (e.g., delayed returns/refunds, promotional platform discounts, cross-city billing offsets, or mismatched time cutoffs like UTC vs IST).
  - Showing numbers that brands cannot trace to store-level operational realities erodes trust. Present the verifiable store-level sum and detail the exact reconciliation items bridging the two numbers.

### Question 3: Where would you NOT use an AI/LLM in this project, and why?
- **Response:** Never use an LLM for **data ingestion, arithmetic computations, availability determination, or database writes**.
- **Rationale:**
  - Metrics like OSA (`SUM(in_stock) / COUNT(*) * 100`), ISO-8601 timezone normalization, and soft-ban detection require deterministic, reproducible, and verifiable logic.
  - LLMs are probabilistic text predictors susceptible to subtle hallucinations, off-by-one errors, math inaccuracies, and nondeterministic outputs across runs. Auditable business intelligence requires deterministic code.

---

## 3. Bonus: Scaling to 20,000 Stores Every 30 Minutes

To ingest 20,000 stores across multiple platforms every 30 minutes (~40,000 pages / 30 mins ≈ 22.2 requests/sec):
1. **Distributed Task Queue:** Decouple scheduling from scraping using Redis/Celery or Temporal. Workflows dispatch per-store sweep jobs with bounded retries and exponential backoff.
2. **Per-Domain Distributed Rate Limiting:** Implement token-bucket or sliding-window rate limiters across workers (e.g. Redis Lua scripts) partitioned by target portal domain and egress IP to strictly honor fair-use ceilings.
3. **Proxy Rotation & IP Pooling:** Spread requests across egress IPs to distribute origin load and isolate soft bans.
4. **Relational Database Sharding / Partitioning:** Migrate from SQLite to PostgreSQL with table partitioning by `(ist_date, city)`. Use bulk inserts (`COPY`) rather than individual transactions.
5. **Real-Time Observability & Dead-Letter Queues:** Monitor scrape success rates, latency, and coverage percentage with Prometheus/Grafana. Route persistent failures to dead-letter queues for asynchronous replay.
