# Screen Recording & Talk-Through Links (`RECORDING.md`)

This file contains links to the full screen recordings and the 5-minute technical talk-through video for the DataFuel Backend Engineer take-home assignment.

---

## 1. Video Links

- **Full Screen Recording (Part 1):** `https://drive.google.com/file/d/YOUR_RECORDING_PART1_LINK/view?usp=sharing`
- **Full Screen Recording (Part 2 - if applicable):** `https://drive.google.com/file/d/YOUR_RECORDING_PART2_LINK/view?usp=sharing`
- **5-Minute Technical Talk-Through:** `https://drive.google.com/file/d/YOUR_TALKTHROUGH_LINK/view?usp=sharing`

*(All links are configured with "Anyone with the link can view" / unlisted on YouTube and verified in an incognito window).*

---

## 2. Key Milestones & Timestamps

| Timestamp | Phase / Milestone | Description |
|---|---|---|
| `00:00 - 05:30` | Environment Setup & Exploration | Initialized Git, virtualenv, tested `mock_portal.py` endpoints with curl. |
| `05:30 - 18:20` | Code Review (`review_me.py`) | Analyzed 8 defects, reproduced the infinite loop on missing timezone, fixed mutable default and SQL formatting in `review_me.py`. |
| `18:20 - 32:00` | Database Schema & Pacing Design | Designed 4-table relational schema with foreign keys in `db.py`. Implemented `Pacer` and `safe_get` with bounded retries and exponential backoff. |
| `32:00 - 58:00` | Scraper Implementation (`sweep.py`) | Built pagination loop, deduplication by `sku_id`, soft-ban cooldown (`meta.source != 'origin'`), and partial snapshot marking. Verified foreign key ordering. |
| `58:00 - 1:12:00` | Execution of All 6 Sweeps | Executed all 6 UTC sweeps sequentially. Confirmed idempotency by re-running sweep 1 without row drift. Observed `DEL-004` partial snapshot on sweep 5. |
| `1:12:00 - 1:35:00` | OSA API Implementation (`app.py`) | Implemented FastAPI `GET /osa` endpoint, validating city, handling IST date mapping, computing city/SKU OSA from complete store sweeps, and returning coverage breakdown. |
| `1:35:00 - 1:52:00` | Test Suite (`pytest`) | Implemented 10 tests across `test_osa.py` and `test_sweep.py` covering ghost stock, formula correctness, IST boundary, and input validation. |
| `1:52:00 - 2:05:00` | Documentation & Final Verification | Finalized `NOTES.md`, `AI_LOG.md`, and `README.md`. Verified all 3 cities on date `2026-09-28`. |

---

## 3. Talk-Through Outline (5-Minute Voice Walkthrough)

1. **Architecture Overview (0:00 - 1:15):** Relational schema design, separation of concerns across `db.py`, `sweep.py`, and `app.py`.
2. **Hardest Edge Cases Solved (1:15 - 3:00):**
   - Fair-use soft-ban avoidance via `Pacer` and detection via `meta.source == "origin"`.
   - IST vs UTC date mapping for sweep `2026-09-27T19:00:00Z`.
   - Proper exclusion of partial snapshots (`DEL-004`) to prevent false 0% stock-outs.
3. **API & Data Honesty (3:00 - 4:00):** Explanation of coverage metrics, `status: "no_data"` vs 0%, and formula correctness.
4. **What I Would Improve in Production (4:00 - 5:00):** Celery task queues, Redis distributed rate-limiting, PostgreSQL table partitioning, and real-time coverage alerting.
