import copy
import os
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from matching import exact_dish, customization_reason
from discovery import new_search, step
from comparison import menu_offers, quote_offer, coupon_candidates
from swiggy import AppError


def menu(rid, name="Plain Dosai", item_id=None, **extra):
    return {"menu_item_id": item_id or rid+"01", "restaurant_id":rid,
            "restaurant_name":"Restaurant "+rid,"name":name,"price":115,"inStock":1,
            "hasAddons":False,"hasVariants":False, **extra}


class DiscoveryProvider:
    def call(self,name,args):
        offset=args.get("offset",0)
        if name == "search_restaurants":
            rows=[{"id":str(i),"name":"Restaurant "+str(i),"distanceKm":i/2,"availabilityStatus":"OPEN"} for i in range(1,17)]
            rows[10].pop("distanceKm")
            rows[11]["availabilityStatus"]="CLOSED"
            return {"restaurants":rows[:8] if offset==0 else rows[8:],"hasMore":offset==0,"nextOffset":"8" if offset==0 else None}
        rid=args.get("restaurantIdOfAddedItem")
        if rid:
            return {"items":[menu(rid),menu(rid,"Masala Dosai",rid+"02")],"hasMore":False}
        return {"items":[menu(str(i)) for i in range(1,9) if offset==0] if offset==0 else [menu(str(i)) for i in range(9,17)],"hasMore":offset==0,"nextOffset":8 if offset==0 else None}


class QuoteProvider:
    def __init__(self, coupons=True, change=False, clear_fail=False):
        self.basket={"old":True};self.names=[];self.calls=0;self.coupons=coupons;self.change=change;self.clear_fail=clear_fail
    def call(self,name,args):
        self.calls+=1;self.names.append(name)
        if name=="flush_food_cart":
            if self.clear_fail:raise AppError("Clear failed",502)
            self.basket=None;return {"success":True}
        if name=="get_food_cart":return {"success":True,"data":copy.deepcopy(self.basket)}
        if name=="search_menu":return {"items":[menu("1", "Masala Dosai" if self.change else "Plain Dosai")],"hasMore":False}
        if name=="update_food_cart":
            self.basket={"cart_id":42,"items":[{"menu_item_id":"101","quantity":1,"name":"Plain Dosai","in_stock":1,"addons":[],"variants":[]}],"pricing":{"item_total":115,"delivery_charge":0,"taxes_and_charges":31.08,"to_pay":146},"offers":{"coupon_applied":None,"coupon_discount":0}}
            return {"success":True}
        if name=="fetch_food_coupons":return {"coupon_sections":[{"title":"More offers","coupons":[{"title":"SAVE20","applicable":True},{"code":"SAVE30","applicabilityStatus":"APPLICABLE"}]}] if self.coupons else [],"summary":{"filter_applied":"COD only"}}
        if name=="apply_food_coupon":
            assert args["cartId"]=="42"
            d=20 if args["couponCode"]=="SAVE20" else 30
            self.basket["offers"]={"coupon_applied":args["couponCode"],"coupon_discount":d};self.basket["pricing"]["to_pay"]=146-d
            return {"success":True}
        if name=="get_addresses":return {"addresses":[{"id":"addr"}],"pagination":{"hasMore":False}}
        raise AssertionError(name)


class LiveTests(unittest.TestCase):
    def test_strict_recipe_matching(self):
        for name in ["Dosa","Dosai","plain DOSAI"]:self.assertTrue(exact_dish(name,"Plain Dosa"))
        for name in ["Masala Dosa","Ghee Dosa","Onion Dosa","Rava Dosa","Plain Dosa Combo","Plain Dosa 2 pcs","Cheese Dosa"]:self.assertFalse(exact_dish(name,"Plain Dosa"))
        self.assertFalse(exact_dish("Chicken Biryani 1 kg","Chicken Biryani"))
    def test_only_known_optional_addons_are_allowed(self):
        self.assertIsNone(customization_reason({"hasAddons":True,"addons":[{"minAddons":0}]}))
        for item in [{"hasAddons":True},{"hasAddons":True,"addons":[]},{"hasAddons":True,"addons":[{}]},{"addons":[{"minAddons":1}]},{"hasVariants":True}]:self.assertIsNotNone(customization_reason(item))
    def test_every_page_and_strict_distance(self):
        run=new_search("Plain Dosa",1,"addr");provider=DiscoveryProvider()
        for _ in range(300):
            step(run,provider)
            if run["done"]:break
        self.assertTrue(run["done"])
        ids={row["restaurant_id"] for row in run["offers"].values()}
        self.assertEqual(ids,{str(i) for i in range(1,14)}-{"11","12"})
        self.assertGreater(len(ids),5)
        self.assertTrue(all(o["distance_km"]<7 and exact_dish(o["dish"],"Plain Dosa") for o in run["offers"].values()))
        self.assertTrue(run["partial"])
    def test_repeated_cursor_is_incomplete(self):
        class Repeat:
            def call(self,name,args):return {"items":[],"restaurants":[],"hasMore":True,"nextOffset":0}
        run=new_search("Idli",1,"addr")
        for _ in range(5):step(run,Repeat())
        self.assertTrue(run["done"] and run["partial"])
    def test_dynamic_coupons_full_bill_and_cleanup(self):
        offer=menu_offers({"items":[menu("1")]},1)[0];offer["distance_km"]=2
        client=QuoteProvider();r=quote_offer(client,offer,"addr",True)
        self.assertEqual(r["offer"]["coupon"],"SAVE30");self.assertEqual(r["offer"]["total"],116)
        self.assertEqual(r["offer"]["distance_km"],2)
        self.assertIsNone(client.basket)
        self.assertEqual(r["_bill_diagnostic"]["initial_cart"]["pricing"]["to_pay"],146)
        self.assertLessEqual(client.calls,36)
    def test_cheapest_40_distinct_restaurants_after_filtering(self):
        class Provider:
            def call(self, name, args):
                self.assert_unscoped(args)
                if name == "search_restaurants":
                    return {"restaurants":[{"id":str(i),"name":f"Restaurant {i}","distanceKm":7 if i == 60 else 2,"availabilityStatus":"CLOSED" if i == 59 else "OPEN"} for i in range(1,61)],"hasMore":False}
                return {"items":[menu(str(i),price=1000-i) for i in range(1,61)] + [menu("45",item_id="cheap45",price=10),menu("58","Masala Dosa",item_id="wrong",price=1),menu("57",item_id="unknown",price=None)],"hasMore":False}
            def assert_unscoped(self,args):
                assert "restaurantIdOfAddedItem" not in args, "Discovery must never crawl every restaurant menu"
        run=new_search("Plain Dosa",2,"addr")
        for _ in range(80):
            step(run,Provider())
            if run["done"]:break
        rows=list(run["offers"].values())
        self.assertEqual(len(rows),40)
        self.assertEqual(len({r["restaurant_id"] for r in rows}),40)
        self.assertEqual(rows[0]["item_id"],"cheap45")
        expected=["45"]+[str(i) for i in range(58,0,-1) if i != 45][:39]
        self.assertEqual([r["restaurant_id"] for r in rows],expected)
        self.assertEqual([r["item_total"] for r in rows],sorted(r["item_total"] for r in rows))
        self.assertTrue(all(r["menu_offset"]==0 for r in rows))
        self.assertLess(run["pages"],10)

    def test_endless_provider_has_firm_page_limit(self):
        class Endless:
            def call(self,name,args):
                offset=args.get("offset",0)
                if name=="search_restaurants":
                    return {"restaurants":[{"id":str(offset+1),"name":f"Restaurant {offset+1}","distanceKm":2,"availabilityStatus":"OPEN"}],"hasMore":True,"nextOffset":offset+1}
                return {"items":[menu(str(offset+1),price=100-offset)],"hasMore":True,"nextOffset":offset+1}
        run=new_search("Plain Dosa",1,"addr")
        for _ in range(90):
            step(run,Endless())
            if run["done"]:break
        self.assertTrue(run["done"])
        self.assertLessEqual(run["pages"],64)
        self.assertLessEqual(run["menu_pages"],20)
        self.assertTrue(run["partial"])

    def test_time_limit_preserves_collected_known_matches(self):
        run=new_search("Plain Dosa",1,"addr")
        provider=DiscoveryProvider()
        step(run,provider);step(run,provider)
        run["started_at"]-=130
        step(run,provider)
        self.assertTrue(run["done"])
        self.assertTrue(run["offers"])
        self.assertEqual(run["stop_reason"],"discovery_limit")

    def test_best_and_more_coupons_checked_without_optional_filter(self):
        class Provider(QuoteProvider):
            def call(self,name,args):
                if name=="fetch_food_coupons":
                    self.calls+=1
                    return {"coupon_sections":[{"title":"Best coupon","coupons":[{"title":"SAVE20","applicable":True}]},{"title":"More offers","coupons":[{"couponCode":"SAVE30","applicabilityStatus":"APPLICABLE"}]}],"summary":{}}
                return super().call(name,args)
        provider=Provider()
        result=quote_offer(provider,menu_offers({"items":[menu("1")]},1)[0],"addr",True)
        self.assertEqual(result["offer"]["coupon"],"SAVE30")
        self.assertEqual(result["offer"]["coupon_checks"]["codes_tested"],["SAVE20","SAVE30"])
        self.assertEqual(result["offer"]["status"],"verified")

    def test_cart_suggested_coupon_is_attempted_and_verified(self):
        class Provider(QuoteProvider):
            def call(self,name,args):
                response=super().call(name,args)
                if name=="update_food_cart":self.basket["offers"]["coupon_applied"]="SAVE30"
                return response
        provider=Provider(coupons=False)
        result=quote_offer(provider,menu_offers({"items":[menu("1")]},1)[0],"addr",True)
        self.assertEqual(result["offer"]["coupon"],"SAVE30")
        self.assertEqual(result["offer"]["discount"],30)
        self.assertIsNone(provider.basket)

    def test_coupon_lookup_failure_is_not_a_finished_deal(self):
        class Provider(QuoteProvider):
            def call(self,name,args):
                if name=="fetch_food_coupons":raise AppError("Unavailable",502)
                return super().call(name,args)
        provider=Provider()
        result=quote_offer(provider,menu_offers({"items":[menu("1")]},1)[0],"addr",True)
        self.assertEqual(result["offer"]["status"],"coupon_unverified")
        self.assertTrue(result["offer"]["bill_verified"])
        self.assertEqual(result["offer"]["total"],146)
        self.assertIsNone(provider.basket)

    def test_coupon_not_actually_applied_is_not_verified(self):
        class Provider(QuoteProvider):
            def call(self,name,args):
                if name=="apply_food_coupon":
                    self.calls+=1
                    return {"success":True}
                return super().call(name,args)
        provider=Provider()
        result=quote_offer(provider,menu_offers({"items":[menu("1")]},1)[0],"addr",True)
        self.assertEqual(result["offer"]["status"],"coupon_unverified")
        self.assertEqual(result["offer"]["discount"],0)
        self.assertEqual(result["offer"]["coupon_checks"]["failed_codes"],["SAVE20","SAVE30"])
        self.assertIsNone(provider.basket)

    def test_payment_and_ineligible_codes_are_excluded(self):
        sections=[{"title":"Best coupon","coupons":[{"code":"YES20","applicable":True},{"code":"NO30","applicable":False},{"code":"BANK40","applicable":True,"description":"Use a credit card"}]},{"title":"Payment offers","coupons":[{"code":"PAY50","applicable":True}]}]
        self.assertEqual(coupon_candidates(sections,False)[0],["YES20"])

    def test_coupon_budget_is_disclosed_and_cleanup_reserved(self):
        class Provider(QuoteProvider):
            def call(self,name,args):
                if name=="fetch_food_coupons":
                    self.calls+=1
                    return {"coupon_sections":[{"title":"Best coupons","coupons":[{"code":f"SAVE{i}","applicable":True} for i in range(30)]}]}
                return super().call(name,args)
        provider=Provider()
        result=quote_offer(provider,menu_offers({"items":[menu("1")]},1)[0],"addr",True)
        self.assertLessEqual(provider.calls,36)
        self.assertFalse(result["offer"]["coupon_checks"]["all_candidates_tested"])
        self.assertEqual(result["offer"]["status"],"coupon_unverified")
        self.assertIsNone(provider.basket)

    def test_mcp_session_reuse_skips_repeated_initialization(self):
        import httpx,json
        from swiggy import SwiggyClient
        messages=[]
        def handler(request):
            self.assertEqual(request.headers["Mcp-Session-Id"],"session-test")
            message=json.loads(request.content);messages.append(message)
            return httpx.Response(200,json={"jsonrpc":"2.0","id":message["id"],"result":{"structuredContent":{"items":[]}}})
        with SwiggyClient("fake",transport=httpx.MockTransport(handler)) as client:
            client.resume_session({"id":"session-test","protocol":"2025-03-26"})
            self.assertEqual(client.call("search_menu",{"addressId":"addr","query":"dosa"}),{"items":[]})
            self.assertEqual(client.session_info()["id"],"session-test")
        self.assertEqual([m["method"] for m in messages],["tools/call"])
    def test_empty_coupon_response_does_not_invent_discount(self):
        offer=menu_offers({"items":[menu("1")]},1)[0]
        client=QuoteProvider(coupons=False);r=quote_offer(client,offer,"addr",True)
        self.assertEqual(r["offer"]["discount"],0);self.assertIsNone(r["offer"]["coupon"])
        self.assertEqual(r["offer"]["coupon_checks"]["availability"],"none_returned")
        self.assertNotIn("apply_food_coupon",client.names)
    def test_changed_recipe_is_never_added(self):
        offer=menu_offers({"items":[menu("1")]},1)[0];client=QuoteProvider(change=True)
        with self.assertRaises(AppError):quote_offer(client,offer,"addr",True)
        self.assertNotIn("update_food_cart",client.names)
    def test_failed_reset_stops_before_add(self):
        client=QuoteProvider(clear_fail=True)
        with self.assertRaises(AppError):quote_offer(client,menu_offers({"items":[menu("1")]},1)[0],"addr",True)
        self.assertEqual(client.names,["flush_food_cart", "get_food_cart"])
        self.assertNotIn("update_food_cart", client.names)
    def test_no_code_from_opaque_id(self):
        codes,_=coupon_candidates([{"title":"Best coupon","coupons":[{"id":"opaque-id","title":"20 percent off","applicable":True}]}],True)
        self.assertEqual(codes,[])
    def test_ai_missing_key_is_honest(self):
        from ai_dishes import suggest_dishes
        with patch.dict(os.environ,{"GEMINI_API_KEY":"","GEMINI_MODEL":""}):
            result=suggest_dishes("Dosa")
        self.assertEqual(result["source"],"manual");self.assertIn("Plain Dosa",result["choices"])
    def test_ai_returns_suggestions_from_mock_provider(self):
        from ai_dishes import suggest_dishes
        class Response:
            def raise_for_status(self):pass
            def json(self):return {"candidates":[{"content":{"parts":[{"text":'{"question":"Which?","choices":["Plain Dosa","Masala Dosa"]}'}]}}]}
        with patch.dict(os.environ,{"GEMINI_API_KEY":"test-key","GEMINI_MODEL":"test-model"}), patch("ai_dishes.httpx.post",return_value=Response()) as post:
            result=suggest_dishes("Dosa")
        self.assertEqual(result["source"],"gemini")
        import json
        sent=json.loads(post.call_args.kwargs["json"]["contents"][0]["parts"][0]["text"])
        self.assertEqual(sent,{"food_query":"Dosa"})

    def test_api_rejects_demo_wrong_search_and_distance(self):
        import app as module
        module.app.config["TESTING"]=True
        client=module.app.test_client()
        with client.session_transaction() as session:session["sid"]="test"
        module.states["test"]={"csrf":"csrf","token":"fake","expires":time.time()+300}
        headers={"Origin":module.ORIGIN,"X-CSRF-Token":"csrf"}
        self.assertEqual(client.post("/api/search",json={"mode":"demo"},headers=headers).status_code,400)
        run=new_search("Plain Dosa",1,"addr");run["done"]=True
        offer=menu_offers({"items":[menu("1")]},1)[0];offer["distance_km"]=7;run["offers"]={offer["id"]:offer}
        module.states["test"]["search"]=run
        req={"search_id":run["id"],"offer_id":offer["id"],"consent":True,"replace_existing_cart":True}
        self.assertEqual(client.post("/api/quote",json=req,headers=headers).status_code,409)
        req["search_id"]="wrong"
        self.assertEqual(client.post("/api/quote",json=req,headers=headers).status_code,409)
        self.assertNotIn(b"Demo",client.get("/",base_url=module.ORIGIN).data)

if __name__=="__main__":unittest.main()
