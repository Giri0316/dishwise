"""Small, bounded MCP client. Credentials stay in Python process memory."""
import json
import time
import httpx

BASE_URL = "https://mcp.swiggy.com"
ALLOWED_TOOLS = frozenset({
    "get_addresses", "create_address", "search_menu", "get_food_cart",
    "update_food_cart", "fetch_food_coupons", "apply_food_coupon", "flush_food_cart",
})


class AppError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def check_http(response):
    if response.status_code in (401, 419):
        raise AppError("Your Swiggy session expired. Connect Swiggy again.", 401)
    if response.status_code == 429:
        raise AppError("Swiggy is limiting requests. Wait before trying again.", 429)
    if not response.is_success:
        raise AppError(f"Swiggy returned HTTP {response.status_code}. No automatic retry was made.", 502)


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

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.client.close()

    def rpc(self, method, params, notification=False):
        self.calls += 1
        if self.calls > 36 or time.monotonic() > self.deadline:
            raise AppError("The comparison limit was reached. Check your Swiggy cart before continuing.", 429)
        self.counter += 1
        message = {"jsonrpc": "2.0", "method": method, "params": params}
        if not notification:
            message["id"] = self.counter
        try:
            with self.client.stream("POST", BASE_URL + "/food", json=message) as response:
                check_http(response)
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
                else:
                    raw = response.read()
                    if len(raw) > 2_000_000:
                        raise AppError("Swiggy response was too large.", 502)
                    result = json.loads(raw)
                if not isinstance(result, dict) or result.get("id") != self.counter or result.get("error"):
                    raise AppError("Swiggy returned an unsuccessful or unrecognized MCP response.", 502)
                return result.get("result")
        except (httpx.HTTPError, ValueError) as exc:
            raise AppError("Swiggy request failed or timed out. No automatic retry was made.", 502) from exc

    def call(self, name, arguments):
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
            raise AppError(f"Swiggy could not complete {name}. No automatic retry was made.", 502)
        payload = result.get("structuredContent")
        if not isinstance(payload, dict):
            payload = None
            for block in result.get("content", []):
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
        if not isinstance(payload, dict) or payload.get("success") is False or payload.get("successful") is False:
            raise AppError(f"The {name} response could not be confirmed.", 502)
        return payload
