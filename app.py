"""Run locally with: python app.py. For a public deployment set APP_URL (see README)."""
import base64
import hashlib
import logging
import os
import secrets
import threading
import time
from pathlib import Path
from urllib.parse import urlencode, urlparse

from dotenv import load_dotenv
from flask import Flask, jsonify, redirect, render_template, request, session
from werkzeug.exceptions import HTTPException
from werkzeug.middleware.proxy_fix import ProxyFix
from comparison import check_address, data_of, menu_offers, quote_offer
from swiggy import AppError, BASE_URL, SwiggyClient, auth_post
from matching import exact_dish
from ai_dishes import ai_ready, suggest_dishes

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")
from discovery import new_search, step as discovery_step, public_search
PORT = int(os.getenv("PORT", "8765"))
if not 1024 <= PORT <= 65535:
    raise ValueError("PORT must be between 1024 and 65535.")
# APP_URL is the public https address of a deployed copy, e.g. https://dishwise.onrender.com.
# When it is empty the app runs in local mode exactly as before (localhost only).
# On Render, RENDER_EXTERNAL_URL is filled in automatically, so APP_URL is only needed for a custom domain.
APP_URL = (os.getenv("APP_URL") or os.getenv("RENDER_EXTERNAL_URL") or "").strip().rstrip("/")
PRODUCTION = bool(APP_URL)
if PRODUCTION:
    parts = urlparse(APP_URL)
    if parts.scheme != "https" or not parts.hostname or parts.path or parts.query:
        raise ValueError("APP_URL must look like https://your-domain.com (https, no path).")
    if len(os.getenv("SECRET_KEY", "")) < 32:
        raise ValueError("Set SECRET_KEY to a random value of at least 32 characters in production.")
    ORIGIN = APP_URL
    ALLOWED_HOSTS = [parts.hostname]
else:
    ORIGIN = f"http://localhost:{PORT}"
    ALLOWED_HOSTS = ["localhost", "127.0.0.1"]
REDIRECT_URI = ORIGIN + "/auth/callback"

app = Flask(__name__)
app.config.update(SECRET_KEY=os.getenv("SECRET_KEY") or secrets.token_hex(32), MAX_CONTENT_LENGTH=32_768,
    SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax", SESSION_COOKIE_SECURE=PRODUCTION,
    SESSION_COOKIE_NAME="dishwise" if PRODUCTION else "dishwise_local", TRUSTED_HOSTS=ALLOWED_HOSTS)
if PRODUCTION:  # Hosting platforms terminate HTTPS at a proxy; trust exactly one proxy hop.
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
# Tokens, temporary results and address IDs are memory-only and disappear on exit.
states = {}
state_lock = threading.RLock()
cart_lock = threading.Lock()
client_factory = SwiggyClient


def state():
    with state_lock:
        sid = session.get("sid")
        if len(states) > 500:  # drop expired anonymous/old sessions so memory cannot grow forever
            for key in [k for k, v in states.items() if v.get("expires", 0) <= time.time() and k != sid][:250]:
                states.pop(key, None)
        if sid not in states:
            sid = secrets.token_urlsafe(32)
            session["sid"] = sid
            states[sid] = {"csrf": secrets.token_urlsafe(32), "expires": 0}
        return states[sid]


def token():
    current = state()
    if current.get("expires", 0) <= time.time() or not current.get("token"):
        current.pop("token", None)
        raise AppError("Click Connect Swiggy and sign in to load live prices.", 401)
    return current["token"]


def body():
    value = request.get_json(silent=True)
    if not isinstance(value, dict):
        raise AppError("Send a valid JSON request.")
    return value


def text(value, label, minimum=1, maximum=180):
    if not isinstance(value, str) or not minimum <= len(value.strip()) <= maximum:
        raise AppError(f"Please enter a valid {label}.")
    return value.strip()


def quantity(value):
    if type(value) is not int or not 1 <= value <= 10:
        raise AppError("Quantity must be a whole number from 1 to 10.")
    return value


@app.before_request
def protect_local_app():
    if not PRODUCTION and request.remote_addr not in {"127.0.0.1", "::1", None}:
        raise AppError("This app is available only on your own computer.", 403)
    if request.method == "POST":
        if request.headers.get("Origin") != ORIGIN:
            raise AppError(f"Open {ORIGIN} to use the app.", 403)
        supplied = request.headers.get("X-CSRF-Token", "")
        if not supplied or not secrets.compare_digest(supplied, state()["csrf"]):
            raise AppError("Refresh the page before trying again.", 403)


@app.after_request
def response_headers(response):
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    return response


@app.errorhandler(AppError)
def known_error(error):
    return jsonify(error=str(error)), error.status


@app.errorhandler(Exception)
def unknown_error(error):
    if isinstance(error, HTTPException):
        return jsonify(error=error.description), error.code
    # Avoid logging provider payloads, phone numbers, tokens or OAuth codes.
    return jsonify(error="The request could not finish. If a bill check was running, review your Swiggy cart."), 500


@app.get("/")
def index():
    if request.host != urlparse(ORIGIN).netloc:
        return redirect(ORIGIN)
    return render_template("index.html")


@app.get("/healthz")
def healthz():
    return "ok"


@app.get("/api/config")
def config():
    current = state()
    return jsonify(csrf=current["csrf"], ai_enabled=ai_ready(), connected=bool(current.get("token") and current.get("expires", 0) > time.time()))


@app.post("/api/auth/start")
def auth_start():
    registration = auth_post("/auth/register", {"client_name": "Dishwise" if PRODUCTION else "Dishwise Local", "redirect_uris": [REDIRECT_URI],
        "grant_types": ["authorization_code"], "response_types": ["code"], "token_endpoint_auth_method": "none"})
    client_id = registration.get("client_id")
    if not isinstance(client_id, str) or not client_id:
        raise AppError("Swiggy did not register the callback URL. See README troubleshooting.", 502)
    verifier, nonce = secrets.token_urlsafe(48), secrets.token_urlsafe(32)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state()["oauth"] = {"state": nonce, "verifier": verifier, "client_id": client_id, "expires": time.time() + 600}
    query = urlencode({"response_type": "code", "client_id": client_id, "redirect_uri": REDIRECT_URI,
        "code_challenge": challenge, "code_challenge_method": "S256", "state": nonce, "scope": "mcp:tools"})
    return jsonify(url=BASE_URL + "/auth/authorize?" + query)


@app.get("/auth/callback")
def auth_callback():
    current = state()
    pending = current.pop("oauth", {})
    nonce, code = request.args.get("state", ""), request.args.get("code", "")
    if (not pending or not nonce or not code or request.args.get("error")
        or not secrets.compare_digest(pending.get("state", ""), nonce) or pending.get("expires", 0) <= time.time()):
        return redirect(ORIGIN + "/?auth_error=1")
    try:
        result = auth_post("/auth/token", {"grant_type": "authorization_code", "code": code,
            "code_verifier": pending["verifier"], "client_id": pending["client_id"], "redirect_uri": REDIRECT_URI})
        access, expires = result.get("access_token"), result.get("expires_in")
        if not isinstance(access, str) or type(expires) not in (int, float) or not 0 < expires <= 10_000_000:
            raise AppError("Invalid login response.")
        current["token"], current["expires"] = access, time.time() + min(expires, 432000)
        current.pop("search", None)
        return redirect(ORIGIN + "/?connected=1")
    except AppError:
        return redirect(ORIGIN + "/?auth_error=1")


@app.post("/api/auth/disconnect")
def disconnect():
    if not cart_lock.acquire(blocking=False):
        raise AppError("Wait for the current bill check and cleanup to finish.", 409)
    try:
        current = state()
        access = current.pop("token", None)
        current.pop("search", None)
        current["expires"] = 0
        revoked = True
        if access:
            try:
                auth_post("/auth/logout", token=access)
            except AppError:
                revoked = False
        return jsonify(disconnected=True, revoked=revoked)
    finally:
        cart_lock.release()


@app.get("/api/addresses")
def addresses():
    page = request.args.get("page", 1, type=int)
    if not 1 <= page <= 20:
        raise AppError("Invalid address page.")
    with client_factory(token()) as client:
        data = data_of(client.call("get_addresses", {"page": page, "pageSize": 10}))
        if not isinstance(data.get("addresses"), list):
            raise AppError("Saved addresses were not returned in the expected format.", 502)
        return jsonify(addresses=[{"id": row["id"], "label": row.get("addressLine", "Saved address"),
            "tag": row.get("addressTag") or row.get("addressCategory") or ""} for row in data["addresses"] if row.get("id")],
            has_more=bool((data.get("pagination") or {}).get("hasMore")))


@app.post("/api/dish/suggest")
def dish_suggest():
    token()
    data = body()
    dish = text(data.get("dish"), "dish", 1, 100)
    current = state()
    if current.get("suggest_after", 0) > time.time():
        raise AppError("Wait a few seconds before another dish suggestion.", 429)
    current["suggest_after"] = time.time() + 3
    return jsonify(suggest_dishes(dish))


def search_run(search_id):
    current = state().get("search", {})
    if (not search_id or current.get("id") != search_id
            or current.get("expires", 0) <= time.time()
            or current.get("hard_expires", 0) <= time.time()):
        raise AppError("This search expired or was replaced. Find your deal again.", 409)
    current["expires"] = min(time.time() + 1800, current["hard_expires"])
    return current


@app.post("/api/search")
def search():
    data = body()
    if data.get("mode") == "demo":
        raise AppError("Only live Swiggy searches are available.")
    dish = text(data.get("confirmed_dish"), "confirmed exact dish name", 1, 100)
    count = quantity(data.get("quantity"))
    address_id = text(data.get("address_id"), "saved delivery address")
    if not cart_lock.acquire(blocking=False):
        raise AppError("Wait for the current bill check to finish.", 409)
    try:
        with client_factory(token()) as client:
            check_address(client, address_id)
        run = new_search(dish, count, address_id)
        state()["search"] = run
        return jsonify(public_search(run))
    finally:
        cart_lock.release()


@app.post("/api/search/next")
def search_next():
    access = token()
    current = search_run(body().get("search_id"))
    with state_lock:
        if current["busy"]:
            raise AppError("A search page is already loading.", 409)
        current["busy"] = True
    try:
        with client_factory(access) as client:
            discovery_step(current, client)
        return jsonify(public_search(current))
    finally:
        with state_lock:
            current["busy"] = False


@app.post("/api/quote")
def quote():
    data = body()
    if data.get("consent") is not True:
        raise AppError("Confirm temporary cart use before checking bills.")
    current = search_run(data.get("search_id"))
    if not current.get("done"):
        raise AppError("Finish restaurant discovery before checking bills.", 409)
    offer = current.get("offers", {}).get(data.get("offer_id"))
    if not offer or current.get("expires", 0) <= time.time():
        raise AppError("Search again before checking this bill.", 409)
    if not exact_dish(offer["dish"], current["dish"]) or not 0 <= offer.get("distance_km", 999) < 7:
        raise AppError("This item does not match the confirmed dish and distance filter.", 409)
    if offer["customized"]:
        raise AppError("This dish requires a customization. Check it in Swiggy.", 409)
    if not cart_lock.acquire(blocking=False):
        raise AppError("A cart check is already running. Wait for it to finish.", 409)
    try:
        with client_factory(token()) as client:
            check_address(client, current["address_id"])
            result = quote_offer(client, offer, current["address_id"],
                                 replace_existing_cart=data.get("replace_existing_cart") is True)
        captured = result.pop("_bill_diagnostic", None)
        if captured is not None:
            # Captured for searched offers only; discarded with the search.
            with state_lock:
                current.setdefault("bill_diagnostics", {})[offer["id"]] = {
                    "restaurant": offer["restaurant"], "dish": offer["dish"],
                    "captured_at": time.time(), "offer": result["offer"],
                    "cart_empty_after_check": result["cart_empty"],
                    **redact_diagnostic(captured),
                }
        current.setdefault("quoted", {})[offer["id"]] = result["offer"]
        return jsonify(result)
    except AppError as exc:
        return jsonify(error=str(exc), safe_to_continue=getattr(exc, "safe_to_continue", False)), exc.status
    finally:
        cart_lock.release()



@app.get("/api/tools")
def list_swiggy_tools():
    """List tools without calling them or modifying the cart."""
    with client_factory(token()) as client:
        initialized = client.rpc("initialize", {
            "protocolVersion": client.protocol,
            "capabilities": {},
            "clientInfo": {"name": "Dishwise", "version": "1.0.0"},
        })
        if not isinstance(initialized, dict) or not isinstance(initialized.get("protocolVersion"), str):
            raise AppError("Swiggy MCP initialization failed.", 502)
        client.client.headers["MCP-Protocol-Version"] = initialized["protocolVersion"]
        client.rpc("notifications/initialized", {}, notification=True)
        client.started = True
        all_tools, params, seen_cursors = [], {}, set()
        for _ in range(20):
            result = client.rpc("tools/list", params)
            if not isinstance(result, dict) or not isinstance(result.get("tools"), list):
                raise AppError("Unexpected tools/list response.", 502)
            if any(not isinstance(t, dict) or not isinstance(t.get("name"), str) for t in result["tools"]):
                raise AppError("Invalid tool definition.", 502)
            all_tools.extend(result["tools"])
            cursor = result.get("nextCursor")
            if cursor is None:
                return jsonify(count=len(all_tools), tools=all_tools)
            if not isinstance(cursor, str) or not cursor or cursor in seen_cursors:
                raise AppError("Invalid tools/list pagination.", 502)
            seen_cursors.add(cursor)
            params = {"cursor": cursor}
        raise AppError("Tool listing exceeded the pagination limit.", 502)


def redact_diagnostic(value):
    """Remove authentication/session fields before exposing diagnostics."""
    sensitive = {"tid", "sid", "token", "access_token", "refresh_token",
                 "authorization", "cookie", "set-cookie", "deviceid"}
    if isinstance(value, dict):
        return {key: redact_diagnostic(item) for key, item in value.items()
                if key.lower() not in sensitive}
    if isinstance(value, list):
        return [redact_diagnostic(item) for item in value]
    return value


@app.get("/api/debug/bill")
def debug_bill():
    """Read captured charges before cleanup; never mutates the live cart."""
    token()
    current = state().get("search", {})
    if current.get("expires", 0) <= time.time():
        raise AppError("Run Find my deal again to capture fresh bills.", 409)
    captures = current.get("bill_diagnostics", {})
    restaurant = request.args.get("restaurant", "").strip().casefold()
    offer_id = request.args.get("offer_id")
    matches = [row for key, row in captures.items()
               if (not offer_id or key == offer_id)
               and (not restaurant or row["restaurant"].casefold() == restaurant)]
    if not matches:
        return jsonify(error="No captured bill for this selection. Run Find my deal and use a restaurant with a completed bill.",
                       available_restaurants=sorted({row["restaurant"] for row in captures.values()})), 404
    return jsonify(bills=redact_diagnostic(matches))


def diagnostic_offer():
    """Use only offers from this session's recent live search."""
    access = token()
    current = state().get("search", {})
    if current.get("expires", 0) <= time.time():
        raise AppError("Run a Live dish search first.", 409)
    offers = current.get("offers", {})
    offer_id = request.args.get("offer_id")
    offer = offers.get(offer_id) if offer_id else next(iter(offers.values()), None)
    if not offer:
        raise AppError("Dish match not found. Run another Live search.", 404)
    return access, current, offer


@app.get("/api/debug/coupons")
def debug_coupons():
    access, current, offer = diagnostic_offer()
    if not cart_lock.acquire(blocking=False):
        raise AppError("Wait for the current bill check and cleanup to finish.", 409)
    try:
        with client_factory(access) as client:
            check_address(client, current["address_id"])
            response = client.call("fetch_food_coupons", {
                "addressId": current["address_id"],
                "restaurantId": offer["restaurant_id"],
            })
        return jsonify(restaurant=offer["restaurant"], dish=offer["dish"],
                       quantity=offer["quantity"], response=redact_diagnostic(response))
    finally:
        cart_lock.release()


@app.get("/api/debug/menu")
def debug_menu():
    """Replay the read-only menu refresh that blocked the full-bill check."""
    access, current, offer = diagnostic_offer()
    with client_factory(access) as client:
        check_address(client, current["address_id"])
        response = client.call("search_menu", {
            "addressId": current["address_id"],
            "query": offer["dish"],
            "restaurantIdOfAddedItem": offer["restaurant_id"],
        })

    refreshed = menu_offers(response, offer["quantity"],
                            restaurant_id=offer["restaurant_id"],
                            restaurant_name=offer["restaurant"])
    exact = next((row for row in refreshed if row["id"] == offer["id"]), None)
    if exact is None:
        reason = "original_item_not_in_parsed_available_results"
    elif exact["customized"]:
        reason = "customization_fields_trigger_current_skip_rule"
    else:
        reason = "menu_refresh_passes_current_check"

    data = data_of(response)
    raw_items = data.get("items", [])
    raw_matches = [
        item for item in raw_items
        if isinstance(item, dict)
        and str(item.get("menu_item_id", "")) == offer["item_id"]
    ]
    return jsonify(
        restaurant=offer["restaurant"],
        dish=offer["dish"],
        original_offer=offer,
        diagnosis={
            "reason": reason,
            "exact_match_found": exact is not None,
            "raw_item_id_match_count": len(raw_matches),
            "refreshed_customized": exact["customized"] if exact else None,
        },
        refreshed_offers=refreshed,
        raw_matching_items=raw_matches,
        response=redact_diagnostic(response),
    )



@app.get("/api/debug/cart")
def debug_cart():
    """Read the provider's current cart payload without modifying it."""
    access, current, offer = diagnostic_offer()
    if not cart_lock.acquire(blocking=False):
        raise AppError("Wait for the current bill check and cleanup to finish.", 409)
    try:
        with client_factory(access) as client:
            response = client.call("get_food_cart", {
                "addressId": current["address_id"],
            })
        return jsonify(response=redact_diagnostic(response))
    finally:
        cart_lock.release()


if __name__ == "__main__":
    # Access logs can contain OAuth query parameters; keep them disabled.
    logging.getLogger("werkzeug").setLevel(logging.ERROR)
    print(f"\nDishwise is ready. Open {ORIGIN}\nPress Ctrl+C to stop.\n")
    app.run(host="127.0.0.1", port=PORT, debug=False, threaded=True, use_reloader=False)
