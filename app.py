"""Run locally with: python app.py. For deployment, see README."""
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

from comparison import check_address, data_of, menu_offers, quote_offer, sample_offers
from swiggy import AppError, BASE_URL, SwiggyClient, auth_post

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")

PORT = int(os.getenv("PORT", "8765"))
if not 1024 <= PORT <= 65535:
    raise ValueError("PORT must be between 1024 and 65535.")

# Render supplies RENDER_EXTERNAL_URL automatically.
# Set APP_URL explicitly when using a custom domain.
APP_URL = (
    os.getenv("APP_URL") or os.getenv("RENDER_EXTERNAL_URL") or ""
).strip().rstrip("/")

PRODUCTION = bool(APP_URL)

if PRODUCTION:
    parts = urlparse(APP_URL)
    if (
        parts.scheme != "https"
        or not parts.hostname
        or parts.path
        or parts.query
    ):
        raise ValueError(
            "APP_URL must look like https://your-domain.com (https, no path)."
        )

    if len(os.getenv("SECRET_KEY", "")) < 32:
        raise ValueError(
            "Set SECRET_KEY to a random value of at least 32 characters "
            "in production."
        )

    ORIGIN = APP_URL
    ALLOWED_HOSTS = [parts.hostname]
else:
    ORIGIN = f"http://localhost:{PORT}"
    ALLOWED_HOSTS = ["localhost", "127.0.0.1"]

REDIRECT_URI = ORIGIN + "/auth/callback"

app = Flask(__name__)
app.config.update(
    SECRET_KEY=os.getenv("SECRET_KEY") or secrets.token_hex(32),
    MAX_CONTENT_LENGTH=32_768,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=PRODUCTION,
    SESSION_COOKIE_NAME="dishwise" if PRODUCTION else "dishwise_local",
    TRUSTED_HOSTS=ALLOWED_HOSTS,
)

if PRODUCTION:
    # Hosting platforms terminate HTTPS at a proxy; trust exactly one hop.
    app.wsgi_app = ProxyFix(
        app.wsgi_app,
        x_for=1,
        x_proto=1,
        x_host=1,
    )

# Tokens, temporary results and address IDs are memory-only.
states = {}
state_lock = threading.RLock()
cart_lock = threading.Lock()
client_factory = SwiggyClient


def state():
    with state_lock:
        sid = session.get("sid")

        if len(states) > 500:
            expired = [
                key
                for key, value in states.items()
                if value.get("expires", 0) <= time.time() and key != sid
            ]
            for key in expired[:250]:
                states.pop(key, None)

        if sid not in states:
            sid = secrets.token_urlsafe(32)
            session["sid"] = sid
            states[sid] = {
                "csrf": secrets.token_urlsafe(32),
                "expires": 0,
            }

        return states[sid]


def token():
    current = state()
    if current.get("expires", 0) <= time.time() or not current.get("token"):
        current.pop("token", None)
        raise AppError(
            "Click Connect Swiggy and sign in to load live prices.",
            401,
        )
    return current["token"]


def body():
    value = request.get_json(silent=True)
    if not isinstance(value, dict):
        raise AppError("Send a valid JSON request.")
    return value


def text(value, label, minimum=1, maximum=180):
    if (
        not isinstance(value, str)
        or not minimum <= len(value.strip()) <= maximum
    ):
        raise AppError(f"Please enter a valid {label}.")
    return value.strip()


def quantity(value):
    if type(value) is not int or not 1 <= value <= 10:
        raise AppError("Quantity must be a whole number from 1 to 10.")
    return value


@app.before_request
def protect_local_app():
    if not PRODUCTION and request.remote_addr not in {
        "127.0.0.1",
        "::1",
        None,
    }:
        raise AppError(
            "This app is available only on your own computer.",
            403,
        )

    if request.method == "POST":
        if request.headers.get("Origin") != ORIGIN:
            raise AppError(f"Open {ORIGIN} to use the app.", 403)

        supplied = request.headers.get("X-CSRF-Token", "")
        if not supplied or not secrets.compare_digest(
            supplied,
            state()["csrf"],
        ):
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
    return jsonify(
        error=(
            "The request could not finish. If a bill check was running, "
            "review your Swiggy cart."
        )
    ), 500


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
    return jsonify(
        csrf=current["csrf"],
        connected=bool(
            current.get("token")
            and current.get("expires", 0) > time.time()
        ),
    )


@app.post("/api/auth/start")
def auth_start():
    registration = auth_post(
        "/auth/register",
        {
            "client_name": "Dishwise" if PRODUCTION else "Dishwise Local",
            "redirect_uris": [REDIRECT_URI],
            "grant_types": ["authorization_code"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
        },
    )

    client_id = registration.get("client_id")
    if not isinstance(client_id, str) or not client_id:
        raise AppError(
            "Swiggy did not register the callback URL. "
            "See README troubleshooting.",
            502,
        )

    verifier = secrets.token_urlsafe(48)
    nonce = secrets.token_urlsafe(32)
    challenge = (
        base64.urlsafe_b64encode(
            hashlib.sha256(verifier.encode()).digest()
        )
        .rstrip(b"=")
        .decode()
    )

    state()["oauth"] = {
        "state": nonce,
        "verifier": verifier,
        "client_id": client_id,
        "expires": time.time() + 600,
    }

    query = urlencode(
        {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": REDIRECT_URI,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": nonce,
            "scope": "mcp:tools",
        }
    )

    return jsonify(url=BASE_URL + "/auth/authorize?" + query)


@app.get("/auth/callback")
def auth_callback():
    current = state()
    pending = current.pop("oauth", {})
    nonce = request.args.get("state", "")
    code = request.args.get("code", "")

    if (
        not pending
        or not nonce
        or not code
        or request.args.get("error")
        or not secrets.compare_digest(
            pending.get("state", ""),
            nonce,
        )
        or pending.get("expires", 0) <= time.time()
    ):
        return redirect(ORIGIN + "/?auth_error=1")

    try:
        result = auth_post(
            "/auth/token",
            {
                "grant_type": "authorization_code",
                "code": code,
                "code_verifier": pending["verifier"],
                "client_id": pending["client_id"],
                "redirect_uri": REDIRECT_URI,
            },
        )

        access = result.get("access_token")
        expires = result.get("expires_in")

        if (
            not isinstance(access, str)
            or type(expires) not in (int, float)
            or not 0 < expires <= 10_000_000
        ):
            raise AppError("Invalid login response.")

        current["token"] = access
        current["expires"] = time.time() + min(expires, 432000)
        current.pop("search", None)

        return redirect(ORIGIN + "/?connected=1")
    except AppError:
        return redirect(ORIGIN + "/?auth_error=1")


@app.post("/api/auth/disconnect")
def disconnect():
    if not cart_lock.acquire(blocking=False):
        raise AppError(
            "Wait for the current bill check and cleanup to finish.",
            409,
        )

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


@app.get("/api/tools")
def list_swiggy_tools():
    """List MCP tools available to the current authenticated user."""
    with client_factory(token()) as client:
        initialized = client.rpc(
            "initialize",
            {
                "protocolVersion": client.protocol,
                "capabilities": {},
                "clientInfo": {
                    "name": "Dishwise",
                    "version": "1.0.0",
                },
            },
        )

        if (
            not isinstance(initialized, dict)
            or not isinstance(
                initialized.get("protocolVersion"),
                str,
            )
        ):
            raise AppError("Swiggy MCP initialization failed.", 502)

        client.client.headers["MCP-Protocol-Version"] = (
            initialized["protocolVersion"]
        )
        client.rpc(
            "notifications/initialized",
            {},
            notification=True,
        )
        client.started = True

        all_tools = []
        params = {}
        seen_cursors = set()

        for _ in range(20):
            result = client.rpc("tools/list", params)

            if (
                not isinstance(result, dict)
                or not isinstance(result.get("tools"), list)
            ):
                raise AppError(
                    "Swiggy returned an unexpected tools/list response.",
                    502,
                )

            tools = result["tools"]
            if any(
                not isinstance(tool, dict)
                or not isinstance(tool.get("name"), str)
                for tool in tools
            ):
                raise AppError(
                    "Swiggy returned an invalid tool definition.",
                    502,
                )

            all_tools.extend(tools)
            cursor = result.get("nextCursor")

            if cursor is None:
                return jsonify(
                    count=len(all_tools),
                    tools=all_tools,
                )

            if (
                not isinstance(cursor, str)
                or not cursor
                or cursor in seen_cursors
            ):
                raise AppError(
                    "Swiggy returned invalid tools/list pagination.",
                    502,
                )

            seen_cursors.add(cursor)
            params = {"cursor": cursor}

        raise AppError(
            "Tool listing exceeded the pagination limit.",
            502,
        )


@app.get("/api/addresses")
def addresses():
    page = request.args.get("page", 1, type=int)
    if not 1 <= page <= 20:
        raise AppError("Invalid address page.")

    with client_factory(token()) as client:
        data = data_of(
            client.call(
                "get_addresses",
                {"page": page, "pageSize": 10},
            )
        )

        if not isinstance(data.get("addresses"), list):
            raise AppError(
                "Saved addresses were not returned in the expected format.",
                502,
            )

        return jsonify(
            addresses=[
                {
                    "id": row["id"],
                    "label": row.get("addressLine", "Saved address"),
                    "tag": (
                        row.get("addressTag")
                        or row.get("addressCategory")
                        or ""
                    ),
                }
                for row in data["addresses"]
                if row.get("id")
            ],
            has_more=bool(
                (data.get("pagination") or {}).get("hasMore")
            ),
        )


@app.post("/api/search")
def search():
    data = body()
    dish = text(data.get("dish"), "dish", 1, 100)
    count = quantity(data.get("quantity"))

    if data.get("mode") == "demo":
        area = text(
            data.get("area", "Adyar, Chennai"),
            "area",
            1,
            200,
        )
        return jsonify(
            offers=sample_offers(dish, count, area),
            scope="Fictional sample prices.",
        )

    if data.get("mode") != "live":
        raise AppError("Select Demo or Live mode.")

    address_id = text(
        data.get("address_id"),
        "saved delivery address",
    )

    with client_factory(token()) as client:
        check_address(client, address_id)
        result = client.call(
            "search_menu",
            {
                "addressId": address_id,
                "query": dish,
                "offset": 0,
            },
        )
        rows = menu_offers(result, count)[:5]

    state()["search"] = {
        "address_id": address_id,
        "expires": time.time() + 900,
        "offers": {row["id"]: row for row in rows},
    }

    return jsonify(
        offers=rows,
        scope=(
            "Up to five matches from the returned menu page. "
            "Fees and coupons are unverified."
        ),
    )


@app.get("/api/debug/coupons")
def debug_coupons():
    """Fetch the unfiltered coupon payload for a recent Live search match."""
    access = token()
    current = state().get("search", {})

    if current.get("expires", 0) <= time.time():
        raise AppError("Run a Live dish search first.", 409)

    offers = current.get("offers", {})
    offer_id = request.args.get("offer_id")

    if offer_id:
        offer = offers.get(offer_id)
        if not offer:
            raise AppError("Offer not found. Search again.", 404)
    else:
        offer = next(iter(offers.values()), None)

    if not offer:
        raise AppError(
            "No dish matches found. Run another Live search.",
            409,
        )

    # Avoid inspecting a temporary cart while a bill check is running.
    if not cart_lock.acquire(blocking=False):
        raise AppError(
            "Wait for the current bill check and cleanup to finish.",
            409,
        )

    try:
        with client_factory(access) as client:
            check_address(client, current["address_id"])
            response = client.call(
                "fetch_food_coupons",
                {
                    "addressId": current["address_id"],
                    "restaurantId": offer["restaurant_id"],
                },
            )

        # Preserve the provider payload without applying coupon filters.
        return jsonify(
            restaurant=offer["restaurant"],
            dish=offer["dish"],
            quantity=offer["quantity"],
            response=response,
        )
    finally:
        cart_lock.release()


@app.post("/api/quote")
def quote():
    data = body()
    if data.get("consent") is not True:
        raise AppError(
            "Confirm temporary cart use before checking bills."
        )

    current = state().get("search", {})
    offer = current.get("offers", {}).get(data.get("offer_id"))

    if not offer or current.get("expires", 0) <= time.time():
        raise AppError(
            "Search again before checking this bill.",
            409,
        )

    if offer["customized"]:
        raise AppError(
            "This dish requires a customization. Check it in Swiggy.",
            409,
        )

    if not cart_lock.acquire(blocking=False):
        raise AppError(
            "A cart check is already running. Wait for it to finish.",
            409,
        )

    try:
        with client_factory(token()) as client:
            check_address(client, current["address_id"])
            result = quote_offer(
                client,
                offer,
                current["address_id"],
            )
        return jsonify(result)
    finally:
        cart_lock.release()


if __name__ == "__main__":
    # Access logs can contain OAuth query parameters; keep them disabled.
    logging.getLogger("werkzeug").setLevel(logging.ERROR)
    print(
        f"\nDishwise is ready. Open {ORIGIN}\n"
        "Press Ctrl+C to stop.\n"
    )
    app.run(
        host="127.0.0.1",
        port=PORT,
        debug=False,
        threaded=True,
        use_reloader=False,
    )
