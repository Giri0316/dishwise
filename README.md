Dishwise: automatic full bill comparison

Replace these four files in your existing project, preserving the paths:
  app.py
  comparison.py
  static/app.js
  templates/index.html

Commit/push the changes and redeploy on Render. Refresh the browser after deployment.
Keep your existing swiggy.py, style.css, requirements, and Render configuration.

Live flow:
1. Connect Swiggy and choose a saved delivery address.
2. Keep your Swiggy cart empty and unchanged while the comparison runs.
3. Click Find my deal. Matching dishes are found, then each supported option's full bill is checked automatically. No separate Check full bills action is needed.
4. Completed cards show items, delivery, taxes and other charges, coupon savings, and total payable, ranked by confirmed total. You can open View bill for the details.

Checks use temporary cart items and eligible restaurant-specific Best coupon codes returned by Swiggy, then remove the test items and verify the cart is empty. No order/payment is made. Existing or unexpected cart items are preserved. Checks are sequential and may take time. Stop waits for the current check and cleanup.

Items needing size/add-on selections are marked as needing choices; failed or stopped checks are labeled clearly and are excluded from confirmed-price ranking. A confirmed bill requires the provider to return all charge components. Swiggy groups taxes/GST and other charges together; this update does not invent a separate GST amount.

Coupon lookup errors preserve an otherwise confirmed full bill after successful cleanup, with savings marked unverified. Missing coupon sections cannot be reconstructed from the mobile app. Up to six eligible Best coupon codes are tested; any untested candidates are disclosed in bill details.

Cart parsing supports the documented success envelope and known raw CART/statusCode response formats. Unknown or unsuccessful formats still stop the comparison. Live backend errors cannot be verified without your authenticated deployment.

Validation: Python/JavaScript syntax checks and simulated provider/browser-flow checks covering automatic sequential quoting, full charge display, total sorting, restaurant-specific coupon selection, cart cleanup, and preservation of unexpected cart items. These are not live Swiggy calls.
