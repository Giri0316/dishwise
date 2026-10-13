"""Failure-injection tests: no authenticated/live cart or order operations."""
import copy
import json
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch
import httpx
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from comparison import parse_cart, clear_and_confirm, quote_offer, menu_offers
from swiggy import AppError, SwiggyClient
from coupons import code_of
from test_live import QuoteProvider, menu


def offer():
    return menu_offers({"items": [menu("1")]}, 1)[0]


def basket():
    p = QuoteProvider(coupons=False)
    p.call("update_food_cart", {})
    return p.basket


class CartRecoveryTests(unittest.TestCase):
    def test_documented_observed_nested_and_bare_shapes(self):
        b = basket()
        for payload in ({"success": True, "data": b}, {"success": True, "data": {"data": b}},
                        {"response": {"statusCode": 0, "data": {**b, "result": "success"}}},
                        {**b, "result": "success"}):
            self.assertEqual(parse_cart(payload)["items"], b["items"])
        for payload in ({"success": True, "data": None},
                        {"response": {"statusCode": 0, "statusMessage": "CART", "data": None}},
                        {"success": True, "data": {"data": {"items": [], "item_count": 0}}}):
            self.assertIsNone(parse_cart(payload))

    def test_error_null_or_conflicting_count_never_proves_empty(self):
        for p in ({}, {"data": None}, {"success": False, "data": None},
                  {"success": True, "data": {"success": False, "data": None}},
                  {"statusCode": 6, "data": None},
                  {"success": True, "data": {"items": [], "item_count": 2}}):
            with self.assertRaises(AppError): parse_cart(p, True)

    def test_unavailable_cart_can_be_identified_but_never_quoted(self):
        for status in (6, 8, 12):
            p = {"statusCode": status, "data": basket()}
            with self.assertRaises(AppError): parse_cart(p)
            self.assertEqual(parse_cart(p, True)["items"], basket()["items"])

    def test_lost_flush_acknowledgement_is_reconciled(self):
        class Provider(QuoteProvider):
            def call(self, name, args):
                response = super().call(name, args)
                if name == "flush_food_cart":
                    raise AppError("timeout", 502, code="transport_error", retryable=True)
                return response
        p = Provider(coupons=False)
        r = quote_offer(p, offer(), "addr", True)
        self.assertTrue(r["cart_empty"])
        self.assertEqual(p.names.count("flush_food_cart"), 2)  # setup + cleanup, no blind retry

    def test_transient_clear_failure_retries_after_cart_read(self):
        class Provider(QuoteProvider):
            def __init__(self):
                super().__init__(); self.first = True; self.basket = basket()
            def call(self, name, args):
                if name == "flush_food_cart" and self.first:
                    self.first = False; self.names.append(name)
                    raise AppError("temporary", 502, retryable=True)
                return super().call(name, args)
        p = Provider()
        with patch("time.sleep"):
            self.assertTrue(clear_and_confirm(p, {"addressId": "addr"}, [], offer()))
        self.assertEqual(p.names, ["get_food_cart", "flush_food_cart", "get_food_cart", "get_food_cart", "flush_food_cart", "get_food_cart"])

    def test_delayed_empty_confirmation_does_not_repeat_clear(self):
        class Provider(QuoteProvider):
            def __init__(self):
                super().__init__(); self.basket = basket(); self.stale = None
            def call(self, name, args):
                if name == "flush_food_cart":
                    old = copy.deepcopy(self.basket)
                    result = super().call(name, args); self.stale = old; return result
                if name == "get_food_cart" and self.stale:
                    b, self.stale = self.stale, None
                    self.names.append(name); return {"success": True, "data": b}
                return super().call(name, args)
        p = Provider()
        with patch("time.sleep"):
            self.assertTrue(clear_and_confirm(p, {}, [], offer()))
        self.assertEqual(p.names.count("flush_food_cart"), 1)

    def test_wrong_recipe_same_identity_is_cleaned_but_not_ranked(self):
        class Provider(QuoteProvider):
            def call(self, name, args):
                result = super().call(name, args)
                if name == "update_food_cart": self.basket["items"][0]["name"] = "Masala Dosai"
                return result
        p = Provider()
        with self.assertRaises(AppError) as caught: quote_offer(p, offer(), "addr", True)
        self.assertTrue(caught.exception.safe_to_continue)
        self.assertIsNone(p.basket)
        self.assertNotIn("fetch_food_coupons", p.names)

    def test_different_item_quantity_or_options_are_preserved(self):
        for change in ({"menu_item_id": "other"}, {"quantity": 2}, {"addons": [{"id": "extra"}]}):
            p = QuoteProvider(); p.basket = basket(); p.basket["items"][0].update(change)
            with self.assertRaises(AppError) as e: clear_and_confirm(p, {}, [], offer())
            self.assertEqual(e.exception.code, "cart_changed")
            self.assertNotIn("flush_food_cart", p.names)

    def test_unrecognized_read_preserves_original_and_cleanup_errors(self):
        class Provider(QuoteProvider):
            def call(self, name, args):
                if name == "get_food_cart" and self.basket and self.basket.get("cart_id"):
                    return {"statusCode": 0, "unrecognized": True}
                return super().call(name, args)
        p = Provider()
        with self.assertRaises(AppError) as e: quote_offer(p, offer(), "addr", True)
        self.assertEqual(e.exception.code, "cleanup_failed")
        self.assertEqual(e.exception.diagnostic["original_error"], "cart_format")
        self.assertEqual(e.exception.diagnostic["cleanup_error"], "cart_format")
        self.assertTrue(e.exception.diagnostic["trace"])
        self.assertFalse(e.exception.safe_to_continue)

    def test_clear_not_taking_effect_stops_after_two_attempts(self):
        class Provider(QuoteProvider):
            def call(self, name, args):
                if name == "flush_food_cart": self.names.append(name); return {"success": True}
                return super().call(name, args)
        p = Provider(); p.basket = basket()
        with patch("time.sleep"), self.assertRaises(AppError) as e: clear_and_confirm(p, {}, [], offer())
        self.assertEqual(e.exception.code, "cart_not_empty")
        self.assertEqual(p.names.count("flush_food_cart"), 2)

    def test_uuid_coupon_uses_display_code(self):
        self.assertEqual(code_of({"code":"acda1234-abcd-abcd-abcd-123456789012", "title":"SAVE30"}), "SAVE30")
        self.assertIsNone(code_of({"id":"opaque", "title":"Save Rs 30"}))


class TransportTests(unittest.TestCase):
    def client(self, handler):
        c = SwiggyClient("test-secret", httpx.MockTransport(handler))
        c.resume_session({"id":"same-session", "protocol":"2025-03-26"})
        return c

    def response(self, req, payload):
        return httpx.Response(200, json={"jsonrpc":"2.0", "id":json.loads(req.content)["id"], "result":{"structuredContent":payload}})

    def test_sse_last_event_without_blank_line(self):
        def handler(req):
            event={"jsonrpc":"2.0", "id":json.loads(req.content)["id"], "result":{"structuredContent":{"success":True,"data":None}}}
            return httpx.Response(200, headers={"Content-Type":"text/event-stream"}, text="data: "+json.dumps(event))
        with self.client(handler) as c:
            self.assertIsNone(parse_cart(c.call("get_food_cart", {"addressId":"addr"})))

    def test_retry_read_on_503_in_same_session(self):
        requests=[]
        def handler(req):
            requests.append(req)
            if len(requests)==1: return httpx.Response(503)
            self.assertEqual(req.headers["Mcp-Session-Id"], "same-session")
            return self.response(req,{"success":True,"data":None})
        with self.client(handler) as c, patch("swiggy.time.sleep") as sleep:
            c.call("get_food_cart", {})
            self.assertEqual(len(requests), 2); sleep.assert_called_once_with(.5)

    def test_mutation_timeout_is_not_blindly_replayed(self):
        calls=[]
        def handler(req): calls.append(req); raise httpx.ReadTimeout("lost ack")
        with self.client(handler) as c, self.assertRaises(AppError): c.call("update_food_cart", {})
        self.assertEqual(len(calls), 1)

    def test_auth_and_expired_mcp_session_are_not_retried(self):
        for status in (401, 419, 404):
            calls=[]
            def handler(req): calls.append(req); return httpx.Response(status)
            with self.client(handler) as c, self.assertRaises(AppError): c.call("get_food_cart", {})
            self.assertEqual(len(calls),1)

    def test_rpc_auth_error_classified(self):
        def handler(req): return httpx.Response(200,json={"jsonrpc":"2.0","id":json.loads(req.content)["id"],"error":{"code":-32001,"message":"private"}})
        with self.client(handler) as c, self.assertRaises(AppError) as e: c.call("get_food_cart", {})
        self.assertEqual(e.exception.status,401)
        self.assertNotIn("private",str(e.exception))

    def test_retry_after_is_honored_and_bounded(self):
        calls=[]
        def handler(req):
            calls.append(req)
            if len(calls)==1:return httpx.Response(429,headers={"Retry-After":"2"})
            return self.response(req,{"success":True,"data":None})
        with self.client(handler) as c, patch("swiggy.time.sleep") as sleep:
            c.call("get_food_cart",{});self.assertEqual(sleep.call_count,1)
            self.assertGreater(sleep.call_args.args[0],1.9)
            self.assertLessEqual(sleep.call_args.args[0],2)
        def long(req):return httpx.Response(429,headers={"Retry-After":"90"})
        with self.client(long) as c, patch("swiggy.time.sleep") as sleep, self.assertRaises(AppError):
            try:c.call("get_food_cart",{})
            finally:sleep.assert_not_called()

    def test_cleanup_has_capacity_after_main_budget_is_exhausted(self):
        def handler(req):return self.response(req,{"success":True,"data":None})
        with self.client(handler) as c:
            c.calls=36;c.deadline=time.monotonic()-1
            c.begin_cleanup()
            self.assertTrue(clear_and_confirm(c,{},[],offer()))
            self.assertEqual(c.calls,37)
            self.assertEqual(c.call_limit,48)

    def test_cleanup_read_honors_flush_rate_limit(self):
        names=[]
        def handler(req):
            name=json.loads(req.content)["params"]["name"];names.append(name)
            if name=="flush_food_cart":return httpx.Response(429,headers={"Retry-After":"2"})
            return self.response(req,{"success":True,"data":None})
        with self.client(handler) as c, patch("swiggy.time.sleep") as sleep:
            self.assertTrue(clear_and_confirm(c,{},[]))
            self.assertEqual(sleep.call_count,1)
            self.assertGreater(sleep.call_args.args[0],1.9)
            self.assertEqual(names,["flush_food_cart","get_food_cart"])

    def test_text_flush_ack_requires_followup_empty_read(self):
        tools=[]
        def handler(req):
            msg=json.loads(req.content);name=msg["params"]["name"];tools.append(name)
            if name=="flush_food_cart":return httpx.Response(200,json={"id":msg["id"],"result":{"content":[{"type":"text","text":"Cart cleared"}]}})
            return self.response(req,{"successful":True,"statusCode":0,"data":None})
        with self.client(handler) as c:self.assertTrue(clear_and_confirm(c,{},[]))
        self.assertEqual(tools,["flush_food_cart","get_food_cart"])

    def test_full_lifecycle_through_http_client_lost_cleanup_ack(self):
        provider=QuoteProvider(coupons=False); flushes=0
        def handler(req):
            nonlocal flushes
            msg=json.loads(req.content);name=msg["params"]["name"]
            result=provider.call(name,msg["params"]["arguments"])
            if name=="flush_food_cart":
                flushes+=1
                if flushes==2: raise httpx.ReadTimeout("response lost after server cleared cart")
            return self.response(req,result)
        with self.client(handler) as c:r=quote_offer(c,offer(),"addr",True)
        self.assertTrue(r["cart_empty"]);self.assertEqual(flushes,2)
        self.assertEqual(r["offer"]["total"],146)


class DiagnosticTests(unittest.TestCase):
    def test_failed_quote_trace_is_retrievable_and_readonly(self):
        import app as module
        from discovery import new_search
        class Broken(QuoteProvider):
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def call(self,name,args):
                if name=="get_food_cart" and self.basket and self.basket.get("cart_id"):
                    return {"statusCode":0,"unexpected":True,"tid":"secret","deliverySubtitle":"private address"}
                return super().call(name,args)
        p=Broken(); c=module.app.test_client()
        with c.session_transaction() as sess:sess["sid"]="recovery-test"
        run=new_search("Plain Dosa",1,"addr");run["done"]=True
        o=offer();o["distance_km"]=2;run["offers"]={o["id"]:o}
        module.states["recovery-test"]={"csrf":"csrf","token":"fake","expires":time.time()+300,"search":run}
        with patch.object(module,"client_factory",lambda _:p):
            result=c.post("/api/quote",json={"search_id":run["id"],"offer_id":o["id"],"consent":True,"replace_existing_cart":True},headers={"Origin":module.ORIGIN,"X-CSRF-Token":"csrf"})
            self.assertEqual(result.status_code,409)
            self.assertEqual(result.json["error_code"],"cleanup_failed")
            before=p.calls
            debug=c.get("/api/debug/cleanup")
            self.assertEqual(before,p.calls)
            self.assertEqual(debug.json["checks"][0]["cleanup_error"],"cart_format")
            self.assertNotIn(b'private address',debug.data)
            self.assertNotIn(b'"secret"',debug.data)
            self.assertTrue(run["cart_blocked"])

if __name__ == "__main__":unittest.main()
