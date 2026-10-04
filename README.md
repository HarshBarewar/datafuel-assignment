# DataFuel Backend Take-Home Assignment

An end-to-end data ingestion scraper and reporting API for QuickMart quick-commerce store inventories, built with Python, SQLite, FastAPI, and pytest.

---

## 1. Project Structure

```
datafuel-take-home/
├── README.md            # Setup, execution commands, and project documentation
├── sweep.py             # Inventory scraper with rate pacing, soft-ban mitigation, and retry logic
├── app.py               # FastAPI report service for GET /osa
├── db.py                # Database connection, relational schema, and timezone utilities
├── tests/
│   ├── test_osa.py      # Pytest suite for OSA metrics, ghost stock, coverage, and validation
│   └── test_sweep.py    # Pytest suite for scraper mechanics, timestamps, and deduplication
├── mock_portal.py       # QuickMart partner portal mock server (UNCHANGED)
├── API.md               # QuickMart API contract documentation (UNCHANGED)
├── review_me.py         # Code review target with top 3 critical bug fixes applied
├── REVIEW.md            # Detailed code review ranking 8 defects with examples and fixes
├── NOTES.md             # Architecture decisions, problem handling, analytical Q&A, and scaling
├── AI_LOG.md            # AI usage disclosure, prompt log, and error corrections
├── RECORDING.md         # Screen recording links, milestones, and talk-through structure
├── requirements.txt     # Python package dependencies
└── .gitignore           # Ignores database files (*.db), venv, and cache directories
```

---

## 2. Setup & Installation

### Requirements
- Python 3.10+ (tested on Python 3.13)
- SQLite3 (standard library)

### Setup Commands

```bash
# 1. Clone the repository
git clone <YOUR_REPO_URL>
cd datafuel-take-home

# 2. Create and activate a virtual environment
python -m venv .venv
# On Windows PowerShell:
.venv\Scripts\activate
# On Linux / macOS:
source .venv/bin/activate

# 3. Install dependencies
pip install -r requirements.txt
```

---

## 3. Starting the Mock Server

In a dedicated terminal, start the QuickMart mock portal:

```bash
python mock_portal.py
```
*The mock portal will start on `http://127.0.0.1:8765`.*
*(If port 8765 is in use, start with `PORT=9000 python mock_portal.py` and pass `--portal http://127.0.0.1:9000` to `sweep.py`).*

Verify it is running:
```bash
curl http://127.0.0.1:8765/v1/health
# Response: {"ok": true}
```

---

## 4. Running the Scraper (`sweep.py`)

The scraper collects inventory for all tracked active stores (`is_active: true`, 26 stores total across Mumbai, Delhi, and Bengaluru), handles rate limits, recovers from soft bans, normalizes timezones and prices, deduplicates items, and saves records idempotently into SQLite (`datafuel.db`).

Run all six sweeps (timestamps are in UTC):

```bash
python sweep.py --as-of 2026-09-27T04:30:00Z
python sweep.py --as-of 2026-09-27T10:30:00Z
python sweep.py --as-of 2026-09-27T19:00:00Z
python sweep.py --as-of 2026-09-28T04:30:00Z
python sweep.py --as-of 2026-09-28T10:30:00Z
python sweep.py --as-of 2026-09-28T18:40:00Z
```

Each sweep runs in ~35–55 seconds and prints a summary:
- Sweep 1–4, 6: `26 stores: 26 complete, 0 incomplete · 36s`
- Sweep 5 (`2026-09-28T10:30:00Z`): `26 stores: 25 complete, 1 incomplete (DEL-004: portal returned partial snapshot) · 51s`

*Re-running any sweep is strictly idempotent and does not produce duplicate rows or data drift.*

---

## 5. Starting the Report API (`app.py`)

Start the FastAPI report server using Uvicorn:

```bash
uvicorn app:app --port 8000 --reload
```
*The API is available at `http://127.0.0.1:8000`.*

---

## 6. Querying the `/osa` Endpoint

The `/osa` endpoint computes the On-Shelf Availability (OSA) percentage for a city across all complete store sweeps conducted on an **Indian Standard Time (IST = UTC + 5:30)** calendar day.

### Example Queries

#### 1. Mumbai on 2026-09-28
```bash
curl "http://127.0.0.1:8000/osa?city=Mumbai&date=2026-09-28"
```
```json
{
  "city": "Mumbai",
  "date": "2026-09-28",
  "status": "ok",
  "osa_pct": 86.06,
  "observations": 753,
  "coverage": {
    "stores_expected": 9,
    "stores_complete": 9,
    "incomplete": []
  },
  "skus": [ ... ]
}
```

#### 2. Delhi on 2026-09-28 (Partial coverage due to DEL-004 snapshot)
```bash
curl "http://127.0.0.1:8000/osa?city=Delhi&date=2026-09-28"
```
```json
{
  "city": "Delhi",
  "date": "2026-09-28",
  "status": "partial",
  "osa_pct": 79.63,
  "observations": 761,
  "coverage": {
    "stores_expected": 9,
    "stores_complete": 8,
    "incomplete": [
      {
        "store_id": "DEL-004",
        "sweep": "2026-09-28T10:30:00Z",
        "reason": "portal returned partial snapshot"
      }
    ]
  },
  "skus": [ ... ]
}
```

#### 3. Bengaluru on 2026-09-28
```bash
curl "http://127.0.0.1:8000/osa?city=Bengaluru&date=2026-09-28"
```
```json
{
  "city": "Bengaluru",
  "date": "2026-09-28",
  "status": "ok",
  "osa_pct": 89.57,
  "observations": 690,
  "coverage": {
    "stores_expected": 8,
    "stores_complete": 8,
    "incomplete": []
  },
  "skus": [ ... ]
}
```

#### 4. Querying a date with no data (or omitting date)
```bash
curl "http://127.0.0.1:8000/osa?city=Mumbai&date=2026-10-04"
```
```json
{
  "city": "Mumbai",
  "date": "2026-10-04",
  "status": "no_data",
  "osa_pct": null,
  "observations": 0,
  "coverage": {
    "stores_expected": 9,
    "stores_complete": 0,
    "incomplete": []
  },
  "skus": []
}
```
*(Notice: Returns `osa_pct: null` and `status: "no_data"`, never 0%).*

#### 5. Validation Errors
```bash
# Invalid city returns HTTP 400:
curl -i "http://127.0.0.1:8000/osa?city=Kolkata"
# HTTP/1.1 400 Bad Request
# {"detail":"Invalid city 'Kolkata'. Must be one of: Bengaluru, Delhi, Mumbai"}

# Invalid date format returns HTTP 400:
curl -i "http://127.0.0.1:8000/osa?city=Mumbai&date=28-09-2026"
# HTTP/1.1 400 Bad Request
# {"detail":"Invalid date format '28-09-2026'. Expected YYYY-MM-DD."}
```

---

## 7. Running the Test Suite

Execute the test suite with pytest:

```bash
pytest -v
```

All 10 tests run in isolation using in-memory SQLite databases:
- `test_incomplete_store_is_excluded_from_osa_and_in_coverage`: Proves missing data is not turned into 0% stock-outs.
- `test_ghost_stock_counted_as_in_stock`: Proves `in_stock: true, qty: 0` is counted as in stock.
- `test_ist_calendar_day_mapping`: Proves 19:00 UTC on 27 Sep belongs to 28 Sep in IST.
- `test_no_data_returns_null_never_zero`: Proves no sweeps on a day returns null, not 0%.
- `test_city_osa_ratio_of_totals`: Proves city OSA uses aggregate ratio, not average of percentages.
- `test_renamed_sku_displays_latest_name`: Proves display name reflects latest rename.
- `test_api_validation_errors`: Proves HTTP 400 for bad city or malformed date.
- `test_timestamp_conversions`: Tests UTC / IST normalization and rejects naive timestamps.
- `test_duplicate_items_deduplicated`: Proves duplicate items across pagination pages are collapsed.
- `test_pacer_pacing_interval`: Verifies request pacer timing.

---

## 8. Verified Results for Submission (2026-09-28 IST)

| City | Expected Stores | Complete Stores | Incomplete Stores | Total Observations | Overall OSA % |
|---|---|---|---|---|---|
| **Mumbai** | 9 | 9 | 0 | 753 | **86.06%** |
| **Delhi** | 9 | 8 | 1 (`DEL-004` partial snapshot) | 761 | **79.63%** |
| **Bengaluru** | 8 | 8 | 0 | 690 | **89.57%** |
