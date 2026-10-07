"""Comparison logic; prices are never inferred from discount advertising."""
import math
import re
from copy import deepcopy
from swiggy import AppError


def number(value):
    return float(value) if type(value) in (int, float) and math.isfinite(value) and value >= 0 else None


def data_of(payload):
    value = payload.get("data", payload)
    if not isinstance(value, dict):
        raise AppError("Swiggy returned an unexpected data format.", 502)
    return value


def parse_cart(payload, allow_out_of_stock=False):
    """Accept the documented envelope and observed raw gateway responses."""
    if not isinstance(payload, dict):
        raise AppError("Cart response was not recognized.", 502)
    confirmed = False
    current = payload
    for _ in range(3):
        if current.get("success") is False or current.get("successful") is False:
            raise AppError("Cart state could not be confirmed.", 502)
        if "statusCode" in current and (type(current["statusCode"]) is not int or current["statusCode"] != 0):
            # Observed statuses 6 and 8 include populated unavailable baskets.
            # Recognize it only for an authorized clear or our own cleanup;
            # it must never be accepted as a purchasable/verified bill.
            value = current.get("data")
            stock_basket = (type(current.get("statusCode")) is int and current["statusCode"] in (6, 8)
                            and isinstance(value, dict) and value.get("result") == "success"
                            and isinstance(value.get("items"), list) and bool(value["items"])
                            and all(isinstance(item, dict) for item in value["items"])
                            and (current["statusCode"] == 6 or any(item.get("in_stock") in (False, 0) for item in value["items"])))
            if not stock_basket:
                raise AppError("Cart state could not be confirmed. No new item was added.", 502)
            if not allow_out_of_stock:
                raise AppError("Swiggy reports a closed restaurant or out-of-stock cart items; this bill cannot be confirmed.", 409)
            confirmed = True
        confirmed = confirmed or current.get("success") is True or current.get("successful") is True
        status_ok = type(current.get("statusCode")) is int and current["statusCode"] == 0
        if "data" not in current:
            raise AppError("Cart response is missing data. Nothing was cleared.", 502)
        value = current["data"]
        if value is None:
            # The raw empty-cart gateway format identifies this as CART.
            if confirmed or (status_ok and current.get("statusMessage") == "CART"):
                return None
            raise AppError("Empty cart state could not be confirmed. Nothing was cleared.", 502)
        if not isinstance(value, dict):
            raise AppError("Cart response was not recognized. Nothing was cleared.", 502)
        if isinstance(value.get("items"), list):
            if confirmed or (status_ok and value.get("result") == "success"):
                return value if value["items"] else None
            raise AppError("Cart state could not be confirmed.", 502)
        current = value
    raise AppError("Cart response was not recognized. Review your cart in Swiggy.", 502)


def exact_basket(cart, offer):
    if not isinstance(cart, dict):
        return False
    restaurant = cart.get("restaurant") or {}
    if not isinstance(restaurant, dict):
        return False
    actual_restaurant_id = restaurant.get("id") or cart.get("restaurant_id")
    if actual_restaurant_id is not None and str(actual_restaurant_id) != offer["restaurant_id"]:
        return False
    # The observed gateway response omits restaurant.id. Match the exact
    # menu item ID from the scoped restaurant request instead of a subtitle.
    items = cart.get("items")
    if not isinstance(items, list) or len(items) != 1 or not isinstance(items[0], dict):
        return False
    item = items[0]
    return (str(item.get("menu_item_id", "")) == offer["item_id"]
            and type(item.get("quantity")) is int and item["quantity"] == offer["quantity"]
            and all(item.get(key) in (None, []) for key in ("variants", "variations", "variantsV2", "addons")))


def require_exact(cart, offer):
    if not exact_basket(cart, offer):
        raise AppError("The cart changed unexpectedly. Nothing was cleared; review your Swiggy cart.", 409)


def read_bill(offer, cart):
    if any(item.get("in_stock") in (False, 0) for item in cart.get("items", []) if isinstance(item, dict)):
        raise AppError("This dish is out of stock; its full bill cannot be confirmed.", 409)
    pricing, coupons = cart.get("pricing") or {}, cart.get("offers") or {}
    total = number(pricing.get("to_pay"))
    if total is None:
        raise AppError("Swiggy did not return a payable total.", 502)
    if any(number(pricing.get(key)) is None for key in ("item_total", "delivery_charge", "taxes_and_charges")):
        raise AppError("Swiggy did not return the complete delivery and tax breakdown. This bill cannot be confirmed.", 502)
    discount = number(coupons.get("coupon_discount")) or 0
    code = coupons.get("coupon_applied")
    confirmed = bool(isinstance(code, str) and code and discount > 0)
    return {**offer, "status": "verified", "total": total,
            "item_total": number(pricing.get("item_total")),
            "delivery": number(pricing.get("delivery_charge")),
            "other_charges": number(pricing.get("taxes_and_charges")),
            "discount": discount if confirmed else 0, "coupon": code if confirmed else None,
            "free_delivery_applied": coupons.get("free_delivery_applied") is True
                and number(pricing.get("delivery_charge")) == 0,
            "delivery_charge_strikeoff": number(pricing.get("delivery_charge_strikeoff"))}


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


def coupon_candidates(sections, cod_filtered):
    """Prefer Best coupon cards; only use explicit codes or code-only titles."""
    valid_sections = [section for section in sections if isinstance(section, dict)]
    best_sections = [section for section in valid_sections
                     if "best" in (str(section.get("title", "")) + " " + str(section.get("type", ""))).lower()]
    chosen = best_sections
    codes = []
    for section in chosen:
        cards = section.get("coupons", [])
        if not isinstance(cards, list):
            continue
        for card in cards:
            if not isinstance(card, dict):
                continue
            status = str(card.get("applicabilityStatus", "")).upper()
            if card.get("applicable") is False or status == "NOT_APPLICABLE":
                continue
            if not cod_filtered or not (card.get("applicable") is True or status in {"APPLICABLE", "APPLIED"}):
                continue
            code = card.get("couponCode") or card.get("coupon_code") or card.get("code")
            # Some documented cards expose their coupon code as the title.
            if not isinstance(code, str) or not code.strip():
                title = card.get("title")
                code = title if isinstance(title, str) and re.fullmatch(r"[A-Z][A-Z0-9_-]{2,39}", title) else None
            if isinstance(code, str) and code.strip() and code.strip() not in codes:
                codes.append(code.strip())
    return codes, bool(best_sections)


def quote_offer(client, offer, address_id, replace_existing_cart=False):
    """Caller holds the lock; existing-cart replacement needs explicit consent."""
    offer = deepcopy(offer)
    cart_args = {"addressId": address_id, "restaurantName": offer["restaurant"]}
    attempted, empty, best, failure = False, False, None, None
    safe_to_continue = False
    existing_cart_cleared = False
    try:
        if replace_existing_cart is True:
            # Cart removal has been explicitly authorized. Reset first so
            # a closed restaurant/out-of-stock old basket cannot block it.
            # A failure or unknown empty-state response stops before add.
            client.call("flush_food_cart", {})
            if parse_cart(client.call("get_food_cart", cart_args)) is not None:
                raise AppError("Swiggy did not confirm an empty cart after clearing. No comparison item was added.", 409)
            existing_cart_cleared = True
        elif parse_cart(client.call("get_food_cart", cart_args)) is not None:
            raise AppError("Your Swiggy cart has items. Allow cart replacement before comparing full bills.", 409)
        safe_to_continue = True
        refreshed = menu_offers(client.call("search_menu", {"addressId": address_id,
            "query": offer["dish"], "restaurantIdOfAddedItem": offer["restaurant_id"]}),
            offer["quantity"], restaurant_id=offer["restaurant_id"], restaurant_name=offer["restaurant"])
        exact = next((row for row in refreshed if row["id"] == offer["id"]), None)
        if not exact or exact["customized"]:
            raise AppError("This dish is unavailable or requires a size/add-on choice. Its bill was not checked.", 409)
        offer = exact
        safe_to_continue = False
        # Recheck after the menu call to avoid overwriting an intervening cart edit.
        if parse_cart(client.call("get_food_cart", cart_args)) is not None:
            raise AppError("Your cart changed before the check. Existing items were preserved.", 409)
        attempted = True
        client.call("update_food_cart", {**cart_args, "restaurantId": offer["restaurant_id"],
            "cartItems": [{"menu_item_id": offer["item_id"], "quantity": offer["quantity"]}]})
        cart = parse_cart(client.call("get_food_cart", cart_args))
        require_exact(cart, offer)
        best = read_bill(offer, cart)
        coupon_warning = None
        try:
            coupons = data_of(client.call("fetch_food_coupons", {"addressId": address_id, "restaurantId": offer["restaurant_id"]}))
            sections = coupons.get("coupon_sections")
            if not isinstance(sections, list):
                raise AppError("Swiggy's coupon response was not recognized.", 502)
        except AppError as exc:
            if exc.status in (401, 429):
                raise
            # A read-only coupon lookup failure does not erase a full cart bill.
            coupons, sections = {}, []
            coupon_warning = "Coupon lookup could not be completed; additional savings are unverified."
        cards = [c for section in sections if isinstance(section, dict)
                 for c in section.get("coupons", []) if isinstance(c, dict)]
        filter_text = str((coupons.get("summary") or {}).get("filter_applied", "")).lower()
        cod = "cod" in filter_text or "cash on delivery" in filter_text
        candidates, best_section_found = coupon_candidates(sections, cod)
        checked, confirmed_codes = [], []
        stopped_early = False
        for code in candidates[:6]:
            # Leave enough RPC capacity for the pre-check, application,
            # verification and three cleanup calls. Never retry a mutation.
            if getattr(client, "calls", 0) + 6 > 36:
                stopped_early = True
                break
            require_exact(parse_cart(client.call("get_food_cart", cart_args)), offer)
            apply_error = None
            try:
                client.call("apply_food_coupon", {"addressId": address_id, "couponCode": code})
            except AppError as exc:
                if exc.status in (401, 429):
                    raise
                apply_error = exc
            # Read actual state even when application reports an error.
            cart = parse_cart(client.call("get_food_cart", cart_args))
            require_exact(cart, offer)
            bill = read_bill(offer, cart)
            checked.append(code)
            if bill["coupon"] == code and bill["discount"] > 0:
                confirmed_codes.append(code)
            if (bill["discount"], -bill["total"]) > (best["discount"], -best["total"]):
                best = bill
            if apply_error is not None:
                # Preserve the verified bill and stop after an uncertain call.
                stopped_early = True
                break
        best["coupon_checks"] = {
            "codes_tested": checked,
            "confirmed_codes": confirmed_codes,
            "best_section_returned": best_section_found,
            "candidate_count": len(candidates),
            "all_candidates_tested": not stopped_early and len(checked) == len(candidates),
        }
        if best.get("coupon") and best["discount"] > 0:
            best["coupon_note"] = (
                f"{best['coupon']} confirmed: Rs {best['discount']:.2f} off. "
                f"Largest verified coupon discount among {len(checked)} checks; payable total confirmed by Swiggy."
            )
        else:
            best["coupon_note"] = (
                f"No coupon discount confirmed after {len(checked)} checks. "
                f"Swiggy returned {len(cards)} COD coupon cards."
            )
        if coupon_warning:
            best["coupon_note"] = coupon_warning
        if not best_section_found:
            best["coupon_note"] += " The MCP response did not expose a Best coupon section."
        elif not candidates:
            best["coupon_note"] += " The Best coupon section contained no usable eligible coupon codes."
        if not best["coupon_checks"]["all_candidates_tested"]:
            best["coupon_note"] += " Some candidates were not tested; maximum savings are unverified."
        if best.get("free_delivery_applied"):
            best["coupon_note"] += " Free delivery offer applied."
    except Exception as exc:
        failure = exc
    finally:
        if attempted:
            # Cleanup has its own bounded deadline, even when the main request failed.
            if hasattr(client, "deadline"):
                import time
                client.deadline = time.monotonic() + 80
            try:
                cart = parse_cart(client.call("get_food_cart", cart_args), allow_out_of_stock=True)
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
        if isinstance(failure, AppError):
            failure.safe_to_continue = (empty if attempted else safe_to_continue) and failure.status not in (401, 429)
        raise failure
    return {"offer": best, "cart_empty": empty, "existing_cart_cleared": existing_cart_cleared}


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
