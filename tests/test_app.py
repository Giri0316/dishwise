"""Offline checks only: never call a real Swiggy account."""
import json
import time
import unittest
from unittest.mock import patch
import httpx
import app as web
from comparison import exact_basket, parse_cart, quote_offer, read_bill, sample_offers
from swiggy import AppError, SwiggyClient

OFFER = {"id":"r:i", "item_id":"i", "restaurant_id":"r", "restaurant":"Test Kitchen",
         "dish":"Rice", "quantity":2, "customized":False}


def cart(extra=False):
    return {"restaurant":{"id":"r"}, "items":[{"menu_item_id":"i","quantity":2}] +
            ([{"menu_item_id":"personal","quantity":1}] if extra else []),
            "pricing":{"item_total":520,"delivery_charge":56,"taxes_and_charges":28.06,"to_pay":604},
            "offers":{"coupon_applied":"SUGGESTED","coupon_discount":0}}


class FakeProvider:
    def __init__(self, existing=False, interfere=False, fail_update=False, coupons=False):
        self.basket = cart() if existing else None
        self.interfere, self.fail_update, self.coupons = interfere, fail_update, coupons
        self.calls = []
    def call(self, name, args):
        self.calls.append((name, args))
        if name == "get_food_cart":
            return {"success":True,"data":{"data":self.basket}}
        if name == "search_menu":
            return {"success":True,"data":{"items":[{"menu_item_id":"i","restaurant_id":"r","restaurant_name":"Test Kitchen","name":"Rice","price":260,"inStock":1}]}}
        if name == "update_food_cart":
            self.basket = cart(extra=self.interfere)
            if self.fail_update:
                raise AppError("Timeout after mutation")
        if name == "fetch_food_coupons":
            cards = [{"code":"MEAL80","applicable":True}] if self.coupons else []
            return {"success":True,"data":{"coupon_sections":[{"coupons":cards}],"summary":{"filter_applied":"COD only"}}}
        if name == "apply_food_coupon":
            self.basket["pricing"]["to_pay"] = 524
            self.basket["offers"] = {"coupon_applied":"MEAL80","coupon_discount":80}
        if name == "flush_food_cart":
            self.basket = None
        return {"success":True,"data":{}}


class ComparisonTests(unittest.TestCase):
    def test_cart_matching_and_unknown_shapes(self):
        self.assertTrue(exact_basket(cart(), OFFER))
        self.assertFalse(exact_basket(cart(True), OFFER))
        custom = cart(); custom["items"][0]["addons"] = [{"id":"x"}]
        self.assertFalse(exact_basket(custom, OFFER))
        self.assertIsNone(parse_cart({"success":True,"data":None}))
        for payload in ({"success":True}, {"success":True,"data":{}}, {"success":False,"data":None}):
            with self.assertRaises(AppError): parse_cart(payload)

    def test_server_total_and_zero_discount(self):
        bill = read_bill(OFFER, cart())
        self.assertEqual(bill["total"],604)
        self.assertEqual(bill["discount"],0)
        self.assertIsNone(bill["coupon"])

    def test_existing_cart_is_never_changed(self):
        provider = FakeProvider(existing=True)
        with self.assertRaises(AppError): quote_offer(provider, OFFER, "a")
        self.assertEqual([n for n,_ in provider.calls],["get_food_cart"])

    def test_normal_quote_cleans_test_cart(self):
        provider = FakeProvider()
        result = quote_offer(provider, OFFER, "a")
        self.assertEqual(result["offer"]["total"],604)
        self.assertTrue(result["cart_empty"])
        self.assertIsNone(provider.basket)

    def test_unexpected_item_is_not_cleared(self):
        provider = FakeProvider(interfere=True)
        with self.assertRaisesRegex(AppError,"cleanup"):
            quote_offer(provider, OFFER, "a")
        self.assertNotIn("flush_food_cart",[n for n,_ in provider.calls])
        self.assertEqual(len(provider.basket["items"]),2)

    def test_failed_write_reconciles_without_retry(self):
        provider = FakeProvider(fail_update=True)
        with self.assertRaises(AppError): quote_offer(provider, OFFER, "a")
        self.assertIsNone(provider.basket)
        self.assertEqual([n for n,_ in provider.calls].count("update_food_cart"),1)

    def test_coupon_savings_require_verified_cart(self):
        result = quote_offer(FakeProvider(coupons=True), OFFER, "a")
        self.assertEqual(result["offer"]["total"],524)
        self.assertEqual(result["offer"]["discount"],80)
        self.assertTrue(result["cart_empty"])

    def test_sample_quantities_and_totals(self):
        for area in ["Adyar, Chennai","Indiranagar, Bengaluru"]:
            for count in range(1,11):
                rows=sample_offers("Biryani",count,area)
                self.assertEqual([r["total"] for r in rows],sorted(r["total"] for r in rows))
                for row in rows:
                    self.assertEqual(row["item_total"],row["unit_price"]*count)
                    self.assertEqual(row["total"],row["item_total"]+row["delivery"]+row["other_charges"]-row["discount"])


class TransportTests(unittest.TestCase):
    def test_json_and_sse_and_allowed_tool_boundary(self):
        for sse in (False, True):
            calls=[]
            def handler(request):
                message=json.loads(request.content); calls.append(message)
                if message["method"]=="notifications/initialized": return httpx.Response(202)
                result={"protocolVersion":"2025-03-26"} if message["method"]=="initialize" else {"content":[{"type":"text","text":json.dumps({"success":True,"data":{"addresses":[]}})}]}
                payload={"jsonrpc":"2.0","id":message["id"],"result":result}
                if sse: return httpx.Response(200,headers={"content-type":"text/event-stream","mcp-session-id":"test"},text="event: message\ndata: "+json.dumps(payload)+"\n\n")
                return httpx.Response(200,json=payload,headers={"mcp-session-id":"test"})
            with SwiggyClient("test-not-a-token",transport=httpx.MockTransport(handler)) as client:
                self.assertTrue(client.call("get_addresses",{})["success"])
                with self.assertRaises(AppError): client.call("place_food_order",{})
            self.assertEqual(len(calls),3)


class RouteTests(unittest.TestCase):
    def setUp(self):
        web.app.config["TESTING"]=True
        self.client=web.app.test_client()
        self.base=web.ORIGIN
        config=self.client.get("/api/config",base_url=self.base).get_json()
        self.headers={"Origin":self.base,"X-CSRF-Token":config["csrf"]}

    def test_home_and_assets(self):
        for path in ("/","/static/app.js","/static/style.css"):
            response=self.client.get(path,base_url=self.base)
            self.assertEqual(response.status_code,200)
            response.close()

    def test_demo_and_validation_and_csrf(self):
        data={"mode":"demo","dish":"Rice","quantity":2,"area":"Chennai"}
        response=self.client.post("/api/search",json=data,headers=self.headers,base_url=self.base)
        self.assertEqual(response.status_code,200)
        self.assertEqual(len(response.get_json()["offers"]),4)
        self.assertEqual(self.client.post("/api/search",json=data,base_url=self.base).status_code,403)
        data["quantity"]=0
        self.assertEqual(self.client.post("/api/search",json=data,headers=self.headers,base_url=self.base).status_code,400)

    def test_live_needs_own_login(self):
        response=self.client.post("/api/search",json={"mode":"live","dish":"Rice","quantity":2,"address_id":"test"},headers=self.headers,base_url=self.base)
        self.assertEqual(response.status_code,401)

    def test_oauth_state_pkce_and_token_not_in_cookie(self):
        with patch.object(web,"auth_post",return_value={"client_id":"test-client"}):
            response=self.client.post("/api/auth/start",json={},headers=self.headers,base_url=self.base)
            self.assertEqual(response.status_code,200)
            self.assertIn("code_challenge_method=S256",response.get_json()["url"])
        with self.client.session_transaction() as session:
            sid=session["sid"]
        nonce=web.states[sid]["oauth"]["state"]
        with patch.object(web,"auth_post",return_value={"access_token":"unit-test-secret","expires_in":300}) as mocked:
            response=self.client.get("/auth/callback",query_string={"code":"test-code","state":nonce},base_url=self.base)
            self.assertEqual(response.status_code,302)
            self.assertIn("connected=1",response.location)
            self.assertNotIn("unit-test-secret",str(response.headers))
            self.assertIn("code_verifier",mocked.call_args.args[1])
        self.assertEqual(web.states[sid]["token"],"unit-test-secret")
        self.assertIsNone(web.states[sid].get("oauth"))

    def test_wrong_oauth_state_never_exchanges_code(self):
        with patch.object(web,"auth_post") as mocked:
            response=self.client.get("/auth/callback?state=wrong&code=test",base_url=self.base)
            self.assertIn("auth_error=1",response.location)
            mocked.assert_not_called()


if __name__=="__main__":
    unittest.main()
