"""Bounded discovery, then cheapest 40 distinct nearby restaurants by dish price."""
import math
import os
import secrets
import time
from comparison import data_of, menu_offers
from matching import exact_dish, query_names
from swiggy import AppError

SHORTLIST_SIZE = 40
# Hard ceilings override an old MAX_DISCOVERY_PAGES=2000 deployment setting.
MAX_PAGES = max(6, min(int(os.getenv("MAX_DISCOVERY_PAGES", "64")), 64))
MAX_MENU_PAGES = max(2, min(int(os.getenv("MAX_MENU_PAGES", "20")), 20))
MAX_METADATA_PAGES = 4
MAX_LOOKUP_PAGES = 2
MAX_SECONDS = 120
MENU_SECONDS = 55


def task_key(task):
    return (task["kind"], task["query"], task.get("rid", ""), str(task.get("offset", 0)))


def enqueue(run, task):
    key = task_key(task)
    if key not in run["scheduled"]:
        run["scheduled"].add(key)
        run["tasks"].append(task)


def new_search(dish, quantity, address_id):
    now = time.time()
    run = {"id": secrets.token_urlsafe(24), "dish": dish, "quantity": quantity,
           "address_id": address_id, "expires": now+1800, "hard_expires": now+14400,
           "started_at": time.monotonic(), "phase": "menus", "tasks": [], "scheduled": set(),
           "fingerprints": set(), "restaurants": {}, "raw_offers": {}, "offers": {},
           "candidates": [], "candidate_index": 0, "lookup": None,
           "bill_diagnostics": {}, "warnings": [], "pages": 0, "menu_pages": 0,
           "metadata_pages": 0, "done": False, "partial": False, "busy": False,
           "stop_reason": "", "mcp_session": None}
    for query in query_names(dish):
        enqueue(run, {"kind": "menu", "query": query, "offset": 0})
        enqueue(run, {"kind": "restaurants", "query": query, "offset": 0})
    return run


def issue(run, message):
    run["partial"] = True
    if message not in run["warnings"]:
        run["warnings"].append(message)


def restaurant_row(run, row):
    if not isinstance(row, dict) or not row.get("id"):
        return
    rid = str(row["id"])
    distance = row.get("distanceKm")
    distance = float(distance) if type(distance) in (int, float) and math.isfinite(distance) and distance >= 0 else None
    run["restaurants"][rid] = {"id": rid, "name": str(row.get("name") or "Swiggy restaurant"),
        "distance_km": distance, "availability": str(row.get("availabilityStatus", "")).upper()}
    # No per-restaurant menu expansion. Only shortlisted items are refreshed.


def page_cursor(run, task, data):
    more, cursor = data.get("hasMore"), data.get("nextOffset")
    if more is False:
        return None
    if cursor is None:
        if more is not False:
            issue(run, "Swiggy did not confirm the end of a search; the candidate pool may be incomplete.")
        return None
    if isinstance(cursor, str) and cursor.isdecimal():
        cursor = int(cursor)
    if type(cursor) is not int or cursor < 0:
        issue(run, "Swiggy returned an unsupported page cursor; that search was stopped.")
        return None
    if task_key({**task, "offset": cursor}) in run["scheduled"]:
        issue(run, "Swiggy repeated a page cursor; that search was stopped.")
        return None
    return cursor


def sort_key(row):
    return (row["item_total"], row["restaurant_id"], row["item_id"])


def priced_candidates(run):
    cheapest = {}
    for row in run["raw_offers"].values():
        if row["item_total"] is None:
            continue
        previous = cheapest.get(row["restaurant_id"])
        if previous is None or sort_key(row) < sort_key(previous):
            cheapest[row["restaurant_id"]] = row
    return sorted(cheapest.values(), key=sort_key)


def begin_distances(run):
    run["phase"] = "distances"
    if run["tasks"]:
        issue(run, "Dish discovery was capped; prices are ranked only among the collected matches.")
    run["tasks"].clear()
    run["candidates"] = priced_candidates(run)
    if any(row["item_total"] is None for row in run["raw_offers"].values()):
        issue(run, "Matches without an item price were excluded from the price shortlist.")


def finish(run, reason):
    run["done"] = True
    run["phase"] = "ready"
    run["stop_reason"] = reason
    run["tasks"].clear()
    run["lookup"] = None
    rows = sorted(run["offers"].values(), key=sort_key)[:SHORTLIST_SIZE]
    run["offers"] = {row["id"]: {**row, "shortlist_rank": i+1} for i, row in enumerate(rows)}


def select_known(run, *, final=False):
    """Advance in price order. Pause on an unresolved cheaper branch."""
    while run["candidate_index"] < len(run["candidates"]) and len(run["offers"]) < SHORTLIST_SIZE:
        row = run["candidates"][run["candidate_index"]]
        info = run["restaurants"].get(row["restaurant_id"])
        known = info and info["distance_km"] is not None and info["availability"] in {"OPEN", "CLOSED", "UNAVAILABLE"}
        if not known and not final:
            return row
        run["candidate_index"] += 1
        if not known:
            issue(run, "Some matches lacked confirmed distance or availability and were excluded.")
        elif info["distance_km"] < 7 and info["availability"] == "OPEN":
            run["offers"][row["id"]] = {**row, "distance_km": info["distance_km"], "restaurant": info["name"]}
    return None


def stop_at_budget(run):
    if run["phase"] == "menus":
        begin_distances(run)
    select_known(run, final=True)
    issue(run, "Discovery reached its time/page limit. Comparing the cheapest verified nearby matches collected so far.")
    finish(run, "discovery_limit")


def step(run, client):
    if run["done"]:
        return
    elapsed = time.monotonic() - run["started_at"]
    if run["pages"] >= MAX_PAGES or elapsed >= MAX_SECONDS:
        stop_at_budget(run)
        return
    if run["phase"] == "menus":
        remaining = [task for task in run["tasks"] if
                     (task["kind"] == "menu" and run["menu_pages"] < MAX_MENU_PAGES) or
                     (task["kind"] == "restaurants" and run["metadata_pages"] < MAX_METADATA_PAGES)]
        if len(remaining) < len(run["tasks"]):
            issue(run, "Discovery pages were capped; the shortlist covers collected results only.")
        run["tasks"] = remaining
        if not run["tasks"] or elapsed >= MENU_SECONDS:
            begin_distances(run)
        else:
            task = run["tasks"].pop(0)
            args = {"addressId": run["address_id"], "query": task["query"], "offset": task["offset"]}
            run["pages"] += 1
            run["menu_pages" if task["kind"] == "menu" else "metadata_pages"] += 1
            try:
                data = data_of(client.call("search_menu" if task["kind"] == "menu" else "search_restaurants", args))
            except AppError as exc:
                if exc.status in (401, 429):
                    run["tasks"].insert(0, task)
                    raise
                issue(run, "A discovery page failed; the candidate pool may be incomplete.")
                return
            rows = data.get("items" if task["kind"] == "menu" else "restaurants")
            if not isinstance(rows, list):
                issue(run, "A discovery response was not recognized; that search was stopped.")
                return
            ids = tuple(sorted(str(row.get("menu_item_id", row.get("id", ""))) + ":" + str(row.get("restaurant_id", "")) for row in rows if isinstance(row, dict)))
            fingerprint = (task["kind"], task["query"], ids)
            if fingerprint in run["fingerprints"]:
                issue(run, "Swiggy repeated the same results; that search was stopped.")
                return
            run["fingerprints"].add(fingerprint)
            if task["kind"] == "restaurants":
                for row in rows:
                    restaurant_row(run, row)
            else:
                for row in menu_offers(data, run["quantity"]):
                    if exact_dish(row["dish"], run["dish"]):
                        # A cross-restaurant offset is not a scoped-menu offset.
                        row.update(menu_query=row["dish"], menu_offset=0, needs_menu_detail=True,
                                   match_basis="Exact confirmed recipe with spelling normalization")
                        run["raw_offers"][row["id"]] = row
            cursor = page_cursor(run, task, data)
            if cursor is not None:
                enqueue(run, {**task, "offset": cursor})
            return
    missing = select_known(run)
    if missing is None:
        finish(run, "shortlist_ready" if len(run["offers"]) == SHORTLIST_SIZE else "pool_exhausted")
        return
    if run["lookup"] is None:
        run["lookup"] = {"kind": "lookup", "query": missing["restaurant"], "rid": missing["restaurant_id"], "offset": 0, "pages": 0}
    task = run["lookup"]
    run["scheduled"].add(task_key(task))
    run["pages"] += 1
    task["pages"] += 1
    try:
        data = data_of(client.call("search_restaurants", {"addressId": run["address_id"], "query": task["query"], "offset": task["offset"]}))
    except AppError as exc:
        if exc.status in (401, 429):
            raise
        data = {"restaurants": [], "hasMore": False}
    rows = data.get("restaurants")
    for row in rows if isinstance(rows, list) else []:
        restaurant_row(run, row)
    info = run["restaurants"].get(task["rid"])
    known = info and info["distance_km"] is not None and info["availability"] in {"OPEN", "CLOSED", "UNAVAILABLE"}
    cursor = page_cursor(run, task, data) if not known else None
    if not known and cursor is not None and task["pages"] < MAX_LOOKUP_PAGES:
        task["offset"] = cursor
        return
    if not known:
        issue(run, "Some matches lacked confirmed distance or availability and were excluded.")
        run["candidate_index"] += 1
    run["lookup"] = None
    if select_known(run) is None:
        finish(run, "shortlist_ready" if len(run["offers"]) == SHORTLIST_SIZE else "pool_exhausted")


def public_search(run):
    candidates = run["candidates"] if run["phase"] != "menus" else priced_candidates(run)
    nearby = [row for row in candidates if (info := run["restaurants"].get(row["restaurant_id"]))
              and info["distance_km"] is not None and info["distance_km"] < 7 and info["availability"] == "OPEN"]
    return {"search_id": run["id"], "done": run["done"], "partial": run["partial"], "phase": run["phase"],
            "pages_checked": run["pages"], "page_limit": MAX_PAGES, "seconds_limit": MAX_SECONDS,
            "elapsed_seconds": round(time.monotonic() - run["started_at"]),
            "pending_pages": len(run["tasks"]), "restaurants_seen": len(run["restaurants"]),
            "candidates_found": len(candidates), "restaurants_matched": len(nearby),
            "shortlist_limit": SHORTLIST_SIZE, "shortlisted": len(run["offers"]), "stop_reason": run["stop_reason"],
            "warnings": run["warnings"], "offers": list(run["offers"].values()) if run["done"] else [],
            "scope": "Up to 40 distinct restaurants below 7 km, selected by lowest dish price from the bounded search results. This is not an exhaustive search of all restaurants."}
