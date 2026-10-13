"""Small, bounded MCP client. Credentials stay in Python process memory."""
import json
import time
import httpx
from email.utils import parsedate_to_datetime

BASE_URL = "https://mcp.swiggy.com"
ALLOWED_TOOLS = frozenset({
    "get_addresses", "create_address", "search_menu", "search_restaurants", "get_food_cart",
    "update_food_cart", "fetch_food_coupons", "apply_food_coupon", "flush_food_cart",
})


class AppError(Exception):
    def __init__(self, message, status=400, *, code=None, retryable=False, retry_after=None):
        super().__init__(message)
        self.status = status
        self.code = code or "request_failed"
        self.retryable = retryable
        self.retry_after = retry_after


def check_http(response):
    if response.status_code in (401, 419):
        raise AppError("Your Swiggy session expired. Connect Swiggy again.", 401)
    if response.status_code == 429:
        value = response.headers.get("Retry-After", "30")
        try:
            delay = max(0, float(value))
        except ValueError:
            try:
                delay = max(0, parsedate_to_datetime(value).timestamp() - time.time())
            except (ValueError, TypeError, OverflowError):
                delay = 30
        raise AppError("Swiggy is limiting requests. Wait before trying again.", 429,
                       code="rate_limited", retryable=True, retry_after=delay)
    if response.status_code == 404:
        raise AppError("The Swiggy MCP session expired. Reconnect before starting a new comparison.", 409,
                       code="mcp_session_expired")
    if not response.is_success:
        raise AppError(f"Swiggy returned HTTP {response.status_code}.", 502,
                       code="upstream_http", retryable=response.status_code >= 500)


def auth_post(path, body=None, token=None):
    if path not in {"/auth/register", "/auth/token", "/auth/logout"}:
        raise AppError("Unsupported authentication endpoint.")
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    try:
        with httpx.Client(timeout=25, follow_redirects=False) as client:
            response = client.post(BASE_URL + path, json=body, headers=headers)
            check_http(response)
            result = {} if response.status_code == 204 else response.json()
            if not isinstance(result, dict):
                raise AppError("Swiggy returned an unexpected authentication response.", 502)
            return result
    except (httpx.HTTPError, ValueError) as exc:
        raise AppError("Could not connect to Swiggy. Check your internet connection and try again.", 502) from exc


class SwiggyClient:
    def __init__(self, token, transport=None):
        self.client = httpx.Client(
            timeout=httpx.Timeout(25, connect=10), follow_redirects=False,
            transport=transport,
            headers={"Authorization": "Bearer " + token,
                     "Accept": "application/json, text/event-stream"},
        )
        self.counter = 0
        self.calls = 0
        self.started = False
        self.protocol = "2025-03-26"
        self.deadline = time.monotonic() + 360
        self.call_limit = 36
        self.cleanup_started = False
        self.retry_deadline = None
        self.events = []
        self.cooldown_until = 0

    def begin_cleanup(self):
        """Reserve independent capacity; coupon/search work cannot consume it."""
        if not self.cleanup_started:
            self.cleanup_started = True
            self.call_limit = self.calls + 12
            self.deadline = time.monotonic() + 60
            self.retry_deadline = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.client.close()

    def resume_session(self, saved):
        """Reuse only this authenticated search's server-issued MCP session."""
        if (isinstance(saved, dict) and isinstance(saved.get("id"), str)
                and saved["id"] and isinstance(saved.get("protocol"), str)):
            self.client.headers["Mcp-Session-Id"] = saved["id"]
            self.client.headers["MCP-Protocol-Version"] = saved["protocol"]
            self.started = True

    def session_info(self):
        sid = self.client.headers.get("Mcp-Session-Id")
        protocol = self.client.headers.get("MCP-Protocol-Version")
        return {"id": sid, "protocol": protocol} if self.started and sid and protocol else None

    def rpc(self, method, params, notification=False):
        delay = self.cooldown_until - time.monotonic()
        if delay > 0:
            # A provider cooldown also applies to cleanup calls; never hammer
            # the server with a flush immediately after a rate-limit response.
            if delay >= 30 or time.monotonic()+delay >= self.deadline:
                raise AppError("Swiggy requested a longer cooldown before another cart call.", 429,
                               code="rate_limited", retryable=True, retry_after=delay)
            time.sleep(delay)
            self.cooldown_until = 0
        self.calls += 1
        if self.calls > self.call_limit or time.monotonic() > self.deadline:
            raise AppError("The local request budget was reached.", 409, code="local_budget")
        self.counter += 1
        message = {"jsonrpc": "2.0", "method": method, "params": params}
        if not notification:
            message["id"] = self.counter
        try:
            timeout = max(.1, min(25, self.deadline - time.monotonic()))
            with self.client.stream("POST", BASE_URL + "/food", json=message, timeout=timeout) as response:
                try:
                    check_http(response)
                except AppError as exc:
                    if exc.status == 429:
                        self.cooldown_until = time.monotonic() + (exc.retry_after if exc.retry_after is not None else 30)
                    raise
                session_id = response.headers.get("mcp-session-id")
                if session_id:
                    self.client.headers["Mcp-Session-Id"] = session_id
                if notification:
                    return None
                if "text/event-stream" in response.headers.get("content-type", ""):
                    parts, size, result = [], 0, None
                    for line in response.iter_lines():
                        size += len(line)
                        if size > 2_000_000 or time.monotonic() > self.deadline:
                            raise AppError("Swiggy response exceeded the request limit.", 502)
                        if line.startswith("data:"):
                            parts.append(line[5:].lstrip())
                        elif not line and parts:
                            event = json.loads("\n".join(parts))
                            parts = []
                            if isinstance(event, dict) and event.get("id") == self.counter:
                                result = event
                                break
                    if result is None and parts:
                        event = json.loads("\n".join(parts))
                        if isinstance(event, dict) and event.get("id") == self.counter:
                            result = event
                else:
                    raw = response.read()
                    if len(raw) > 2_000_000:
                        raise AppError("Swiggy response was too large.", 502)
                    result = json.loads(raw)
                if not isinstance(result, dict) or result.get("id") != self.counter:
                    raise AppError("Swiggy returned an unrecognized MCP response.", 502, code="rpc_format")
                if result.get("error"):
                    error = result["error"] if isinstance(result["error"], dict) else {}
                    if error.get("code") == -32001:
                        raise AppError("Your Swiggy session expired. Connect Swiggy again.", 401, code="auth_expired")
                    raise AppError("Swiggy rejected the MCP request.", 502, code="rpc_error")
                return result.get("result")
        except httpx.HTTPError as exc:
            raise AppError("Swiggy request failed or timed out.", 502, code="transport_error", retryable=True) from exc
        except ValueError as exc:
            raise AppError("Swiggy returned invalid JSON.", 502, code="invalid_json") from exc

    def call(self, name, arguments):
        # Retry only explicit transient failures, in the SAME MCP session.
        # Never retry ordering, payment, auth, malformed data or validation errors.
        retry_tools = ALLOWED_TOOLS - {"create_address", "update_food_cart", "apply_food_coupon", "flush_food_cart"}
        for attempt in range(3):
            started = time.monotonic()
            try:
                result = self._call_once(name, arguments)
                self.events.append({"tool": name, "status": "ok", "attempt": attempt + 1,
                                    "duration_ms": round((time.monotonic()-started)*1000)})
                return result
            except AppError as exc:
                self.events.append({"tool": name, "status": "error", "code": exc.code,
                                    "http_status": exc.status, "attempt": attempt + 1})
                if name not in retry_tools or not exc.retryable or attempt == 2:
                    raise
                if self.retry_deadline is None:
                    self.retry_deadline = time.monotonic() + 30
                delay = exc.retry_after if exc.retry_after is not None else .5 * 2**attempt
                if time.monotonic()+delay >= min(self.deadline, self.retry_deadline):
                    raise
                if exc.status != 429:
                    time.sleep(delay)

    def _call_once(self, name, arguments):
        if name not in ALLOWED_TOOLS:
            raise AppError("Ordering and payment tools are not enabled.", 403)
        if not self.started:
            init = self.rpc("initialize", {"protocolVersion": self.protocol,
                "capabilities": {}, "clientInfo": {"name": "Dishwise Local", "version": "1.0.0"}})
            if not isinstance(init, dict) or not isinstance(init.get("protocolVersion"), str):
                raise AppError("Swiggy MCP initialization failed.", 502)
            self.client.headers["MCP-Protocol-Version"] = init["protocolVersion"]
            self.rpc("notifications/initialized", {}, notification=True)
            self.started = True
        result = self.rpc("tools/call", {"name": name, "arguments": arguments})
        if not isinstance(result, dict) or result.get("isError"):
            raise AppError(f"Swiggy could not complete {name}.", 502, code="tool_error")
        payload = result.get("structuredContent")
        if not isinstance(payload, dict):
            payload = None
            for block in result.get("content", []):
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text":
                    text = block.get("text", "").strip()
                    if text.startswith("```"):
                        text = "\n".join(text.splitlines()[1:-1])
                    try:
                        parsed = json.loads(text)
                        if isinstance(parsed, dict):
                            payload = parsed
                            break
                    except ValueError:
                        continue
        if not isinstance(payload, dict):
            if name == "flush_food_cart":
                # An acknowledgement is NOT evidence of an empty cart.
                # The cart lifecycle always follows this with get_food_cart.
                return {"acknowledgement_only": True}
            raise AppError(f"The {name} response could not be parsed as JSON.", 502, code="tool_format")
        if name != "get_food_cart" and (payload.get("success") is False or payload.get("successful") is False):
            raise AppError(f"The {name} response reported failure.", 502, code="tool_failed")
        return payload
