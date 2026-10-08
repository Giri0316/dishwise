# Dishwise — live exact-dish comparison

This is the complete updated Python/Flask project for your existing Render service.

## Deploy the update

1. Replace the project files with the contents of this folder. Preserve your existing secret environment settings. The main changed files are `app.py`, `comparison.py`, `swiggy.py`, `static/app.js`, `static/style.css`, and `templates/index.html`. Add the new `matching.py`, `discovery.py`, and `ai_dishes.py` files.
2. Commit/push and redeploy your existing Render service. Keep one Gunicorn worker because session data is stored in process memory. The included `render.yaml` preserves that setup.
3. In Render → Environment, set `GEMINI_API_KEY` to your own Gemini API key and `GEMINI_MODEL` to a text model enabled for that key. `gemini-3.5-flash-lite` is an example from Google's current model catalog; use the model available to your account. Keys stay on the server. Do not put them in JavaScript or commit them.
4. Redeploy after changing environment settings, refresh Dishwise, and connect Swiggy again if the process restarted.

The AI cannot be activated without your provider credentials. Without both variables, exact-name confirmation still works, with an explicit manual-mode message. AI failures fall back to manual choices. Only the typed food query is sent to Gemini; addresses, Swiggy credentials, carts and prices are not sent. Provider usage and billing depend on your Google account.

## New flow

Enter a dish and quantity, choose your saved address, and click **Find my deal**. The app asks which exact dish you mean. Select a suggestion or type the exact name, then compare. Dosa, Dosai and Plain Dosa normalize to the same plain dish. Masala, onion, rava, ghee, cheese, combinations and differently named portions do not match Plain Dosa. Other names use exact normalized token equality; uncertain aliases are excluded. No fuzzy substring or AI-based recipe substitution is used.

Discovery follows all pages exposed by `search_restaurants` and `search_menu`, including scoped restaurant menu pages. The previous five-result cap is removed. Swiggy restaurant IDs identify the branch; only `availabilityStatus=OPEN` and a numeric `distanceKm < 7` qualify. Exactly 7 km is excluded. Unknown distance or availability is never invented. The coverage panel reports pages, restaurants, incomplete pagination and missing distance data. This does not guarantee the search gateway exposes every restaurant in the real world.

Every eligible exact menu match is processed sequentially for a full MCP bill. Optional add-ons are omitted only when their returned minimum selection counts are explicitly zero. Required variants/add-ons and missing selection requirements are marked as needing choices. Such cards remain visible and are counted as unchecked; their prices are not treated as full bills. A changed dish, provider error or unavailable restaurant can also prevent a quote. Safe per-item failures continue after verified cleanup; uncertain cart state, rate limits or authentication errors stop the comparison.

**Existing cart items are removed when a bill check begins and are not restored**, as requested. The app clears and verifies the cart, adds the exact menu item ID, verifies quantity and selections, reads the bill, then clears only the expected test basket. No ordering or payment tool is enabled. Leave the Swiggy cart unchanged during a comparison.

There is a Stop button and a Continue button for intentionally paused discovery/comparison. A search expires after 30 minutes of inactivity or four hours overall. Each page is a separate short HTTP request; bill requests run one at a time. `MAX_DISCOVERY_PAGES` defaults to 2000 as an abuse/runaway safeguard. Reaching it is shown as incomplete coverage, never as a complete comparison.

## Coupons and pricing limitations

The app fetches live coupons after adding the actual item/quantity. It checks applicable codes from Best coupon sections first. When that section is absent, it considers other eligible returned sections, excluding payment-offer sections. It accepts explicit code fields or code-only titles, never guesses from an opaque coupon ID, and requires the returned COD filter and applicability. Each application includes the current cart ID when present. A savings amount is confirmed only when a fresh cart shows that exact code and a positive discount.

The six-code slice is removed, but the bounded MCP call/time budget remains. Capacity is reserved for cleanup. If not every candidate can be tested, the bill says maximum savings are unverified. Among checked candidates, the largest confirmed coupon discount wins, with lower payable total breaking ties. Restaurant cards are ranked by final payable total.

An empty coupon list is not a working discount. In your captured Aruvi example, Swiggy returned zero COD coupons and a ₹146 MCP bill while mobile checkout showed ₹217 with different item and delivery discounts. This release does not fabricate the missing ₹89 item price or a coupon. The UI labels totals as MCP quotes and states that mobile checkout may differ. Ask Swiggy engineering about missing promotions or pricing parity.

## Diagnostics

- `/api/tools`: available gateway tools. This update requires `search_restaurants` in addition to the existing menu/cart/coupon tools.
- `/api/debug/bill`: captured initial and winning cart fields before cleanup, for all completed matches in the current search.
- `/api/debug/bill?restaurant=Aruvi%20Veg%20Restaurant`: filter captures by exact restaurant name.
- Existing menu/cart/coupon diagnostics remain available. Use `offer_id` to select a specific match.

Diagnostics are authenticated, uncached and session-scoped. Authentication/session fields are redacted. The search is held in memory and replaced by a new search or process restart; expired captures are inaccessible through the endpoint.

## Local run and validation

Python 3.12 recommended. Install with `pip install -r requirements.txt`. Copy `.env.example` to `.env`, set your own credentials, and run `python app.py`. Use the deployed URL for Swiggy authentication unless a local redirect URI is separately approved.

Run `python -m unittest discover -s tests -v` , `node tests/test_ui.cjs` and `node --check static/app.js`. The UI test simulates DOM interactions; browser layout and live provider calls have not been verified. Tests use simulated Swiggy responses; they do not authenticate, mutate a live cart, call Gemini or place orders.

## References

- https://mcp.swiggy.com/builders/docs/reference/food/search_restaurants/
- https://mcp.swiggy.com/builders/docs/reference/food/search_menu/
- https://mcp.swiggy.com/builders/docs/reference/food/fetch_food_coupons/
- https://mcp.swiggy.com/builders/docs/reference/food/apply_food_coupon/
- https://ai.google.dev/api/generate-content
- https://ai.google.dev/gemini-api/docs/models
