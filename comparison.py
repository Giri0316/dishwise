"""Comparison logic; prices are never inferred from discount advertising."""
import math
import re
from copy import deepcopy
from swiggy import AppError
from matching import customization_reason, exact_dish
from coupons import coupon_plan, coupon_candidates


def number(value):
    return float(value) if type(value) in (int, float) and math.isfinite(value) and value >= 0 else None


def data_of(payload):
    value = payload.get("data", payload)
    if not isinstance(value, dict):
        raise AppError("Swiggy returned an unexpected data format.", 502)
    return value


def parse_cart(payload, allow_out_of_stock=False):
    """Separate basket identity from whether its current bill is usable.

    `allow_out_of_stock` is used ONLY for cleanup: explicit item identities
    can still be inspected under a service error. Such responses never prove
    emptiness and are never used as price quotes.
    """
    current, confirmed, failed = payload, False, False
    for _ in range(7):
        if not isinstance(current, dict):
            break
        failed = failed or current.get("success") is False or current.get("successful") is False
        code = current.get("statusCode")
        if code is not None and (type(code) is not int or code != 0):
            failed = True
        confirmed = (confirmed or current.get("success") is True
                     or current.get("successful") is True or current.get("result") == "success"
                     or (type(code) is int and code == 0))
        if "items" in current:
            items = current["items"]
            if not isinstance(items, list) or any(not isinstance(i, dict) for i in items):
                break
            if not items:
                count = current.get("item_count")
                if confirmed and not failed and count in (None, 0):
                    return None
                break
            if failed and not allow_out_of_stock:
                raise AppError("Swiggy returned an unavailable cart; its bill cannot be verified.", 409,
                               code="cart_unavailable")
            if confirmed or (allow_out_of_stock and failed):
                return current
            break
        key = "response" if isinstance(current.get("response"), dict) else "data"
        if key not in current:
            break
        value = current[key]
        if value is None:
            if confirmed and not failed:
                return None
            break
        current = value
    raise AppError("Swiggy did not return a recognizable cart state.", 502, code="cart_format")


def cart_summary(payload):
    """Allowlist diagnostic structure; exclude tokens, addresses and free text."""
    summary = {"layers": []}
    current = payload
    for _ in range(7):
        if not isinstance(current, dict):
            summary["terminal_type"] = type(current).__name__
            break
        layer = {"keys": sorted(str(k) for k in current)[:35]}
        for key in ("statusCode", "success", "successful", "result", "item_count"):
            value = current.get(key)
            if isinstance(value, (bool, int, float)) or (key == "result" and value in ("success", "failure")):
                layer[key] = value
        summary["layers"].append(layer)
        if isinstance(current.get("items"), list):
            summary["items"] = [{k: item[k] for k in ("menu_item_id", "quantity", "in_stock")
                                 if k in item and isinstance(item[k], (str, int, bool))}
                                for item in current["items"][:10] if isinstance(item, dict)]
            break
        key = "response" if isinstance(current.get("response"), dict) else "data"
        if key not in current:
            break
        current = current[key]
    return summary


def cart_identity_matches(cart, offer):
    """Cleanup ownership uses IDs/quantity/options, never mutable display names."""
    if not isinstance(cart, dict):
        return False
    restaurant = cart.get("restaurant") or {}
    if not isinstance(restaurant, dict):
        return False
    rid = restaurant.get("id") or cart.get("restaurant_id")
    if rid is not None and str(rid) != str(offer["restaurant_id"]):
        return False
    items = cart.get("items")
    if not isinstance(items, list) or len(items) != 1 or not isinstance(items[0], dict):
        return False
    item = items[0]
    return (str(item.get("menu_item_id", "")) == str(offer["item_id"])
            and type(item.get("quantity")) is int and item["quantity"] == offer["quantity"]
            and all(item.get(key) in (None, []) for key in ("variants", "variations", "variantsV2", "addons")))


def read_cart(client, args, trace, phase, cleanup=False):
    try:
        payload = client.call("get_food_cart", args)
        entry = {"phase": phase, "response": cart_summary(payload)}
        trace.append(entry)
        cart = parse_cart(payload, allow_out_of_stock=cleanup)
        entry["state"] = "empty" if cart is None else "populated"
        return cart
    except AppError as exc:
        trace.append({"phase": phase, "error_code": exc.code, "status": exc.status})
        raise


def clear_and_confirm(client, args, trace, offer=None):
    """At most two clears. Reconcile a lost acknowledgement before retrying.

    Setup has explicit replace-cart consent. Cleanup only clears the test
    basket and rechecks its identity before any subsequent clear.
    """
    import time
    retry_until = time.monotonic() + 30
    if offer is not None:
        cart = read_cart(client, args, trace, "cleanup.inspect", cleanup=True)
        if cart is None:
            return True
        if not cart_identity_matches(cart, offer):
            raise AppError("The cart contains different items. They were preserved.", 409, code="cart_changed")
    for attempt in range(2):
        clear_error = None
        try:
            response = client.call("flush_food_cart", {})
            trace.append({"phase": "clear", "attempt": attempt+1, "response": cart_summary(response)})
        except AppError as exc:
            clear_error = exc
            trace.append({"phase": "clear", "attempt": attempt+1, "error_code": exc.code, "status": exc.status})
            if exc.status == 401 or exc.code == "mcp_session_expired":
                raise
            if exc.status == 429:
                delay = exc.retry_after if exc.retry_after is not None else 30
                if time.monotonic()+delay >= retry_until:
                    raise
                if not hasattr(client, "cooldown_until"):
                    time.sleep(delay)
        # A timeout/error may follow a successful clear. The read decides.
        cart = read_cart(client, args, trace, "clear.verify", cleanup=True)
        if cart is None:
            return True
        if offer is not None and not cart_identity_matches(cart, offer):
            raise AppError("The cart changed during cleanup. Different items were preserved.", 409, code="cart_changed")
        if clear_error is not None and not clear_error.retryable:
            raise clear_error
        if attempt == 0 and time.monotonic()+.5 < retry_until:
            time.sleep(.5)
            # Allow one delayed read before retrying the idempotent clear.
            cart = read_cart(client, args, trace, "clear.settle", cleanup=True)
            if cart is None:
                return True
            if offer is not None and not cart_identity_matches(cart, offer):
                raise AppError("The cart changed during cleanup. Different items were preserved.", 409, code="cart_changed")
        else:
            break
    raise AppError("Swiggy still returned items after two clear attempts.", 409, code="cart_not_empty")


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
    if item.get("name") and not exact_dish(item["name"], offer["dish"]):
        return False
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
            "customized": bool(customization_reason(item)), "customization_reason": customization_reason(item)}
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


def refresh_offer(client, offer, address_id):
    """Look up the exact selected ID in at most four scoped menu pages."""
    offset, seen = 0, set()
    for _ in range(4):
        payload = data_of(client.call("search_menu", {"addressId": address_id,
            "query": offer["dish"], "offset": offset, "restaurantIdOfAddedItem": offer["restaurant_id"]}))
        rows = menu_offers(payload, offer["quantity"], offer["restaurant_id"], offer["restaurant"])
        exact = next((row for row in rows if row["id"] == offer["id"]), None)
        if exact is not None:
            if exact["customized"] or not exact_dish(exact["dish"], offer["dish"]):
                break
            return {**offer, **exact, "needs_menu_detail": False}
        cursor = payload.get("nextOffset")
        if isinstance(cursor, str) and cursor.isdecimal():
            cursor = int(cursor)
        if payload.get("hasMore") is False or type(cursor) is not int or cursor < 0 or cursor == offset or cursor in seen:
            break
        seen.add(offset)
        offset = cursor
    raise AppError("The exact dish is unavailable or needs a size/add-on choice. Its bill was not checked.", 409)


def quote_offer(client, offer, address_id, replace_existing_cart=False):
    """Caller holds the lock; existing-cart replacement needs explicit consent."""
    offer = deepcopy(offer)
    cart_args = {"addressId": address_id, "restaurantName": offer["restaurant"]}
    attempted, empty, best, failure = False, False, None, None
    safe_to_continue = False
    existing_cart_cleared = False
    trace = []
    initial_cart = best_cart = coupons = None
    phase = "reset"
    cleanup_error = None
    try:
        if replace_existing_cart is True:
            # Cart removal has been explicitly authorized. Reset first so
            # a closed restaurant/out-of-stock old basket cannot block it.
            # A failure or unknown empty-state response stops before add.
            clear_and_confirm(client, cart_args, trace)
            existing_cart_cleared = True
        elif parse_cart(client.call("get_food_cart", cart_args)) is not None:
            raise AppError("Your Swiggy cart has items. Allow cart replacement before comparing full bills.", 409)
        safe_to_continue = True
        phase = "menu_refresh"
        offer = refresh_offer(client, offer, address_id)
        safe_to_continue = False
        # Recheck after the menu call to avoid overwriting an intervening cart edit.
        if parse_cart(client.call("get_food_cart", cart_args)) is not None:
            raise AppError("Your cart changed before the check. Existing items were preserved.", 409)
        phase = "add_item"
        attempted = True
        client.call("update_food_cart", {**cart_args, "restaurantId": offer["restaurant_id"],
            "cartItems": [{"menu_item_id": offer["item_id"], "quantity": offer["quantity"]}]})
        phase = "read_bill"
        cart = read_cart(client, cart_args, trace, phase)
        require_exact(cart, offer)
        best = read_bill(offer, cart)
        initial_cart = deepcopy(cart)
        best_cart = deepcopy(cart)
        phase = "coupons"
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
                 for c in (section.get("coupons") if isinstance(section.get("coupons"), list) else []) if isinstance(c, dict)]
        plan = coupon_plan(sections, (cart.get("offers") or {}).get("coupon_applied"))
        candidates, best_section_found = plan["codes"], plan["best_section"]
        checked, confirmed_codes = [], []
        stopped_early = False
        application_failed = False
        for code in candidates:
            # Stop adding coupon work near the main limit. Cleanup has its
            # own allowance, including when read retries used the main budget.
            if getattr(client, "calls", 0) + 6 > 36:
                stopped_early = True
                break
            cart = parse_cart(client.call("get_food_cart", cart_args))
            require_exact(cart, offer)
            apply_error = None
            try:
                client.call("apply_food_coupon", {"addressId": address_id, "couponCode": code,
                    **({"cartId": str(cart["cart_id"])} if cart.get("cart_id") is not None else {})})
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
            else:
                application_failed = True
            if bill["coupon"] == code and (bill["discount"], -bill["total"]) > (best["discount"], -best["total"]):
                best = bill
                best_cart = deepcopy(cart)
            if apply_error is not None:
                # Preserve the verified bill and stop after an uncertain call.
                stopped_early = True
                break
        coupon_complete = not (coupon_warning or stopped_early or plan["unresolved_cards"] or application_failed)
        best["bill_verified"] = True
        best["status"] = "verified" if coupon_complete else "coupon_unverified"
        best["coupon_status"] = ("applied" if best["discount"] > 0 else "none_available") if coupon_complete else "verification_incomplete"
        best["coupon_checks"] = {
            "codes_tested": checked,
            "confirmed_codes": confirmed_codes,
            "best_section_returned": best_section_found,
            "candidate_count": len(candidates),
            "all_candidates_tested": not stopped_early and len(checked) == len(candidates),
            "availability": "lookup_failed" if coupon_warning else "none_returned" if not cards else "returned",
            "complete": coupon_complete,
            "unresolved_cards": plan["unresolved_cards"],
            "failed_codes": [code for code in checked if code not in confirmed_codes],
        }
        if best.get("coupon") and best["discount"] > 0:
            best["coupon_note"] = (
                f"{best['coupon']} confirmed: Rs {best['discount']:.2f} off. "
                f"Largest verified coupon discount among {len(checked)} checks; payable total confirmed by Swiggy."
            )
        else:
            best["coupon_note"] = (
                f"No coupon discount confirmed after {len(checked)} checks. "
                f"Swiggy returned {len(cards)} coupon cards for this cart."
            )
        if coupon_warning:
            best["coupon_note"] = coupon_warning
        if not best_section_found:
            best["coupon_note"] += " No Best section was returned; available coupon codes and cart suggestions were checked."
        elif not candidates:
            best["coupon_note"] += " No usable eligible coupon codes were returned."
        if plan["unresolved_cards"]:
            best["coupon_note"] += f" {plan['unresolved_cards']} coupon cards did not expose a usable code."
        if application_failed:
            best["coupon_note"] += " Swiggy did not confirm positive savings for every attempted code."
        if not coupon_complete:
            best["coupon_note"] += " Coupon verification is incomplete; this bill is excluded from the lowest-price ranking."
        if not best["coupon_checks"]["all_candidates_tested"]:
            best["coupon_note"] += " Some candidates were not tested; maximum savings are unverified."
        if best.get("free_delivery_applied"):
            best["coupon_note"] += " Free delivery offer applied."
    except Exception as exc:
        failure = exc
    finally:
        if attempted:
            if hasattr(client, "begin_cleanup"):
                client.begin_cleanup()
            try:
                empty = clear_and_confirm(client, cart_args, trace, offer)
            except Exception as exc:
                cleanup_error = exc
                empty = False
    diagnostic = {"phase": phase, "cart_empty_after_check": empty,
                  "trace": trace, "rpc_events": getattr(client, "events", []),
                  "initial_cart": initial_cart, "best_cart": best_cart,
                  "coupon_response": coupons,
                  "original_error": getattr(failure, "code", None),
                  "cleanup_error": getattr(cleanup_error, "code", None)}
    if attempted and not empty:
        reason = str(cleanup_error) if isinstance(cleanup_error, AppError) else "Unexpected cleanup failure."
        error = AppError("Cart cleanup could not be confirmed: " + reason, 409, code="cleanup_failed")
        error.diagnostic = diagnostic
        error.safe_to_continue = False
        error.partial_offer = best
        raise error
    if failure:
        if not isinstance(failure, AppError):
            failure = AppError("The bill check encountered an unexpected response.", 502, code="unexpected_response")
        failure.diagnostic = diagnostic
        failure.safe_to_continue = (empty if attempted else safe_to_continue) and failure.status not in (401, 429)
        raise failure
    return {"offer": best, "cart_empty": empty, "existing_cart_cleared": existing_cart_cleared,
            "_bill_diagnostic": diagnostic}
