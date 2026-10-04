"""
tests/test_osa.py: Tests for On-Shelf Availability (OSA) calculation and API endpoints.
Validates metric accuracy, ghost stock handling, incomplete store exclusion,
IST timezone day mapping, and API error handling.
"""
import sqlite3
import pytest
from fastapi import HTTPException

from app import app, build_osa_report, get_osa
from db import init_db, to_utc_iso, ist_calendar_date


@pytest.fixture
def mem_db():
    """Create an isolated in-memory SQLite database for testing."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    init_db(conn)
    yield conn
    conn.close()


def add_store(conn, store_id, city, name=None, is_active=1, is_serviceable=1):
    with conn:
        conn.execute(
            "INSERT INTO stores (store_id, city, name, is_active, is_serviceable) VALUES (?, ?, ?, ?, ?)",
            (store_id, city, name or f"Store {store_id}", is_active, is_serviceable),
        )


def add_sweep(conn, as_of_utc):
    canonical = to_utc_iso(as_of_utc)
    ist_date = ist_calendar_date(canonical)
    with conn:
        conn.execute(
            "INSERT INTO sweeps (as_of, ist_date, started_at, finished_at) VALUES (?, ?, ?, ?)",
            (canonical, ist_date, canonical, canonical),
        )
    return canonical


def add_store_sweep(conn, as_of, store_id, status="complete", reason=None, items=None):
    items = items or []
    with conn:
        conn.execute(
            "INSERT INTO store_sweeps (as_of, store_id, status, reason, item_count, fetched_at) VALUES (?, ?, ?, ?, ?, ?)",
            (as_of, store_id, status, reason, len(items), as_of),
        )
        if status == "complete":
            for sku_id, in_stock, name, qty, price in items:
                conn.execute(
                    """
                    INSERT INTO observations (as_of, store_id, sku_id, name, in_stock, qty, price, observed_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (as_of, store_id, sku_id, name, 1 if in_stock else 0, qty, price, as_of),
                )


def test_incomplete_store_is_excluded_from_osa_and_in_coverage(mem_db):
    """
    CRITICAL: Incomplete store sweeps must NOT be treated as 0% in-stock (stock-out).
    They must be excluded from observations and listed in coverage.incomplete.
    """
    add_store(mem_db, "MUM-001", "Mumbai", is_active=1)
    add_store(mem_db, "MUM-002", "Mumbai", is_active=1)
    as_of = add_sweep(mem_db, "2026-09-28T04:30:00Z")

    # MUM-001 is complete with 1 in-stock item
    add_store_sweep(
        mem_db, as_of, "MUM-001", status="complete",
        items=[("SKU-0001", True, "Product 1", 10, 100.0)]
    )
    # MUM-002 failed with partial data
    add_store_sweep(
        mem_db, as_of, "MUM-002", status="incomplete", reason="portal returned partial snapshot"
    )

    report = build_osa_report("Mumbai", "2026-09-28", conn=mem_db)

    # If MUM-002 was counted as 0%, OSA would be 50%. Since it's excluded, OSA is 100%.
    assert report["osa_pct"] == 100.0
    assert report["observations"] == 1
    assert report["coverage"]["stores_expected"] == 2
    assert report["coverage"]["stores_complete"] == 1
    assert len(report["coverage"]["incomplete"]) == 1
    assert report["coverage"]["incomplete"][0]["store_id"] == "MUM-002"
    assert "partial" in report["coverage"]["incomplete"][0]["reason"]


def test_ghost_stock_counted_as_in_stock(mem_db):
    """
    Verify ghost stock: items with in_stock=True but qty=0 are counted as IN STOCK.
    """
    add_store(mem_db, "MUM-001", "Mumbai", is_active=1)
    as_of = add_sweep(mem_db, "2026-09-28T04:30:00Z")

    # Item with in_stock=True but qty=0 (ghost stock)
    add_store_sweep(
        mem_db, as_of, "MUM-001", status="complete",
        items=[("SKU-0002", True, "Britannia Bread", 0, 40.0)]
    )

    report = build_osa_report("Mumbai", "2026-09-28", conn=mem_db)
    assert report["osa_pct"] == 100.0
    assert report["skus"][0]["in_stock"] == 1


def test_ist_calendar_day_mapping(mem_db):
    """
    19:00 UTC on 2026-09-27 is 00:30 on 2026-09-28 IST.
    It MUST belong to date 2026-09-28, not 2026-09-27.
    """
    add_store(mem_db, "DEL-001", "Delhi", is_active=1)
    as_of_27_night = add_sweep(mem_db, "2026-09-27T19:00:00Z")
    add_store_sweep(
        mem_db, as_of_27_night, "DEL-001", status="complete",
        items=[("SKU-0001", True, "Product 1", 5, 50.0)]
    )

    # Querying 2026-09-28 IST should find this observation
    report_28 = build_osa_report("Delhi", "2026-09-28", conn=mem_db)
    assert report_28["observations"] == 1
    assert report_28["osa_pct"] == 100.0

    # Querying 2026-09-27 IST should have NO data
    report_27 = build_osa_report("Delhi", "2026-09-27", conn=mem_db)
    assert report_27["status"] == "no_data"
    assert report_27["osa_pct"] is None


def test_no_data_returns_null_never_zero(mem_db):
    """
    When no sweeps exist for the requested day, return status='no_data' and osa_pct=None.
    Never return 0% for days with no data.
    """
    add_store(mem_db, "BLR-001", "Bengaluru", is_active=1)
    report = build_osa_report("Bengaluru", "2026-10-04", conn=mem_db)

    assert report["status"] == "no_data"
    assert report["osa_pct"] is None
    assert report["observations"] == 0
    assert report["skus"] == []


def test_city_osa_ratio_of_totals(mem_db):
    """
    City OSA must be total_in_stock / total_observations, NOT the average of percentages.
    Example:
      SKU 1: 1 in stock / 1 observation = 100%
      SKU 2: 0 in stock / 9 observations = 0%
      Total: 1 in stock / 10 observations = 10.00%
      Average of averages would mistakenly produce (100% + 0%) / 2 = 50.00%.
    """
    add_store(mem_db, "MUM-001", "Mumbai", is_active=1)
    as_of = add_sweep(mem_db, "2026-09-28T04:30:00Z")

    items = [("SKU-0001", True, "Item 1", 1, 10.0)]
    for i in range(9):
        items.append((f"SKU-{i+2:04d}", False, f"Item {i+2}", 0, 10.0))

    add_store_sweep(mem_db, as_of, "MUM-001", status="complete", items=items)

    report = build_osa_report("Mumbai", "2026-09-28", conn=mem_db)
    assert report["observations"] == 10
    # Expected: 1/10 = 10.0%, NOT 50.0%
    assert report["osa_pct"] == 10.0


def test_renamed_sku_displays_latest_name(mem_db):
    """
    When a product is renamed across sweeps, report must show the latest known name.
    """
    add_store(mem_db, "MUM-001", "Mumbai", is_active=1)
    sw1 = add_sweep(mem_db, "2026-09-27T04:30:00Z")
    sw2 = add_sweep(mem_db, "2026-09-27T10:30:00Z")

    # In sweep 1, name was old name
    add_store_sweep(
        mem_db, sw1, "MUM-001", status="complete",
        items=[("SKU-0001", True, "Amul Taaza Toned Milk 500ml", 5, 25.0)]
    )
    # In sweep 2, name was updated
    add_store_sweep(
        mem_db, sw2, "MUM-001", status="complete",
        items=[("SKU-0001", True, "Amul Taaza Toned Milk 500 ml Pouch", 5, 25.0)]
    )

    report = build_osa_report("Mumbai", "2026-09-27", conn=mem_db)
    assert report["skus"][0]["name"] == "Amul Taaza Toned Milk 500 ml Pouch"


def test_api_validation_errors():
    """Test HTTP 400 responses for invalid city and invalid date format."""
    # Invalid city
    with pytest.raises(HTTPException) as exc1:
        get_osa(city="Chennai", date="2026-09-28")
    assert exc1.value.status_code == 400
    assert "Invalid city" in exc1.value.detail

    # Invalid date
    with pytest.raises(HTTPException) as exc2:
        get_osa(city="Mumbai", date="28-09-2026")
    assert exc2.value.status_code == 400
    assert "Invalid date format" in exc2.value.detail
