"""Use actual returned coupon codes; cart verification is the final authority."""
import re


def code_of(card):
    for field in ("couponCode", "coupon_code", "code"):
        code = card.get(field)
        if (isinstance(code, str) and re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{2,39}", code.strip())
                and not re.fullmatch(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", code.strip())):
            return code.strip()
    title = card.get("title", "")
    if isinstance(title, str) and re.fullmatch(r"[A-Z][A-Z0-9_-]{2,39}", title.strip()):
        return title.strip()
    # Never turn opaque card IDs, savings amounts or advertising into codes.
    return None


def payment_only(section, card):
    terms = card.get("terms_and_conditions") or {}
    fragments = [section.get("title", ""), section.get("type", ""), card.get("subtitle", ""), card.get("description", "")]
    if isinstance(terms, dict):
        fragments += terms.get("bullet_texts", []) if isinstance(terms.get("bullet_texts"), list) else []
    content = " ".join(str(part) for part in fragments).lower()
    if re.search(r"(?:not|invalid|unavailable) (?:applicable |valid )?(?:on|for|with) (?:cod|cash on delivery)", content):
        return True
    if re.search(r"(?:valid|applicable|available) (?:on|for|with) (?:cod|cash on delivery)", content):
        return False
    return bool(re.search(r"payment offer|bank offer|credit card|debit card|net.?banking|upi|wallet|online payment", content))


def coupon_plan(sections, suggested=None):
    sections = [s for s in sections if isinstance(s, dict)]
    is_best = lambda s: "best" in (str(s.get("title", "")) + " " + str(s.get("type", ""))).lower()
    ordered = sorted(sections, key=lambda s: not is_best(s))
    codes, unavailable_codes = [], set()
    unresolved, eligible_cards = 0, 0
    payment_offers = []
    for section in ordered:
        cards = section.get("coupons")
        for card in cards if isinstance(cards, list) else []:
            if not isinstance(card, dict):
                continue
            code = code_of(card)
            status = str(card.get("applicabilityStatus", "")).upper()
            is_payment = payment_only(section, card)
            if is_payment and card.get("applicable") is not False and len(payment_offers) < 8:
                # Shown for information only. Swiggy MCP quotes are cash-on-delivery,
                # so these cannot be applied or verified here.
                payment_offers.append({k: card.get(k) for k in ("title", "subtitle", "description") if card.get(k)})
            if card.get("applicable") is False or status == "NOT_APPLICABLE" or is_payment:
                if code:
                    unavailable_codes.add(code)
                continue
            # Optional applicability/filter fields may be absent. Try the actual
            # returned code, but only a positive cart discount counts as applied.
            eligible_cards += 1
            if code:
                if code not in codes:
                    codes.append(code)
            elif card.get("applicable") is True:
                # Only a card Swiggy says IS applicable but gives no usable code
                # makes the check incomplete. Cards with no code and no applicable
                # flag (banners, item promos) cannot be applied, so they are not
                # a reason to discard an otherwise complete check.
                unresolved += 1
    if isinstance(suggested, str) and suggested.strip() and suggested not in unavailable_codes and suggested not in codes:
        codes.insert(0, suggested.strip())
    return {"codes": codes, "best_section": any(is_best(s) for s in sections),
            "unresolved_cards": unresolved, "eligible_cards": eligible_cards,
            "payment_offers": payment_offers}


def coupon_candidates(sections, cod_filtered=True):
    # Kept for integrations importing the older helper. filter_applied is optional.
    plan = coupon_plan(sections)
    return plan["codes"], plan["best_section"]
