Dishwise: clear existing cart and compare full bills

Replace these four files in your existing project, keeping the paths:
  app.py
  comparison.py
  static/app.js
  templates/index.html
Commit/push, redeploy on Render, and refresh your browser.
Keep the existing swiggy.py, stylesheet, requirements, and Render settings.

Clicking Find my deal in Live mode now authorizes removal of existing cart items. A visible note explains that previous items will not be restored. The app reads the cart, clears existing items once if present, confirms it is empty, then adds the selected comparison dish. It checks full charges and restaurant-specific Best coupons, clears the test item, and verifies empty before the next restaurant.

The observed statusCode 8 response with result=success and out-of-stock items is recognized as a populated cart for authorized clearing. It is never accepted as a valid bill. Unknown or failed cart responses still stop the comparison. If the clear fails or the cart remains populated, no new item is added and no automatic mutation retry occurs.

Cart changes detected after the initial clear or during comparison stop the check. Unexpected items are preserved during cleanup. No ordering or payment is enabled. Previous cart contents are deliberately removed, not backed up or restored.

Dishes flagged with size/add-on customization remain excluded. Coupons depend on what Swiggy exposes; up to six eligible Best coupon codes are verified. GST/taxes and other charges are displayed together as supplied by Swiggy.

Validation: syntax checks and simulated checks using the observed out-of-stock basket, authorized/default preservation paths, clear failure, failed empty confirmation, unexpected cart edits, exact test-item cleanup, automatic quoting and final total sorting. Live operation requires your authenticated deployment.
