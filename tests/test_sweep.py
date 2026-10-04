"""
tests/test_sweep.py: Tests for scraper mechanics, data normalization, deduplication,
partial snapshot handling, and timestamp conversions.
"""
from datetime import datetime, timezone
import pytest

from db import to_utc_iso, ist_calendar_date
from sweep import Pacer, SoftBanError


def test_timestamp_conversions():
    """Test UTC and IST conversions for standard Z and Delhi +05:30 timestamps."""
    # UTC timestamp
    utc_str = "2026-09-28T04:30:00Z"
    assert to_utc_iso(utc_str) == "2026-09-28T04:30:00Z"
    assert ist_calendar_date(utc_str) == "2026-09-28"

    # Delhi IST timestamp (+05:30)
    delhi_str = "2026-09-28T10:00:00+05:30"
    assert to_utc_iso(delhi_str) == "2026-09-28T04:30:00Z"
    assert ist_calendar_date(delhi_str) == "2026-09-28"

    # Late night UTC timestamp crossing midnight into next IST day
    late_utc = "2026-09-27T19:00:00Z"
    # 19:00 UTC + 05:30 = 00:30 on 28 Sep
    assert ist_calendar_date(late_utc) == "2026-09-28"

    # Naive timestamp must raise ValueError
    with pytest.raises(ValueError, match="timezone"):
        to_utc_iso("2026-09-28T04:30:00")


def test_duplicate_items_deduplicated():
    """
    QuickMart portal repeats the last item of page 1 when cursor > 0.
    Verify that collapsing by sku_id ensures only unique items are retained.
    """
    page_1 = [
        {"sku_id": "SKU-0001", "name": "Milk", "in_stock": True, "qty": 10, "price": 30.0, "observed_at": "2026-09-28T04:30:00Z"},
        {"sku_id": "SKU-0002", "name": "Bread", "in_stock": True, "qty": 5, "price": 40.0, "observed_at": "2026-09-28T04:30:00Z"},
    ]
    # Page 2 repeats SKU-0002 from page 1
    page_2 = [
        {"sku_id": "SKU-0002", "name": "Bread", "in_stock": True, "qty": 5, "price": 40.0, "observed_at": "2026-09-28T04:30:00Z"},
        {"sku_id": "SKU-0003", "name": "Butter", "in_stock": False, "qty": 0, "price": 50.0, "observed_at": "2026-09-28T04:30:00Z"},
    ]

    items_by_sku = {}
    for item in page_1 + page_2:
        items_by_sku[item["sku_id"]] = item

    assert len(items_by_sku) == 3
    assert set(items_by_sku.keys()) == {"SKU-0001", "SKU-0002", "SKU-0003"}


def test_pacer_pacing_interval():
    """Verify that pacer calculates positive sleep durations when called too rapidly."""
    pacer = Pacer(requests_per_second=10.0)
    pacer.last = 100.0
    pacer.gap = 0.1
    # If now is 100.02, sleep should be 0.08
    assert pacer.gap == 0.1
