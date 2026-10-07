"""Comparison logic; prices are never inferred from discount advertising."""
import math
from copy import deepcopy
from swiggy import AppError


def number(value):
    return float(value) if type(value) in (int, float) and math.isfinite(value) and value >= 0 else None


def data_of(payload):
    value = payload.get("data", payload)
    if not isinstance(value, dict):
        raise AppError("Swiggy returned an unexpected data format.", 502)
    return value


def parse_cart(payload):
    if payload.get("success") is not True and payload.get("successful") is not True:
        raise AppError("Cart state could not be confirmed.", 502)
    if "data" not in payload:
        raise AppError("Cart response is missing data. Nothing was cleared.", 502)
    data = payload["data"]
    cart = data.get("data", data) if isinstance(data, dict) else data
    if cart is None:
        return None
    if isinstance(cart, dict) and isinstance(cart.get("items"), list):
        return None if not cart["items"] else cart
    raise AppError("Cart response was not recognized. Review your cart in Swiggy.", 502)


def exact_basket(cart, offer):
    if not isinstance(cart, dict) or str((cart.get("restaurant") or {}).get("id", "")) != offer["restaurant_id"]:
        return False
    items = cart.get("items")
    if not isinstance(items, list) or len(items) != 1:
        return False
    item = items[0]
    return (str(item.get("menu_item_id", "")) == offer["item_id"]
            and type(item.get("quantity")) is int and item["quantity"] == offer["quantity"]
            and all(item.get(key) in (None, []) for key in ("variants", "variations", "variantsV2", "addons")))


def require_exact(cart, offer):
    if not exact_basket(cart, offer):
        raise AppError("The cart changed unexpectedly. Nothing was cleared; review your Swiggy cart.", 409)


def read_bill(offer, cart):
    pricing, coupons = cart.get("pricing") or {}, cart.get("offers") or {}
    total = number(pricing.get("to_pay"))
    if total is None:
        raise AppError("Swiggy did not return a payable total.", 502)
    discount = number(coupons.get("coupon_discount")) or 0
    code = coupons.get("coupon_applied")
    confirmed = bool(isinstance(code, str) and code and discount > 0)
    return {**offer, "status": "verified", "total": total,
            "item_total": number(pricing.get("item_total")),
            "delivery": number(pricing.get("delivery_charge")),
            "other_charges": number(pricing.get("taxes_and_charges")),
            "discount": discount if confirmed else 0, "coupon": code if confirmed else None}


def menu_offers(payload, quantity, restaurant_id=None, restaurant_name=None):
    items = data_of(payload).get("items")
    if not isinstance(items, list):
        raise AppError("Menu response format was not recognized.", 502)
    results = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        # Scoped menu results can omit the per-item restaurant ID.
        # Only use a fallback explicitly supplied by the scoped caller.
        resolved_restaurant_id = item.get("restaurant_id") or restaurant_id
        if not item.get("menu_item_id") or not resolved_restaurant_id or item.get("inStock") in (False, 0):
            continue
        if restaurant_id is not None and str(resolved_restaurant_id) != str(restaurant_id):
            continue
        price = number(item.get("price"))
        identifier = str(resolved_restaurant_id) + ":" + str(item["menu_item_id"])
        results[identifier] = {"id": identifier, "item_id": str(item["menu_item_id"]),
            "restaurant_id": str(resolved_restaurant_id), "restaurant": str(item.get("restaurant_name") or restaurant_name or "Swiggy restaurant"),
            "dish": str(item.get("name") or "Menu item"), "quantity": quantity,
            "portion": str(item.get("portion_size") or "Portion size unknown"),
            "unit_price": price, "item_total": round(price * quantity, 2) if price is not None else None,
            "delivery": None, "other_charges": None, "discount": 0, "coupon": None, "total": None,
            "status": "estimate", "rating": str(item.get("rating") or ""),
            "customized": bool(item.get("hasVariants") or item.get("hasAddons") or item.get("variations") or item.get("variantsV2") or item.get("addons"))}
    return sorted(results.values(), key=lambda row: row["item_total"] if row["item_total"] is not None else math.inf)


def check_address(client, address_id):
    for page in range(1, 6):
        data = data_of(client.call("get_addresses", {"page": page, "pageSize": 10}))
        addresses = data.get("addresses")
        if not isinstance(addresses, list):
            raise AppError("Could not verify your saved delivery address.", 502)
        if any(a.get("id") == address_id for a in addresses):
            return
        if not (data.get("pagination") or {}).get("hasMore"):
            break
    raise AppError("Choose an address saved in your own Swiggy account.", 403)


def quote_offer(client, offer, address_id):
    """Caller holds the cart lock. Never clears an existing or unexpected basket."""
    offer = deepcopy(offer)
    cart_args = {"addressId": address_id, "restaurantName": offer["restaurant"]}
    attempted, empty, best, failure = False, False, None, None
    try:
        if parse_cart(client.call("get_food_cart", cart_args)) is not None:
            raise AppError("Your Swiggy cart has items. Empty it yourself before a full-bill comparison.", 409)
        refreshed = menu_offers(client.call("search_menu", {"addressId": address_id,
            "query": offer["dish"], "restaurantIdOfAddedItem": offer["restaurant_id"]}),
            offer["quantity"], restaurant_id=offer["restaurant_id"], restaurant_name=offer["restaurant"])
        exact = next((row for row in refreshed if row["id"] == offer["id"]), None)
        if not exact or exact["customized"]:
            raise AppError("This dish is unavailable or requires a size/add-on choice. Its bill was not checked.", 409)
        offer = exact
        # Recheck after the menu call to avoid overwriting an intervening cart edit.
        if parse_cart(client.call("get_food_cart", cart_args)) is not None:
            raise AppError("Your cart changed before the check. Existing items were preserved.", 409)
        attempted = True
        client.call("update_food_cart", {**cart_args, "restaurantId": offer["restaurant_id"],
            "cartItems": [{"menu_item_id": offer["item_id"], "quantity": offer["quantity"]}]})
        cart = parse_cart(client.call("get_food_cart", cart_args))
        require_exact(cart, offer)
        best = read_bill(offer, cart)
        coupons = data_of(client.call("fetch_food_coupons", {"addressId": address_id, "restaurantId": offer["restaurant_id"]}))
        sections = coupons.get("coupon_sections")
        if not isinstance(sections, list):
            raise AppError("Coupon response was not recognized. The test cart will be cleaned up.", 502)
        cards = [c for section in sections for c in section.get("coupons", []) if isinstance(c, dict)]
        filter_text = str((coupons.get("summary") or {}).get("filter_applied", "")).lower()
        cod = "cod" in filter_text or "cash on delivery" in filter_text
        # An opaque offer id is not assumed to be an applicable coupon code.
        candidates = [c for c in cards if isinstance(c.get("couponCode") or c.get("code"), str)
            and (c.get("applicable") is True or c.get("applicabilityStatus") == "APPLICABLE") and cod][:2]
        for coupon in candidates:
            require_exact(parse_cart(client.call("get_food_cart", cart_args)), offer)
            client.call("apply_food_coupon", {"addressId": address_id, "couponCode": coupon.get("couponCode") or coupon["code"]})
            cart = parse_cart(client.call("get_food_cart", cart_args))
            require_exact(cart, offer)
            bill = read_bill(offer, cart)
            if bill["total"] < best["total"]:
                best = bill
        best["coupon_note"] = ("No coupons returned by Swiggy." if not cards else
            f"{len(cards)} offers returned; {len(candidates)} explicit COD coupon codes checked. Other savings are unverified.")
    except Exception as exc:
        failure = exc
    finally:
        if attempted:
            # Cleanup has its own bounded deadline, even when the main request failed.
            if hasattr(client, "deadline"):
                import time
                client.deadline = time.monotonic() + 80
            try:
                cart = parse_cart(client.call("get_food_cart", cart_args))
                if cart is None:
                    empty = True
                else:
                    require_exact(cart, offer)
                    client.call("flush_food_cart", {})
                    empty = parse_cart(client.call("get_food_cart", cart_args)) is None
            except Exception:
                empty = False
    if attempted and not empty:
        raise AppError("Cart cleanup could not be confirmed. Stop and check your cart in Swiggy before continuing.", 409)
    if failure:
        raise failure
    return {"offer": best, "cart_empty": empty}


def sample_offers(dish, quantity, area):
    base = 125 if "roll" in dish.lower() else 285 if "pizza" in dish.lower() else 230
    rows = []
    for i, restaurant in enumerate(["Copper Pot Kitchen", "The Rice Room", "Pepper & Plate", "Kitchen No. 7"]):
        unit = base + [0, 19, 38, 12][i]
        subtotal = unit * quantity
        delivery = [35, 25, 39, 45][i] + (8 if "Bengaluru" in area else 0)
        charges = round(subtotal * 0.05) + [16, 22, 28, 24][i]
        discount = 80 if i == 0 and subtotal >= 399 else min(100, math.floor(subtotal * 0.2)) if i == 1 and subtotal >= 499 else 0
        rows.append({"id": f"demo-{i}", "restaurant": restaurant, "dish": dish, "quantity": quantity,
            "portion": "Regular · sample portion", "unit_price": unit, "item_total": subtotal,
            "delivery": delivery, "other_charges": charges, "discount": discount,
            "coupon": ("MEAL80" if i == 0 else "TASTE20") if discount else None,
            "total": subtotal + delivery + charges - discount, "status": "demo", "customized": False,
            "rating": ["4.5", "4.3", "4.6", "4.2"][i], "coupon_note": "Fictional demonstration offer."})
    return sorted(rows, key=lambda row: row["total"])
