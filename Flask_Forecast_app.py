from datetime import datetime, timedelta
from flask import Flask, render_template, request, Response
import pandas as pd
from sqlalchemy import text
from src.database_setup import get_db_connection
from src.excel_helper import get_excel_download_buffer

app = Flask(__name__)


@app.template_filter('padding')
def padding_filter(s, width=3):
    return s.rjust(width)


# ─────────────────────────────────────────────
# ROUTE CONFIGURATION
# ─────────────────────────────────────────────

ROUTE_LOOK_AHEAD_MAP = {
    "North": {
        0: [0, 1],      # Mon export covers: Mon, Tue
        1: [],           # Tue restricted
        2: [2, 3],      # Wed export covers: Wed, Thu
        3: [],           # Thu restricted
        4: [4, 5, 6],   # Fri export covers: Fri, Sat, Sun
        5: [],           # Sat restricted
        6: [],           # Sun restricted
    },
    "South": {
        0: [],           # Mon restricted
        1: [1, 2],      # Tue export covers: Tue, Wed
        2: [],           # Wed restricted
        3: [3, 4],      # Thu export covers: Thu, Fri
        4: [],           # Fri restricted
        5: [5, 6, 0],   # Sat export covers: Sat, Sun, Next Mon
        6: [],           # Sun restricted
    },
    "Default": {
        0: [0, 1, 2], 1: [1, 2], 2: [2, 3], 3: [3, 4], 4: [4, 5], 5: [5, 6], 6: [6, 0]
    }
}


# ─────────────────────────────────────────────
# QUERY HELPER
# ─────────────────────────────────────────────

def run_query(query, params=None):
    engine = get_db_connection()
    with engine.connect() as conn:
        return pd.read_sql(text(query), conn, params=params)


# ─────────────────────────────────────────────
# FIX 1: Lightweight location lookup
# drill_down and export_excel use this instead
# of calling the full build_grid_dataset() just
# to get loc_id and route.
# ─────────────────────────────────────────────

def get_location_maps():
    """Returns (loc_map, route_map) from the locations table directly."""
    df = run_query("SELECT location_name, store_guid, route FROM locations")
    loc_map   = dict(zip(df["location_name"], df["store_guid"]))
    route_map = dict(zip(df["location_name"], df["route"]))
    return loc_map, route_map


# ─────────────────────────────────────────────
# GRID DATASET (index page only)
# ─────────────────────────────────────────────

def build_grid_dataset():
    start_date = datetime.now().date() + timedelta(days=1)
    end_date   = start_date + timedelta(days=13)
    all_dates  = [start_date + timedelta(days=n) for n in range(14)]

    query = """
        SELECT
            l.location_name                                                      AS "Location",
            l.store_guid                                                         AS "location_id",
            l.route                                                              AS "Route",
            (h.estimated_fulfillment_date AT TIME ZONE 'America/New_York')::date AS "Date",
            CASE
                WHEN extract(hour FROM (h.estimated_fulfillment_date AT TIME ZONE 'America/New_York')) < 13
                THEN 'AM' ELSE 'PM'
            END                                                                  AS "DayPart",
            count(DISTINCT h.order_guid)                                         AS "OrderCount",
            sum(sum(c.total_amount)) OVER (
                PARTITION BY
                    l.location_name,
                    (h.estimated_fulfillment_date AT TIME ZONE 'America/New_York')::date
            )                                                                    AS "DailyTotalRevenue"
        FROM orders_head h
        LEFT JOIN locations l
               ON h.location_id::uuid = l.store_guid
        JOIN order_checks c
               ON h.order_guid = c.order_guid
        WHERE (h.estimated_fulfillment_date AT TIME ZONE 'America/New_York')::date
              BETWEEN :start AND :end
          AND h.deleted = FALSE
          AND c.voided  = FALSE
        GROUP BY 1, 2, 3, 4, 5
    """

    df = run_query(query, params={"start": start_date, "end": end_date})

    if df.empty:
        return [], [], {}, {}, {}

    rev_map   = df.groupby(["Location", "Date"])["DailyTotalRevenue"].first().to_dict()
    route_map = dict(zip(df["Location"], df["Route"]))
    loc_map   = dict(zip(df["Location"], df["location_id"]))

    pivot = df.pivot_table(
        index="Location",
        columns=["Date", "DayPart"],
        values="OrderCount",
        aggfunc="sum"
    ).fillna(0).astype(int)

    am = pivot.xs("AM", axis=1, level="DayPart") if "AM" in pivot.columns.get_level_values("DayPart") \
        else pd.DataFrame(0, index=pivot.index, columns=all_dates)
    pm = pivot.xs("PM", axis=1, level="DayPart") if "PM" in pivot.columns.get_level_values("DayPart") \
        else pd.DataFrame(0, index=pivot.index, columns=all_dates)

    am = am.reindex(columns=all_dates, fill_value=0)
    pm = pm.reindex(columns=all_dates, fill_value=0)

    matrix = []
    for loc in pivot.index:
        row_cells = []
        for dt in all_dates:
            a_count = int(am.at[loc, dt])
            p_count = int(pm.at[loc, dt])
            revenue = rev_map.get((loc, dt), 0.0)
            row_cells.append({
                "date":     dt,
                "date_str": dt.strftime("%Y-%m-%d"),
                "is_empty": (a_count == 0 and p_count == 0),
                "am":       a_count,
                "pm":       p_count,
                "rev":      revenue,
                "is_gold":  revenue >= 1000
            })
        matrix.append({"location": loc, "cells": row_cells})

    daily_totals = [sum(rev_map.get((loc, dt), 0) for loc in pivot.index) for dt in all_dates]

    return all_dates, matrix, loc_map, route_map, daily_totals


# ─────────────────────────────────────────────
# ROUTES
# ─────────────────────────────────────────────

@app.route("/")
def index():
    all_dates, matrix, _, _, daily_totals = build_grid_dataset()
    return render_template("index.html", all_dates=all_dates, matrix=matrix, daily_totals=daily_totals)


@app.route("/drill-down")
def drill_down():
    loc_name = request.args.get("location")
    date_str = request.args.get("date")

    # FIX 1: lightweight lookup instead of full grid query
    loc_map, route_map = get_location_maps()
    loc_id     = loc_map.get(loc_name)
    sel_route  = route_map.get(loc_name, "Default")
    sel_date   = datetime.strptime(date_str, "%Y-%m-%d").date()
    export_weekday = sel_date.weekday()

    route_rules     = ROUTE_LOOK_AHEAD_MAP.get(sel_route, ROUTE_LOOK_AHEAD_MAP["Default"])
    day_offsets     = route_rules.get(export_weekday, [])
    export_disabled = (len(day_offsets) == 0)

    params = {"loc_id": str(loc_id), "sel_date": sel_date}

    detail_query = """
        WITH OrderTotals AS (
            SELECT
                h.order_guid,
                SUM(c.total_amount) AS true_order_total
            FROM orders_head h
            JOIN order_checks c ON h.order_guid = c.order_guid
            WHERE h.location_id::uuid = :loc_id
              AND (h.estimated_fulfillment_date AT TIME ZONE 'America/New_York')::date = :sel_date
              AND h.deleted = FALSE
              AND c.voided  = FALSE
            GROUP BY h.order_guid
        )
        SELECT
            h.order_guid,
            h.order_number,
            CONCAT_WS(' ', c.customer_first, c.customer_last) AS customer_name,
            (h.estimated_fulfillment_date AT TIME ZONE 'America/New_York') AS local_time,
            oi.item_name,
            oi.quantity,
            STRING_AGG(DISTINCT im.mod_name, ', ')             AS mods,
            ot.true_order_total                                AS order_total,
            od.name                                            AS dining_option
        FROM orders_head h
        JOIN order_checks c
               ON h.order_guid = c.order_guid
        JOIN order_items oi
               ON c.check_guid = oi.check_guid
        LEFT JOIN dining_options od
               ON h.dining_option_guid::uuid = od.guid
        LEFT JOIN item_modifiers im
               ON oi.selection_guid = im.selection_guid
        JOIN OrderTotals ot
               ON h.order_guid = ot.order_guid
        WHERE h.location_id::uuid = :loc_id
          AND (h.estimated_fulfillment_date AT TIME ZONE 'America/New_York')::date = :sel_date
          AND h.deleted = FALSE
          AND c.voided  = FALSE
          AND oi.voided = FALSE
        GROUP BY 1, 2, 3, 4, 5, 6, 8, 9
        ORDER BY local_time ASC
    """

    full_df = run_query(detail_query, params)

    bb_badges, taco_badges = [], []
    orders = {}

    if not full_df.empty:
        bb_mask = full_df["item_name"].str.contains("BB", case=False, na=False)
        if bb_mask.any():
            bb_badges = full_df[bb_mask].groupby("item_name")["quantity"].sum().reset_index().to_dict(orient="records")

        taco_mask = full_df["item_name"].str.contains("Taco Bar", case=False, na=False)
        if taco_mask.any():
            taco_badges = full_df[taco_mask].groupby("item_name")["quantity"].sum().reset_index().to_dict(orient="records")

        for order_id, group in full_df.groupby("order_guid", sort=False):
            orders[order_id] = {
                "number":      group["order_number"].iloc[0],
                "customer":    (group["customer_name"].iloc[0] or "").strip() or "NO NAME",
                "time":        group["local_time"].iloc[0].strftime("%I:%M %p"),
                "total":       group["order_total"].iloc[0],
                "is_high":     group["order_total"].iloc[0] >= 2000,
                "is_delivery": group["item_name"].str.contains("delivery", case=False).any(),
                "items":       group[["quantity", "item_name", "mods"]].to_dict(orient="records")
            }

    return render_template(
        "partials/drill_details.html",
        location=loc_name, date=date_str, route=sel_route,
        bb_badges=bb_badges, taco_badges=taco_badges, orders=orders,
        export_disabled=export_disabled,
        day_name=sel_date.strftime('%A')
    )


@app.route("/export")
def export_excel():
    loc_name = request.args.get("location")
    date_str = request.args.get("date")

    # FIX 1: lightweight lookup instead of full grid query
    loc_map, route_map = get_location_maps()
    loc_id       = loc_map.get(loc_name)
    sel_route    = route_map.get(loc_name, "Default")
    clicked_date = datetime.strptime(date_str, "%Y-%m-%d").date()
    export_weekday = clicked_date.weekday()

    route_rules = ROUTE_LOOK_AHEAD_MAP.get(sel_route, ROUTE_LOOK_AHEAD_MAP["Default"])
    day_offsets = route_rules.get(export_weekday, [])

    if not day_offsets:
        return Response(
            f"Downloads are restricted for {loc_name} ({sel_route} Route) on {clicked_date.strftime('%A')}s.",
            status=403
        )

    target_dates = []
    for offset in day_offsets:
        days_to_add = offset - export_weekday
        if days_to_add < 0:
            days_to_add += 7
        target_dates.append(clicked_date + timedelta(days=days_to_add))

    target_date_strs = [d.strftime("%Y-%m-%d") for d in target_dates]
    params = {"loc_id": str(loc_id), "target_dates": tuple(target_date_strs)}

    # FIX 2 + 3: single query replacing the two sequential queries.
    # The order_totals CTE is now scoped to the same location and dates
    # as the outer query (fixes the unscoped subquery scan).
    # Both store_prep and supply_detail data are returned together
    # and split in Python (eliminates the second DB round trip).
    combined_query = """
        WITH OrderTotals AS (
            SELECT
                h.order_guid,
                SUM(c.total_amount) AS true_order_total
            FROM orders_head h
            JOIN order_checks c ON h.order_guid = c.order_guid
            WHERE h.location_id::uuid = :loc_id
              AND (h.estimated_fulfillment_date AT TIME ZONE 'America/New_York')::date IN :target_dates
              AND h.deleted = FALSE
              AND c.voided  = FALSE
            GROUP BY h.order_guid
        )
        SELECT
            h.order_guid,
            h.order_number,
            CONCAT_WS(' ', c.customer_first, c.customer_last) AS customer_name,
            (h.estimated_fulfillment_date AT TIME ZONE 'America/New_York') AS local_time,
            ot.true_order_total                                AS order_total,
            od.name                                            AS dining_option,
            cpc.supply_id,
            cpc.supply_name,
            cpc.supply_type,
            oi.quantity                                        AS item_qty,
            cpc.quantity                                       AS supply_qty,
            (oi.quantity * cpc.quantity)                       AS units_needed
        FROM orders_head h
        JOIN order_checks c
               ON h.order_guid = c.order_guid
        JOIN order_items oi
               ON c.check_guid = oi.check_guid
        JOIN catering_pack_components cpc
               ON oi.item_guid::uuid = cpc.item_guid
        LEFT JOIN dining_options od
               ON h.dining_option_guid::uuid = od.guid
        JOIN OrderTotals ot
               ON h.order_guid = ot.order_guid
        WHERE h.location_id::uuid = :loc_id
          AND (h.estimated_fulfillment_date AT TIME ZONE 'America/New_York')::date IN :target_dates
          AND h.deleted = FALSE
          AND c.voided  = FALSE
          AND oi.voided = FALSE
        ORDER BY local_time ASC, h.order_number
    """

    full_df = run_query(combined_query, params)

    # Split into the two dataframes get_excel_download_buffer expects
    if not full_df.empty:
        store_prep_df = (
            full_df.groupby(["supply_id", "supply_name", "supply_type"], as_index=False)
            ["units_needed"].sum()
            .rename(columns={"supply_name": "Supply Item", "supply_type": "Type", "units_needed": "Total Qty"})
            .sort_values(["Type", "Supply Item"])
        )
        supply_df = full_df[[
            "order_guid", "order_number", "customer_name", "local_time",
            "order_total", "dining_option", "supply_id", "supply_name",
            "supply_type", "units_needed"
        ]].copy()
    else:
        store_prep_df = pd.DataFrame(columns=["supply_id", "Supply Item", "Type", "Total Qty"])
        supply_df     = pd.DataFrame(columns=[
            "order_guid", "order_number", "customer_name", "local_time",
            "order_total", "dining_option", "supply_id", "supply_name",
            "supply_type", "units_needed"
        ])

    range_label = (
        f"{target_dates[0].strftime('%m.%d')}-{target_dates[-1].strftime('%m.%d')}"
        if len(target_dates) > 1
        else target_dates[0].strftime('%m.%d')
    )

    excel_bin = get_excel_download_buffer(
        store_prep_df, supply_df,
        sheet_name=f"{loc_name[:5]}_{range_label}",
        location_name=loc_name, route=sel_route, report_date=clicked_date
    )

    return Response(
        excel_bin,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename=PREP_{loc_name}_{date_str}_BATCHED.xlsx"}
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000)
