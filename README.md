# Dishwise — Python app for local VS Code

A Flask web app for your food-offer idea: enter a dish and quantity, choose one of your saved Swiggy delivery addresses, then compare Swiggy menu options and optional full bills. Open the project in VS Code; its screen runs in your normal web browser. No Node.js, Google Maps key, or AI API key is needed.

## 1. Run it on Windows

Install Python 3.11 or newer if needed. Extract this ZIP. In VS Code, choose **File → Open Folder → dishwise-python**, then open **Terminal → New Terminal**.

Run these commands in PowerShell:

```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
.\.venv\Scripts\python.exe app.py
```

Open **http://localhost:8765**. The sample comparison works immediately. Keep the terminal running; press **Ctrl+C** to stop.

You do not need to activate the environment or change PowerShell execution policy. On later runs, only the final command is needed. If `py` is unavailable but Python is installed, use `python -m venv .venv` instead. In VS Code, select `.venv` with **Python: Select Interpreter**. The included launch configuration also supports F5 after installing VS Code's Python extension.

macOS/Linux:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
cp .env.example .env
.venv/bin/python app.py
```

## 2. Delivery address

The app uses the delivery addresses already saved in your Swiggy account. Add or edit them in the Swiggy app. After you connect Swiggy and switch to **Live Swiggy**, click the address field and pick one. The app never creates or changes addresses, and only accepts an address ID that belongs to your own account. Demo mode offers three fictional sample areas.

## 3. Connect Swiggy locally

Click **Connect Swiggy**. The app registers a local OAuth client, opens Swiggy's own sign-in page, and receives the callback at:

```text
http://localhost:8765/auth/callback
```

Enter your phone/OTP only on Swiggy's page. The app does not ask for your OTP and does not extract VS Code credentials. Your previous VS Code MCP login is separate: sign in once for this Python application. Restarting Python clears this local session, so reconnect afterward.

Swiggy documents localhost development support. Actual access still depends on your account and provider checks. If Swiggy rejects the callback, verify the exact localhost URL; request support for that URI from `builders@swiggy.in`. Do not replace it with a made-up production URL or bypass the check.

## 4. Compare your dish

1. Switch from **Demo** to **Live Swiggy**.
2. Click the delivery address field and select one of your saved Swiggy addresses.
3. Enter the dish and quantity. Click **Find my deal**.
4. Review up to five matching menu options. These initially show item subtotals only.
5. For full totals, click **Check full bills** and explicitly allow the temporary cart checks.

Full-bill comparison requires an empty Swiggy cart. It checks one option at a time, uses Swiggy's returned `to_pay`, and clears only the exact test basket. If your basket contains existing food, the check stops. Keep the Swiggy app cart unchanged during comparison. If Python is closed, the network fails, or cleanup is uncertain, review your cart manually before continuing. A stop button finishes the current cleanup before stopping the remaining checks.

No checkout, order, or payment tools are allowed. This version skips dishes with variant/add-on choices. It compares only the returned options, not every restaurant. Portions may differ. It checks at most two explicit, applicable COD coupon codes per option; opaque offer IDs are not treated as coupon codes. When offers lack explicit codes, the bill states that other savings remain unverified. Suggested coupons with zero discount are never counted as savings. Do not describe the output as an exhaustive best offer across Swiggy or across payment methods.

## Files you can edit

| File | Purpose |
| --- | --- |
| `app.py` | Flask routes, local OAuth, validation and memory-only sessions |
| `swiggy.py` | Bounded JSON-RPC / SSE MCP transport |
| `comparison.py` | Menu parsing, bill ranking, coupon and cart guards |
| `templates/index.html` | Web-app screen |
| `static/app.js` | Browser actions and saved-address picker |
| `static/style.css` | Responsive appearance |
| `tests/test_app.py` | Offline tests with simulated provider responses |

The small JavaScript file is for browser controls. The server and comparison logic are Python. This is a local personal-use prototype; it is not configured for public hosting or multiple remote users. There is no price harvesting, background polling or persistent menu database. Temporary search results remain in process memory; restarting the app removes them.

## Deploy it publicly (free on Render)

Local use is unchanged. To get a public HTTPS address (needed for Swiggy's "production redirect URI"):

1. Put this folder in a GitHub repository (the `.gitignore` keeps `.env` out).
2. On https://render.com choose **New → Blueprint**, select the repository, and apply `render.yaml`. `SECRET_KEY` is generated for you and the site's own address is detected automatically (`RENDER_EXTERNAL_URL`); you only need to set `APP_URL` if you attach a custom domain.
3. When the deploy finishes, note the address Render shows, e.g. `https://dishwise.onrender.com`.
4. Your production redirect URI is that address + `/auth/callback`, e.g. `https://dishwise.onrender.com/auth/callback`. Submit it in the Swiggy form.

Notes: Render's free service sleeps after 15 minutes idle and takes about a minute to wake, and a sleep or restart clears everyone's Swiggy login (reconnect afterwards). Keep one gunicorn worker (`-w 1`) because sessions are held in memory. The live Swiggy sign-in will only work on the public address after Swiggy approves that redirect URI.

## Checks and troubleshooting

Run the offline checks:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

The checks cover local routes, sample totals, OAuth state/PKCE, JSON/SSE transport, preservation of existing or unexpected carts, write-timeout cleanup, and confirmed coupon discounts. Live Swiggy sign-in and authenticated provider calls require your own configuration and have not been exercised against your account here. A provider schema change is reported as an error rather than guessed.

- **No saved addresses listed:** add a delivery address in the Swiggy app, then reopen the address window.
- **Session expired:** reconnect Swiggy; there are no automatic write retries.
- **Cart response not recognized:** review the cart in Swiggy; do not remove the strict checks to force an operation.
- **Port already used:** stop the other copy, or change `PORT` in `.env`. The local URL and OAuth callback must use that same port.
- **Page errors about origin:** use `http://localhost:8765`, not a file URL or a different hostname.

Official integration references checked 30 September 2026:

- https://mcp.swiggy.com/builders/docs/start/authenticate/
- https://mcp.swiggy.com/builders/docs/reference/food/search_menu/
- https://mcp.swiggy.com/builders/docs/reference/food/get_food_cart/
- https://mcp.swiggy.com/builders/docs/reference/food/fetch_food_coupons/
- https://mcp.swiggy.com/builders/docs/reference/food/create_address/
