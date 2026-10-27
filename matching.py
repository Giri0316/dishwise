"""Conservative dish equality. AI never decides final match eligibility."""
import math
import os
import re
import unicodedata

SPELLINGS = {"dosai": "dosa", "dosha": "dosa", "biriyani": "biryani", "briyani": "biryani", "idly": "idli", "chappathi": "chapati", "chapathi": "chapati"}


COUNT = re.compile(r"\(?\b(\d+)\s*(?:pcs?|pieces?|nos?)\b\.?\)?", re.I)


def dish_count(name):
    """Piece count written in a menu name, e.g. 'Idly (2 Pcs)' -> '2', else None."""
    found = COUNT.search(unicodedata.normalize("NFKC", str(name)))
    return found.group(1) if found else None


def dish_key(name):
    text = COUNT.sub(" ", unicodedata.normalize("NFKC", str(name))).casefold()
    tokens = [SPELLINGS.get(token, token) for token in re.findall(r"[^\W_]+", text)]
    # A bare dosa and Plain Dosa are the only implicit recipe equivalence.
    if tokens in (["dosa"], ["plain", "dosa"]):
        return "plain dosa"
    return " ".join(tokens)


def exact_dish(name, confirmed):
    """Same recipe. A piece count in the confirmed name must match exactly;
    without one, any piece count of the same recipe matches (shown as its portion)."""
    if not dish_key(confirmed) or dish_key(name) != dish_key(confirmed):
        return False
    wanted = dish_count(confirmed)
    # STRICT_PORTIONS=1 restores the old rule: counts must always be equal, so
    # "Idly" never matches "Idly (2 Pcs)".
    if os.getenv("STRICT_PORTIONS") == "1":
        return dish_count(name) == wanted
    return wanted is None or dish_count(name) == wanted


def query_names(confirmed):
    base = dish_key(confirmed)
    names = [base]
    if "dosa" in base.split():
        # Broad query for discovery; the exact matcher still rejects fillings,
        # preparation differences, portions and combos.
        names = ["dosa", "dosai"]
    elif "biryani" in base.split():
        names += [base.replace("biryani", "biriyani")]
    return list(dict.fromkeys(names))


def customization_reason(item):
    if item.get("hasVariants") or item.get("variations") or item.get("variantsV2"):
        return "A size or variant choice is needed."
    addons = item.get("addons")
    if item.get("hasAddons") and (not isinstance(addons, list) or not addons):
        return "Add-on requirements were not supplied."
    if isinstance(addons, list):
        for group in addons:
            if (not isinstance(group, dict) or type(group.get("minAddons")) not in (int, float)
                    or not math.isfinite(group["minAddons"]) or group["minAddons"] < 0):
                return "Add-on requirements could not be confirmed."
            if group["minAddons"] > 0:
                return "A required add-on choice is needed."
    return None
