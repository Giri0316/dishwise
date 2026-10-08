"""Resumable discovery: one provider page per request, no five-restaurant cap."""
import math
import os
import secrets
import time
from comparison import data_of, menu_offers
from matching import exact_dish, query_names
from swiggy import AppError

MAX_PAGES = int(os.getenv("MAX_DISCOVERY_PAGES", "2000"))


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
           "tasks": [], "scheduled": set(), "restaurants": {}, "raw_offers": {},
           "offers": {}, "bill_diagnostics": {}, "warnings": [], "pages": 0, "done": False,
           "partial": False, "busy": False}
    for query in query_names(dish):
        enqueue(run, {"kind": "restaurants", "query": query, "offset": 0})
        enqueue(run, {"kind": "menu", "query": query, "offset": 0})
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
    distance = float(distance) if type(distance) in (int,float) and math.isfinite(distance) and distance >= 0 else None
    info = {"id": rid, "name": str(row.get("name") or "Swiggy restaurant"),
            "distance_km": distance, "availability": str(row.get("availabilityStatus", "")).upper()}
    run["restaurants"][rid] = info
    if distance is not None and distance < 7 and info["availability"] == "OPEN":
        for query in query_names(run["dish"]):
            enqueue(run, {"kind": "menu", "query": query, "rid": rid,
                          "name": info["name"], "offset": 0})


def next_page(run, task, data):
    more, cursor = data.get("hasMore"), data.get("nextOffset")
    if more is False:
        return
    if cursor is None:
        if more is True:
            issue(run, "Swiggy indicated more results but omitted the next page; coverage is incomplete.")
        elif more is not False:
            issue(run, "Swiggy omitted pagination completion; coverage cannot be confirmed.")
        return
    if isinstance(cursor, str) and cursor.isdecimal():
        cursor = int(cursor)
    if type(cursor) is not int or cursor < 0:
        issue(run, "Swiggy returned an unsupported pagination cursor; coverage is incomplete.")
        return
    following = {**task, "offset": cursor}
    if task_key(following) in run["scheduled"]:
        issue(run, "Swiggy repeated a page cursor; coverage is incomplete.")
        return
    enqueue(run, following)


def step(run, client):
    if run["done"]:
        return
    if run["pages"] >= MAX_PAGES:
        issue(run, "Discovery reached its configured page limit. Only the returned matches can be compared.")
        run["tasks"].clear()
    if not run["tasks"]:
        finish(run)
        return
    task = run["tasks"][0]
    args = {"addressId": run["address_id"], "query": task["query"], "offset": task["offset"]}
    if task.get("rid") and task["kind"] == "menu":
        args["restaurantIdOfAddedItem"] = task["rid"]
    try:
        data = data_of(client.call("search_menu" if task["kind"] == "menu" else "search_restaurants", args))
    except AppError as exc:
        if exc.status in (401, 429):
            raise
        issue(run, f"A search page failed for {task['query']}; coverage is incomplete.")
        run["tasks"].pop(0)
        run["pages"] += 1
        return
    run["tasks"].pop(0)
    run["pages"] += 1
    if task["kind"] in ("restaurants", "lookup"):
        rows = data.get("restaurants")
        if not isinstance(rows, list):
            issue(run, "Restaurant search did not return distance data in the expected format.")
        else:
            for row in rows:
                # Name lookups must resolve the exact restaurant ID, not another branch.
                if task["kind"] != "lookup" or (isinstance(row, dict) and str(row.get("id")) == task["rid"]):
                    restaurant_row(run, row)
    else:
        try:
            rows = menu_offers(data, run["quantity"], task.get("rid"), task.get("name"))
        except AppError:
            rows = []
            issue(run, "A menu page was not recognized; coverage is incomplete.")
        for row in rows:
            if not exact_dish(row["dish"], run["dish"]):
                continue
            row.update(menu_query=task["query"], menu_offset=task["offset"],
                       match_basis="Exact confirmed dish with spelling normalization")
            previous = run["raw_offers"].get(row["id"])
            if previous is None or task.get("rid"):
                run["raw_offers"][row["id"]] = row
            rid = row["restaurant_id"]
            if rid not in run["restaurants"]:
                enqueue(run, {"kind": "lookup", "query": row["restaurant"], "rid": rid, "offset": 0})
    next_page(run, task, data)
    if not run["tasks"]:
        finish(run)


def finish(run):
    run["done"] = True
    run["offers"] = {}
    for key, row in run["raw_offers"].items():
        info = run["restaurants"].get(row["restaurant_id"])
        if not info or info["distance_km"] is None:
            issue(run, "Some exact matches lack a confirmed distance and were excluded from the below-7-km comparison.")
            continue
        if info["availability"] not in {"OPEN", "CLOSED", "UNAVAILABLE"}:
            issue(run, "Some exact matches lack confirmed restaurant availability and were excluded.")
        if info["distance_km"] >= 7 or info["availability"] != "OPEN":
            continue
        run["offers"][key] = {**row, "distance_km": info["distance_km"], "restaurant": info["name"]}


def public_search(run):
    eligible_restaurants = {o["restaurant_id"] for o in run["offers"].values()}
    return {"search_id": run["id"], "done": run["done"], "partial": run["partial"],
            "pages_checked": run["pages"], "pending_pages": len(run["tasks"]),
            "restaurants_seen": len(run["restaurants"]), "restaurants_matched": len(eligible_restaurants),
            "warnings": run["warnings"], "offers": list(run["offers"].values()) if run["done"] else [],
            "scope": "All eligible exact matches returned by the searched Swiggy pages, with confirmed distance below 7 km. Provider search coverage may be incomplete."}
