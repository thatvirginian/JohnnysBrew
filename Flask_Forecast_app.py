import io
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


# (Global Configuration Scope)
ROUTE_LOOK_AHEAD_MAP = {
    "North": {
        0: [0, 1],      # Mon export covers: Mon (0), Tue (1)
        1: [],           # Tue export restricted
        2: [2, 3],      # Wed export covers: Wed, Thu
        3: [],           # Thu export restricted
        4: [4, 5, 6],   # Fri export covers: Fri, Sat, Sun
        5: [],           # Sat export restricted
        6: [],           # Sun export restricted
    },
    "South": {
        0: [],           # Mon export restricted
        1: [1, 2],      # Tue export covers: Tue, Wed
        2: [],           # Wed export restricted
        3: [3, 4],      # Thu export covers: Thu, Fri
        4: [],           # Fri export restricted
        5: [5, 6, 0],   # Sat export covers: Sat, Sun, Next Mon
        6: [],           # Sun export restricted
    },
    "Default": {         # Fallback safety window
        0: [0, 1, 2], 1: [1, 2], 2: [2, 3], 3: [3, 4], 4: [4, 5], 5: [5, 6], 6: [6, 0]
    }
}


def run_query(query, params=None):
    engine = get_db_connection()
    with engine.connect() as conn:
        return pd.read_sql(text(query), conn, params=params)


# ─────────────────────────────────────────────
# CORE DATA GENERATION (Unified Grid Logic)
# ─────────────────────────────────────────────

def build_grid_dataset():
    start_date = datetime.now().date() + timedelta(days=1)
    end_date = start_date + timedelta(days=13)
    all_dates = [start_date + timedelta(days=n) for n in range(14)]

    # SCHEMA CHANGE NOTES:
    #   - orders_head: unchanged (order_guid, location_id, estimated_fulfillment_date, deleted)
    #   - order_checks: unchanged (order_guid, total_amount)
    #   - locations: unchanged (location_name, store_guid, route) — kept as-is per your config
    #
    # location_id is VARCHAR(64) in orders_head; cast ::uuid when joining to locations.store_guid (UUID type).

    query = """
        SELECT
            l.location_name                                                     AS "Location",
            l.store_guid                                                        AS "location_id",
            l.route                                                             AS "Route",
            (h.estimated_fulfillment_date AT TIME ZONE 'America/New_York')::date AS "Date",
            CASE
                WHEN extract(hour FROM (h.estimated_fulfillment_date AT TIME ZONE 'America/New_York')) < 13
                THEN 'AM' ELSE 'PM'
            END                                                                 AS "DayPart",
            count(DISTINCT h.order_guid)                                        AS "OrderCount",
            sum(sum(c.total_amount)) OVER (
                PARTITION BY
                    l.location_name,
                    (h.estimated_fulfillment_date AT TIME ZONE 'America/New_York')::date
            )                                                                   AS "DailyTotalRevenue"
        FROM orders_head h
        LEFT JOIN locations l
               ON h.location_id::uuid = l.store_guid
        JOIN order_checks c
               ON h.order_guid = c.order_guid
        WHERE (h.estimated_fulfillment_date AT TIME ZONE 'America/New_York')::date
              BETWEEN :start AND :end
          AND h.deleted = FALSE
          AND c.voided  = FALSE                          -- NEW: exclude voided checks
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

    _, _, loc_map, route_map, _ = build_grid_dataset()
    loc_id     = loc_map.get(loc_name)
    sel_route  = route_map.get(loc_name, "Default")
    sel_date   = datetime.strptime(date_str, "%Y-%m-%d").date()
    export_weekday = sel_date.weekday()

    route_rules    = ROUTE_LOOK_AHEAD_MAP.get(sel_route, ROUTE_LOOK_AHEAD_MAP["Default"])
    day_offsets    = route_rules.get(export_weekday, [])
    export_disabled = (len(day_offsets) == 0)

    params = {"loc_id": str(loc_id), "sel_date": sel_date}

    # SCHEMA CHANGE NOTES:
    #   - order_checks: customer_first / customer_last unchanged
    #   - order_items:  item_name / quantity unchanged; check_guid FK unchanged
    #   - item_modifiers: mod_name unchanged; selection_guid FK unchanged
    #   - dining_options: table still exists (guid / name unchanged)
    #   - location_id is VARCHAR(64) in orders_head; cast ::uuid when joining to locations.store_guid (UUID)

    detail_query = """
        WITH OrderTotals AS (
            SELECT
                h.order_guid,
                SUM(c.total_amount) AS true_order_total
            FROM orders_head h
            JOIN order_checks c ON h.order_guid = c.order_guid
            WHERE h.location_id::uuid = :loc_id
              AND (h.estimated_fulfillment_date AT TIME ZONE 'America/New_York')::date = :sel_date
              AND h.deleted  = FALSE
              AND c.voided   = FALSE                         -- NEW: exclude voided checks
            GROUP BY h.order_guid
        )
        SELECT
            h.order_guid,
            h.order_number,
            CONCAT_WS(' ', c.customer_first, c.customer_last)       AS customer_name,
            (h.estimated_fulfillment_date AT TIME ZONE 'America/New_York') AS local_time,
            oi.item_name,
            oi.quantity,
            STRING_AGG(DISTINCT im.mod_name, ', ')                   AS mods,
            ot.true_order_total                                      AS order_total,
            od.name                                                  AS dining_option
        FROM orders_head h
        JOIN order_checks c
               ON h.order_guid = c.order_guid
        JOIN order_items oi
               ON c.check_guid = oi.check_guid
        LEFT JOIN dining_options od
               ON h.dining_option_guid = od.guid             -- dining_options: unchanged
        LEFT JOIN item_modifiers im
               ON oi.selection_guid = im.selection_guid
        JOIN OrderTotals ot
               ON h.order_guid = ot.order_guid
        WHERE h.location_id::uuid = :loc_id
          AND (h.estimated_fulfillment_date AT TIME ZONE 'America/New_York')::date = :sel_date
          AND h.deleted  = FALSE
          AND c.voided   = FALSE                             -- NEW: exclude voided checks
          AND oi.voided  = FALSE                             -- NEW: exclude voided items
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

    _, _, loc_map, route_map, _ = build_grid_dataset()
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

    # ─────────────────────────────────────────────────────────────────
    # TODO: catering_pack_components has been removed from the new schema.
    #
    # Both queries below (store_prep_query and supply_detail_query) JOIN
    # on catering_pack_components to map order_items.item_guid → supply
    # items and quantities. You need to recreate or replace this table
    # before these export queries will work.
    #
    # The table needs at minimum:
    #   - item_guid      VARCHAR(64)   — matches order_items.item_guid
    #   - supply_id      VARCHAR / INT — unique supply item identifier
    #   - supply_name    VARCHAR       — human-readable supply label
    #   - supply_type    VARCHAR       — category (e.g. 'Box', 'Utensils')
    #   - quantity       NUMERIC       — units of supply per 1 ordered item
    #
    # Once recreated, the queries below should work without further changes.
    # ─────────────────────────────────────────────────────────────────

    # SCHEMA CHANGE NOTES (beyond the TODO above):
    #   - location_id: VARCHAR(64) in orders_head, cast ::uuid to match locations.store_guid (UUID)
    #   - order_checks → order_items FK path unchanged (order_guid → check_guid → selection_guid)
    #   - dining_options: unchanged
    #   - Added voided = FALSE filters on checks and items

    store_prep_query = """
        SELECT
            cpc.supply_id,
            cpc.supply_name                     AS "Supply Item",
            cpc.supply_type                     AS "Type",
            SUM(oi.quantity * cpc.quantity)     AS "Total Qty"
        FROM orders_head h
        JOIN order_checks oc
               ON h.order_guid = oc.order_guid
        JOIN order_items oi
               ON oc.check_guid = oi.check_guid
        JOIN catering_pack_components cpc       -- TODO: recreate this table (see note above)
               ON oi.item_guid = cpc.item_guid
        WHERE h.location_id::uuid = :loc_id
          AND (h.estimated_fulfillment_date AT TIME ZONE 'America/New_York')::date IN :target_dates
          AND h.deleted  = FALSE
          AND oc.voided  = FALSE                -- NEW: exclude voided checks
          AND oi.voided  = FALSE                -- NEW: exclude voided items
        GROUP BY 1, 2, 3
        ORDER BY 3, 2
    """

    supply_detail_query = """
        SELECT
            h.order_guid,
            h.order_number,
            CONCAT_WS(' ', c.customer_first, c.customer_last)       AS customer_name,
            (h.estimated_fulfillment_date AT TIME ZONE 'America/New_York') AS local_time,
            ot.true_order_total                                      AS order_total,
            od.name                                                  AS dining_option,
            cpc.supply_id,
            cpc.supply_name,
            cpc.supply_type,
            SUM(oi.quantity * cpc.quantity)                          AS units_needed
        FROM orders_head h
        JOIN order_checks c
               ON h.order_guid = c.order_guid
        JOIN order_items oi
               ON c.check_guid = oi.check_guid
        JOIN catering_pack_components cpc       -- TODO: recreate this table (see note above)
               ON oi.item_guid = cpc.item_guid
        LEFT JOIN dining_options od
               ON h.dining_option_guid = od.guid
        JOIN (
            SELECT order_guid, SUM(total_amount) AS true_order_total
            FROM order_checks
            WHERE voided = FALSE                 -- NEW: exclude voided checks from totals
            GROUP BY order_guid
        ) ot ON h.order_guid = ot.order_guid
        WHERE h.location_id::uuid = :loc_id
          AND (h.estimated_fulfillment_date AT TIME ZONE 'America/New_York')::date IN :target_dates
          AND h.deleted  = FALSE
          AND c.voided   = FALSE                -- NEW: exclude voided checks
          AND oi.voided  = FALSE                -- NEW: exclude voided items
        GROUP BY 1, 2, 3, 4, 5, 6, 7, 8, 9
        ORDER BY local_time ASC, h.order_number
    """

    store_prep_df = run_query(store_prep_query, params)
    supply_df     = run_query(supply_detail_query, params)

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
    app.run(host="0.0.0.0", port=8080)
