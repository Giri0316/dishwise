# Validation — cart-recovery-2026-10-13

- 44 Python regression tests passed.
- The UI flow simulation completed 40 sequential quotes and excluded incomplete coupon checks from the winner.
- A second UI scenario confirmed unresolved cleanup stops further quotes, preserves completed results and shows the saved diagnostic link.
- JavaScript syntax validation passed.

Tests exercise cart response envelopes, closed/out-of-stock baskets, lost clear acknowledgements, delayed state, identity changes, failed resets, cleanup after budget exhaustion, JSON and SSE transport, authentication failures, rate-limit delays, coupon verification, strict dish matching, distance filtering and the 40-restaurant shortlist.

All provider calls in these tests are simulated. No live Swiggy or Gemini session was used, no live cart was modified, no order was placed, and Render was not deployed by this test run. The supplied screenshot did not expose the original underlying exception; these results do not establish the precise historical cause or promise error-free external service behavior.

Run locally:

```sh
python -m unittest discover -s tests -v
node tests/test_ui.cjs
RECOVERY_FAILURE=1 node tests/test_ui.cjs
node --check static/app.js
```

On Windows PowerShell set `$env:RECOVERY_FAILURE="1"`, run the UI test, then remove that environment variable.
