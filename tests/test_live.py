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
            self.basket={"cart_id":42,"items":[{"menu_item_id":"101","quantity":1,"name":"Plain Dosai","in_stock":1,"addons":[],"variants":[]}],"pricing":{"item_total":115,"delivery_charge":0,"taxes_and_charges":31.08,"to_pay":146},"offers":{"coupon_applied":"SUGGESTED","coupon_discount":0}}
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
        self.assertEqual(client.names,["flush_food_cart"])
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
