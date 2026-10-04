"""
app.py: FastAPI service providing the On-Shelf Availability (OSA) report API.

Endpoint:
    GET /osa?city=Mumbai&date=2026-09-28

Calculates on-shelf availability percentage for products in a given city on an IST calendar day,
excluding incomplete/degraded store sweeps and reporting data coverage metrics.
"""
from datetime import datetime
import os
import sqlite3
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse

from db import get_connection, yesterday_in_ist

app = FastAPI(title="DataFuel QuickMart OSA Report API")

ALLOWED_CITIES = {"Mumbai", "Delhi", "Bengaluru"}


def build_osa_report(
    city: str,
    target_date: str,
    conn: Optional[sqlite3.Connection] = None,
) -> Dict[str, Any]:
    """
    Build the OSA report for a given city and IST calendar date.
    Pure logic function that can be tested independently with any SQLite connection.
    """
    close_conn = False
    if conn is None:
        conn = get_connection()
        close_conn = True

    try:
        # 1. Get expected active stores in the city
        expected_rows = conn.execute(
            """
            SELECT store_id, name FROM stores
            WHERE city = ? AND is_active = 1
            ORDER BY store_id
            """,
            (city,),
        ).fetchall()
        expected_stores = [r["store_id"] for r in expected_rows]
        stores_expected_count = len(expected_stores)

        # 2. Get all sweeps conducted on this IST calendar day
        sweep_rows = conn.execute(
            """
            SELECT as_of FROM sweeps
            WHERE ist_date = ?
            ORDER BY as_of
            """,
            (target_date,),
        ).fetchall()
        sweep_as_ofs = [r["as_of"] for r in sweep_rows]

        # If no sweeps exist for this IST date, return no_data
        if not sweep_as_ofs:
            return {
                "city": city,
                "date": target_date,
                "status": "no_data",
                "osa_pct": None,
                "observations": 0,
                "coverage": {
                    "stores_expected": stores_expected_count,
                    "stores_complete": 0,
                    "incomplete": [],
                },
                "skus": [],
            }

        # 3. Analyze coverage: check every expected store in every sweep for this date
        incomplete_entries: List[Dict[str, str]] = []
        stores_with_issues = set()

        for sid in expected_stores:
            for sw_as_of in sweep_as_ofs:
                st_row = conn.execute(
                    """
                    SELECT status, reason FROM store_sweeps
                    WHERE as_of = ? AND store_id = ?
                    """,
                    (sw_as_of, sid),
                ).fetchone()

                if not st_row:
                    incomplete_entries.append({
                        "store_id": sid,
                        "sweep": sw_as_of,
                        "reason": "missing sweep record",
                    })
                    stores_with_issues.add(sid)
                elif st_row["status"] != "complete":
                    incomplete_entries.append({
                        "store_id": sid,
                        "sweep": sw_as_of,
                        "reason": st_row["reason"] or "incomplete snapshot",
                    })
                    stores_with_issues.add(sid)

        # Stores that had complete snapshots across all sweeps for this day
        stores_complete_count = stores_expected_count - len(stores_with_issues)

        # 4. Compute SKU observations and OSA percentages
        # We only count observations from COMPLETE store-sweeps
        sku_rows = conn.execute(
            """
            SELECT
                o.sku_id,
                COUNT(*) AS observations,
                SUM(o.in_stock) AS in_stock
            FROM observations o
            JOIN store_sweeps ss ON ss.as_of = o.as_of AND ss.store_id = o.store_id
            JOIN sweeps sw ON sw.as_of = o.as_of
            JOIN stores s ON s.store_id = o.store_id
            WHERE s.city = ?
              AND sw.ist_date = ?
              AND ss.status = 'complete'
            GROUP BY o.sku_id
            ORDER BY o.sku_id;
            """,
            (city, target_date),
        ).fetchall()

        # Retrieve the latest display name for each SKU (handles SKU renaming)
        latest_names_rows = conn.execute(
            """
            SELECT sku_id, name, MAX(as_of)
            FROM observations
            GROUP BY sku_id
            """
        ).fetchall()
        latest_name_by_sku = {r["sku_id"]: r["name"] for r in latest_names_rows}

        total_obs = 0
        total_in_stock = 0
        skus_list = []

        for row in sku_rows:
            sku_id = row["sku_id"]
            obs = row["observations"]
            inst = row["in_stock"] if row["in_stock"] is not None else 0
            sku_osa = round(100.0 * inst / obs, 2) if obs > 0 else 0.0

            total_obs += obs
            total_in_stock += inst

            skus_list.append({
                "sku_id": sku_id,
                "name": latest_name_by_sku.get(sku_id, sku_id),
                "observations": obs,
                "in_stock": inst,
                "osa_pct": sku_osa,
            })

        # City overall OSA = total in_stock observations / total observations * 100
        overall_osa = (
            round(100.0 * total_in_stock / total_obs, 2)
            if total_obs > 0
            else None
        )

        status = "ok" if not incomplete_entries else "partial"
        if total_obs == 0:
            status = "no_data"

        return {
            "city": city,
            "date": target_date,
            "status": status,
            "osa_pct": overall_osa,
            "observations": total_obs,
            "coverage": {
                "stores_expected": stores_expected_count,
                "stores_complete": max(0, stores_complete_count),
                "incomplete": incomplete_entries,
            },
            "skus": skus_list,
        }
    finally:
        if close_conn:
            conn.close()


@app.get("/osa")
def get_osa(
    city: str = Query(..., description="Target city: Mumbai, Delhi, or Bengaluru"),
    date: Optional[str] = Query(None, description="IST calendar day (YYYY-MM-DD). Defaults to yesterday in IST."),
):
    # Validate city
    if city not in ALLOWED_CITIES:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid city '{city}'. Must be one of: {', '.join(sorted(ALLOWED_CITIES))}",
        )

    # Validate date
    if date is not None:
        try:
            parsed = datetime.strptime(date, "%Y-%m-%d")
            target_date = parsed.strftime("%Y-%m-%d")
        except ValueError:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid date format '{date}'. Expected YYYY-MM-DD.",
            )
    else:
        target_date = yesterday_in_ist()

    report = build_osa_report(city, target_date)
    return JSONResponse(content=report)
