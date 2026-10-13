# Dishwise — cheapest 40 nearby exact-dish matches

This is the complete updated Python/Flask project for your existing Render service.

## Deploy the update

1. Replace the project files with the contents of this folder. Preserve your existing secret environment settings. The main changed files are `app.py`, `comparison.py`, `swiggy.py`, `static/app.js`, `static/style.css`, and `templates/index.html`. Include `matching.py`, `discovery.py`, `ai_dishes.py` and the new `coupons.py` file. Replace the frontend and backend together.
2. Commit/push and redeploy your existing Render service. Keep one Gunicorn worker because session data is stored in process memory. The included `render.yaml` preserves that setup.
3. In Render → Environment, set `GEMINI_API_KEY` to your own Gemini API key and `GEMINI_MODEL` to a text model enabled for that key. Use a model available to your account. Keys stay on the server. Do not put them in JavaScript or commit them.
4. Redeploy after changing environment settings, refresh Dishwise, and connect Swiggy again if the process restarted.

The AI cannot be activated without your provider credentials. Without both variables, exact-name confirmation still works, with an explicit manual-mode message. AI failures fall back to manual choices. Only the typed food query is sent to Gemini; addresses, Swiggy credentials, carts and prices are not sent. Provider usage and billing depend on your Google account.

## New flow

Enter a dish and quantity, choose your saved address, and click **Find my deal**. The app asks which exact dish you mean. Select a suggestion or type the exact name, then compare. Dosa, Dosai and Plain Dosa normalize to the same plain dish. Masala, onion, rava, ghee, cheese, combinations and differently named portions do not match Plain Dosa. Other names use exact normalized token equality; uncertain aliases are excluded. No fuzzy substring or AI-based recipe substitution is used.

## Bounded discovery and the 40-restaurant shortlist

The previous version could expand a queue into every nearby restaurant's menu. That caused the observed 119 discovery pages before rate limiting. This version removes that expansion.

1. Collect cross-restaurant dish results with exact recipe matching. Collect a small set of restaurant metadata pages for distance and availability.
2. Keep the cheapest returned exact menu item per restaurant ID. Exclude missing prices. Sort by dish price for the requested quantity, low to high.
3. Resolve unknown branch distances in that price order, using exact restaurant IDs. Keep only OPEN restaurants strictly below 7 km. Exactly 7 km and unknown distances are excluded.
4. Shortlist the first **40 distinct qualifying restaurants**, or fewer if fewer qualify. Only then refresh each selected item and calculate its full bill and coupons. No scoped menu crawling occurs during discovery.
5. Rank completed bills by the final payable total after confirmed coupons. A bill whose coupon checks could not be completed remains visible with an explicit warning and is excluded from the cheapest-result ranking.

Search has hard limits: at most 20 cross-restaurant menu pages, four initial restaurant metadata pages, two pages per unresolved branch lookup, and 64 discovery calls in total. The menu collection phase starts winding down after 55 seconds; discovery stops after a **120-second budget plus any request already in flight**. These are ceilings, not targets. A small result pool finishes earlier. Old `MAX_DISCOVERY_PAGES=2000` settings are clamped to 64. The app reuses a server-issued MCP session when one is supplied, reducing repeated initialization calls.

Swiggy's documented search does not expose an item-price sort parameter. Therefore these are the cheapest qualifying matches **among the bounded results collected**, not a guarantee of the 40 cheapest restaurants across the entire city. The interface reports coverage and the number selected. It does not invent restaurants to fill the 40 slots.

A fresh scoped item lookup checks the exact item ID, recipe and customization requirements immediately before building each test cart. At most four scoped pages are searched for that selected item. Cross-restaurant pagination offsets are never reused as scoped-menu offsets. Known optional add-ons with minimum zero are omitted; required or unknown selections fail that bill explicitly. Safe per-item failures continue after confirmed cleanup.

Existing cart items are removed when bill checks begin and are not restored, as requested. The app verifies an empty cart, adds the selected item and quantity, reads prices/coupons, and then clears only the expected test basket. No ordering or payment tool is enabled. Leave the Swiggy cart unchanged during checks.

Up to 40 full bills can still take several minutes: each uses actual cart calls. Stop waits for the current request and cleanup. Continue resumes remaining safe work. Authentication errors, provider rate limits and uncertain cleanup stop the run; a clear can be retried once after reading and checking the cart; item additions are never blindly replayed. A search expires after 30 minutes of inactivity or four hours overall.

## Coupon checks

- Fetch current coupons after adding the actual item and quantity.
- Test actual codes from **Best coupons and More offers**, with Best coupons first. A Best section no longer hides More offers. Exclude explicitly inapplicable codes and payment-specific offers.
- Missing optional `filter_applied` metadata no longer discards otherwise usable returned codes. The application response and fresh cart determine success.
- Also test a code suggested by the cart, even if its initial discount is zero, unless the returned coupon details explicitly disallow it. A suggestion alone is never reported as savings.
- Accept explicit coupon-code fields or code-only titles; never guess from opaque coupon IDs.
- Include the current cart ID with each application, verify the exact test basket afterward, and require the same code plus a positive cart discount.
- Keep the largest confirmed coupon saving among checked candidates; use the lower final total to break a savings tie. All tax, delivery and payable values come from the cart response.
- A failed coupon lookup, an available card without a usable code, an application without confirmed savings, or an exhausted test budget yields **Coupon verification incomplete**. The full bill remains visible but cannot win the cheapest-result ranking.

Each restaurant has a 36-RPC main budget. Cleanup receives a separate allowance of up to 12 additional RPCs and 60 seconds, even if the main budget was exhausted. Read retries are bounded and honor Retry-After; a cooldown longer than the recovery budget stops the run. All candidates are attempted when they fit; any untested candidate is disclosed. No unsupported claim of a guaranteed maximum discount is made.

Your earlier Aruvi response returned an empty COD coupon list. If Swiggy returns neither a usable coupon code nor a cart suggestion, the app has nothing to apply. Item promotions and payment-specific discounts visible only in the mobile app cannot be manufactured. Prices are labeled as MCP quotes because mobile checkout may differ.

## Diagnostics

- `/api/tools`: available gateway tools. This update requires `search_restaurants` in addition to the existing menu/cart/coupon tools.
- `/api/debug/cleanup`: saved step-by-step traces for successful and failed checks, with error codes and redacted response structure. Reading it never calls Swiggy.
- `/api/debug/bill`: captured initial and winning cart fields before cleanup, for completed matches and available partial snapshots from failed checks in the current search.
- `/api/debug/bill?restaurant=Aruvi%20Veg%20Restaurant`: filter captures by exact restaurant name.
- Existing menu/cart/coupon diagnostics remain available. Use `offer_id` to select a specific match.

Diagnostics are authenticated, uncached and session-scoped. Authentication/session fields are redacted. The search is held in memory and replaced by a new search or process restart; expired captures are inaccessible through the endpoint.

## Local run and validation

Python 3.12 recommended. Install with `pip install -r requirements.txt`. Copy `.env.example` to `.env`, set your own credentials, and run `python app.py`. Use the deployed URL for Swiggy authentication unless a local redirect URI is separately approved.

Run `python -m unittest discover -s tests -v`, `node tests/test_ui.cjs` and `node --check static/app.js`. The UI test simulates DOM interactions; browser layout and live provider calls have not been verified. Tests use simulated Swiggy responses; they do not authenticate, mutate a live cart, call Gemini or place orders.

## References

- https://mcp.swiggy.com/builders/docs/reference/food/search_restaurants/
- https://mcp.swiggy.com/builders/docs/reference/food/search_menu/
- https://mcp.swiggy.com/builders/docs/reference/food/fetch_food_coupons/
- https://mcp.swiggy.com/builders/docs/reference/food/apply_food_coupon/
- https://ai.google.dev/api/generate-content
- https://ai.google.dev/gemini-api/docs/models


## Cart recovery revision — 2026-10-13

The screenshot's generic cleanup error did not include the underlying exception.
This release fixes reproducible failure paths; it cannot establish which one
occurred in that live request retroactively.

- Reconcile clear timeouts by reading the same MCP session. A lost acknowledgement
  no longer automatically means the clear failed.
- Allow a delayed empty-cart read, then at most one repeated clear. An unknown
  response never counts as empty. Failed/null responses cannot prove emptiness.
- During cleanup, identify the test item by restaurant/item ID, quantity and
  options, independently of availability and mutable display names. Strict dish
  matching still gates quotes. Different IDs, quantities or extras stop cleanup.
- Handle nested and raw JSON cart formats, text-only clear acknowledgements,
  and the final SSE frame without a trailing blank line.
- Retry transient read failures with a bounded delay. Preserve authentication,
  expired-session, rate-limit and local-budget errors separately. Do not change
  MCP sessions during an uncertain cart transaction.
- Save original and cleanup failures separately. An unresolved cleanup blocks
  further quotes in that search. A new search may reset the cart under the
  existing visible replacement consent.
- Prefer a coupon's display code over a UUID in its code field. All savings still
  require a returned applied code and a positive discount.

Replace the full package, preserve Render secrets, and redeploy. Keep one worker
and one instance; this version uses process-memory sessions and a process-local
cart lock. The config endpoint returns build `cart-recovery-2026-10-13`.
The added regression tests use injected HTTP/provider failures. They do not prove
live Swiggy behavior or mobile price parity. The first live deployment check should
use one exact dish; inspect `/api/debug/cleanup` if the provider still fails.

## Research notes

Reviewed the official get/update/flush cart and coupon references, production
retry guidance and MCP transport specification. Also reviewed the public clients
below, including their cart/probe functions and MCP transport code. No source
code was copied from those projects. The toy comparator suppresses flush errors;
that behavior was deliberately not adopted. The CLI preserves MCP sessions and
handles JSON/SSE; its general 404 reinitialization is not used mid-cart because
this app must preserve the session being cleaned up.

- https://mcp.swiggy.com/builders/docs/reference/food/flush_food_cart/
- https://mcp.swiggy.com/builders/docs/reference/food/get_food_cart/
- https://mcp.swiggy.com/builders/docs/reference/food/update_food_cart/
- https://mcp.swiggy.com/builders/docs/build/ship-to-production/
- https://modelcontextprotocol.io/specification/2025-03-26/basic/transports
- https://github.com/HKTITAN/swiggy-cli/blob/main/src/lib/mcp.ts
- https://github.com/HKTITAN/swiggy-cli/blob/main/src/commands/food.ts
- https://github.com/Atishyy27/swiggy-mcp-toy/blob/main/src/agent.mjs
