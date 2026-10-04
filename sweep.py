#!/usr/bin/env python3
"""
sweep.py: QuickMart inventory scraper for DataFuel take-home.

Collects inventory snapshots for all tracked stores for a given UTC timestamp,
detects portal misbehavior (burst 429s, 500/503 errors, 8s slow delays,
soft bans with meta.source='edge', partial snapshots, duplicate SKUs across pages,
and timezone variations), writes to the database idempotently, and prints a summary.

Usage:
    python sweep.py --as-of 2026-09-28T04:30:00Z
"""
import argparse
import os
import sys
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import requests

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from db import (
    get_connection,
    init_db,
    ist_calendar_date,
    to_utc_iso,
)

DEFAULT_PORTAL = os.environ.get("PORTAL_URL", "http://127.0.0.1:8765")
DEFAULT_API_KEY = os.environ.get("API_KEY", "dfhire-2026")


class SoftBanError(Exception):
    """Raised when portal returns degraded data (meta.source != 'origin')."""
    pass


class Pacer:
    """Enforces a minimum interval between requests to avoid fair-use soft bans."""

    def __init__(self, requests_per_second: float = 2.0):
        self.gap = 1.0 / requests_per_second
        self.last = 0.0

    def wait(self) -> None:
        now = time.monotonic()
        sleep_duration = (self.last + self.gap) - now
        if sleep_duration > 0:
            time.sleep(sleep_duration)
        self.last = time.monotonic()


def safe_get(
    session: requests.Session,
    url: str,
    params: Optional[Dict[str, Any]] = None,
    headers: Optional[Dict[str, str]] = None,
    pacer: Optional[Pacer] = None,
    timeout: float = 5.0,
    max_attempts: int = 5,
    verbose: bool = False,
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """
    Perform an HTTP GET request with timeouts, rate limit handling, and bounded retries.
    Returns (json_body, error_reason).
    """
    last_reason = "unknown"
    for attempt in range(1, max_attempts + 1):
        if pacer:
            pacer.wait()
        try:
            resp = session.get(url, params=params, headers=headers, timeout=timeout)
        except requests.Timeout:
            last_reason = "request timed out (server slow delay)"
            if verbose:
                print(f"    [attempt {attempt}/{max_attempts}] Timeout on {url}, retrying...")
        except requests.RequestException as e:
            last_reason = f"network error: {type(e).__name__}"
            if verbose:
                print(f"    [attempt {attempt}/{max_attempts}] Network error: {e}, retrying...")
        else:
            if resp.status_code == 200:
                try:
                    return resp.json(), None
                except ValueError:
                    return None, "invalid json in response"

            if resp.status_code == 429:
                retry_after = float(resp.headers.get("Retry-After", 2))
                if verbose:
                    print(f"    [attempt {attempt}/{max_attempts}] Rate limited (429). Sleeping {retry_after}s...")
                time.sleep(retry_after + 0.2)
                last_reason = f"rate limited (429)"
                continue

            if resp.status_code in (400, 401, 404):
                try:
                    err_msg = resp.json().get("error", f"http {resp.status_code}")
                except Exception:
                    err_msg = f"http {resp.status_code}"
                return None, f"fatal client error: {err_msg}"

            if resp.status_code in (500, 503):
                last_reason = f"http {resp.status_code}"
                if verbose:
                    print(f"    [attempt {attempt}/{max_attempts}] Server error {resp.status_code}, retrying...")

        backoff = min(1.0 * (1.5 ** (attempt - 1)), 6.0)
        time.sleep(backoff)

    return None, f"gave up after {max_attempts} attempts ({last_reason})"


def fetch_all_stores(
    session: requests.Session,
    portal_url: str,
    api_key: str,
    pacer: Pacer,
    verbose: bool = False,
) -> List[Dict[str, Any]]:
    """Fetch the complete store roster across all pagination pages."""
    stores = []
    page = 1
    headers = {"X-Api-Key": api_key}
    while True:
        url = f"{portal_url}/v1/stores"
        data, err = safe_get(session, url, params={"page": page}, headers=headers, pacer=pacer, verbose=verbose)
        if err or not data:
            raise RuntimeError(f"Failed to fetch store roster on page {page}: {err}")
        page_stores = data.get("stores", [])
        stores.extend(page_stores)
        next_page = data.get("next_page")
        if not next_page:
            break
        page = next_page
    return stores


def fetch_store_inventory(
    session: requests.Session,
    portal_url: str,
    api_key: str,
    store_id: str,
    as_of: str,
    pacer: Pacer,
    verbose: bool = False,
) -> Tuple[str, Optional[str], List[Dict[str, Any]]]:
    """
    Fetch all inventory pages for a single store.
    Handles soft bans, partial flags, pagination, and duplicates across pages.
    Returns (status, reason, cleaned_items_list).
    Status is either 'complete' or 'incomplete'.
    """
    url = f"{portal_url}/v1/stores/{store_id}/inventory"
    headers = {"X-Api-Key": api_key}
    max_soft_ban_retries = 3

    for ban_retry in range(max_soft_ban_retries):
        cursor: Optional[str] = "0"
        items_by_sku: Dict[str, Dict[str, Any]] = {}
        is_partial = False
        saw_soft_ban = False
        page_count = 0
        max_pages = 50

        while cursor is not None and page_count < max_pages:
            page_count += 1
            params = {"as_of": as_of, "cursor": cursor}
            body, err = safe_get(session, url, params=params, headers=headers, pacer=pacer, verbose=verbose)
            if err:
                return "incomplete", err, []

            # Soft ban detection: meta.source != 'origin'
            meta = body.get("meta", {})
            source = meta.get("source")
            if source != "origin":
                saw_soft_ban = True
                print(f"  [!] Soft ban detected for store {store_id} (meta.source='{source}'). Cooling down 25s...")
                time.sleep(25.0)  # mock_portal ban is 20s, wait 25s to fully expire
                break

            # Partial snapshot detection
            if body.get("partial") is True:
                is_partial = True
                return "incomplete", "portal returned partial snapshot", []

            page_items = body.get("items", [])
            for it in page_items:
                # Deduplicate by sku_id across pages
                items_by_sku[it["sku_id"]] = it

            cursor = body.get("next_cursor")

        if saw_soft_ban:
            continue

        if page_count >= max_pages and cursor is not None:
            return "incomplete", "exceeded max pagination depth", []

        # Clean and normalize items
        cleaned_items = []
        for it in items_by_sku.values():
            try:
                price_val = float(it["price"])
            except (ValueError, TypeError):
                price_val = 0.0

            cleaned_items.append({
                "sku_id": it["sku_id"],
                "name": it["name"],
                "in_stock": 1 if bool(it["in_stock"]) else 0,
                "qty": int(it.get("qty", 0)),
                "price": price_val,
                "observed_at": to_utc_iso(it["observed_at"]),
            })

        return "complete", None, cleaned_items

    return "incomplete", "persistent soft ban from portal", []


def run_sweep(
    as_of_input: str,
    portal_url: str = DEFAULT_PORTAL,
    api_key: str = DEFAULT_API_KEY,
    db_path: Optional[str] = None,
    verbose: bool = False,
) -> Dict[str, Any]:
    """Execute a single sweep run for all tracked stores."""
    # 1. Normalize and validate timestamp
    try:
        as_of_utc = to_utc_iso(as_of_input)
    except Exception as e:
        print(f"Error: Invalid --as-of timestamp '{as_of_input}': {e}", file=sys.stderr)
        sys.exit(1)

    ist_date = ist_calendar_date(as_of_utc)
    started_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    conn = get_connection(db_path)
    init_db(conn)

    # Record or update sweep metadata
    with conn:
        conn.execute(
            """
            INSERT INTO sweeps (as_of, ist_date, started_at)
            VALUES (?, ?, ?)
            ON CONFLICT(as_of) DO UPDATE SET
                started_at = excluded.started_at;
            """,
            (as_of_utc, ist_date, started_at),
        )

    session = requests.Session()
    # Pace at ~2 requests per second to avoid fair-use soft bans
    pacer = Pacer(requests_per_second=2.0)

    start_mono = time.monotonic()
    if verbose:
        print(f"Starting sweep for as_of={as_of_utc} (IST date: {ist_date})...")

    # 2. Fetch and synchronize store list
    all_stores = fetch_all_stores(session, portal_url, api_key, pacer, verbose=verbose)
    with conn:
        for s in all_stores:
            conn.execute(
                """
                INSERT INTO stores (store_id, city, name, is_active, is_serviceable)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(store_id) DO UPDATE SET
                    city = excluded.city,
                    name = excluded.name,
                    is_active = excluded.is_active,
                    is_serviceable = excluded.is_serviceable;
                """,
                (s["store_id"], s["city"], s["name"], int(s["is_active"]), int(s["is_serviceable"])),
            )

    # 3. Decide which stores to track:
    # Track stores where is_active is True. Inactive (decommissioned) stores are excluded
    # even if serviceable flag is True (which is a decommissioned store data anomaly).
    # Active stores are tracked even if temporarily unserviceable (rain, maintenance, etc.).
    tracked_stores = [s for s in all_stores if s["is_active"]]

    complete_count = 0
    incomplete_list: List[Tuple[str, str]] = []

    # 4. Fetch inventory for each tracked store
    for store in tracked_stores:
        sid = store["store_id"]
        status, reason, items = fetch_store_inventory(
            session, portal_url, api_key, sid, as_of_utc, pacer, verbose=verbose
        )

        now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        # Save store results idempotently in a single transaction
        with conn:
            existing = conn.execute(
                "SELECT status FROM store_sweeps WHERE as_of = ? AND store_id = ?",
                (as_of_utc, sid),
            ).fetchone()

            # Rule: do not overwrite existing 'complete' data with 'incomplete' on a transient re-run failure
            if existing and existing["status"] == "complete" and status == "incomplete":
                if verbose:
                    print(f"  [{sid}] Retaining previous complete snapshot; skipping incomplete re-run ({reason})")
                complete_count += 1
                continue

            if status == "complete":
                conn.execute(
                    "DELETE FROM observations WHERE as_of = ? AND store_id = ?",
                    (as_of_utc, sid),
                )
                conn.execute(
                    """
                    INSERT INTO store_sweeps (as_of, store_id, status, reason, item_count, fetched_at)
                    VALUES (?, ?, ?, NULL, ?, ?)
                    ON CONFLICT(as_of, store_id) DO UPDATE SET
                        status = 'complete',
                        reason = NULL,
                        item_count = excluded.item_count,
                        fetched_at = excluded.fetched_at;
                    """,
                    (as_of_utc, sid, "complete", len(items), now_utc),
                )
                conn.executemany(
                    """
                    INSERT INTO observations (as_of, store_id, sku_id, name, in_stock, qty, price, observed_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (as_of_utc, sid, it["sku_id"], it["name"], it["in_stock"], it["qty"], it["price"], it["observed_at"])
                        for it in items
                    ],
                )
                complete_count += 1
            else:
                # Incomplete: do not store partial or degraded items
                conn.execute(
                    "DELETE FROM observations WHERE as_of = ? AND store_id = ?",
                    (as_of_utc, sid),
                )
                conn.execute(
                    """
                    INSERT INTO store_sweeps (as_of, store_id, status, reason, item_count, fetched_at)
                    VALUES (?, ?, 'incomplete', ?, 0, ?)
                    ON CONFLICT(as_of, store_id) DO UPDATE SET
                        status = 'incomplete',
                        reason = excluded.reason,
                        item_count = 0,
                        fetched_at = excluded.fetched_at;
                    """,
                    (as_of_utc, sid, reason, now_utc),
                )
                incomplete_list.append((sid, reason or "unknown"))

    finished_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with conn:
        conn.execute("UPDATE sweeps SET finished_at = ? WHERE as_of = ?", (finished_at, as_of_utc))

    elapsed_sec = int(time.monotonic() - start_mono)
    mins = elapsed_sec // 60
    secs = elapsed_sec % 60
    duration_str = f"{mins}m{secs:02d}s" if mins > 0 else f"{secs}s"

    # Format summary output
    total_tracked = len(tracked_stores)
    incomplete_count = len(incomplete_list)
    if incomplete_list:
        inc_details = ", ".join(f"{sid}: {r}" for sid, r in incomplete_list)
        summary = f"{total_tracked} stores: {complete_count} complete, {incomplete_count} incomplete ({inc_details}) · {duration_str}"
    else:
        summary = f"{total_tracked} stores: {complete_count} complete, 0 incomplete · {duration_str}"

    print(summary)
    return {
        "as_of": as_of_utc,
        "total_stores": total_tracked,
        "complete": complete_count,
        "incomplete": incomplete_list,
        "duration": duration_str,
    }


def main():
    parser = argparse.ArgumentParser(description="QuickMart Sweep Scraper")
    parser.add_argument("--as-of", required=True, help="Snapshot timestamp (e.g. 2026-09-28T04:30:00Z)")
    parser.add_argument("--portal", default=DEFAULT_PORTAL, help="QuickMart portal base URL")
    parser.add_argument("--api-key", default=DEFAULT_API_KEY, help="API key for QuickMart portal")
    parser.add_argument("--db", default=None, help="Path to SQLite database file")
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable verbose logging")
    args = parser.parse_args()

    run_sweep(
        as_of_input=args.as_of,
        portal_url=args.portal,
        api_key=args.api_key,
        db_path=args.db,
        verbose=args.verbose,
    )


if __name__ == "__main__":
    main()
