"use strict";
const $ = id => document.getElementById(id);
let config = {}, locationChoice = null, offers = [], run = null, busy = false, stopRequested = false, addressPage = 1;
let selectedDish = "", requestedQuantity = 1;
const money = n => n == null ? "Not returned" : new Intl.NumberFormat("en-IN", {style:"currency",currency:"INR",maximumFractionDigits:2}).format(n).replace(/\.00$/, "");
const esc = value => String(value ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
async function api(path, data) {
  const response = await fetch("/api/" + path, {method:data === undefined ? "GET" : "POST", headers:data === undefined ? {} : {"Content-Type":"application/json","X-CSRF-Token":config.csrf}, body:data === undefined ? undefined : JSON.stringify(data)});
  let result;
  try { result = await response.json(); } catch (_) { throw Error("The server did not return a response. Check your cart before restarting a bill check."); }
  if (!response.ok) {const error = Error(result.error || "Request failed."); error.safeToContinue = result.safe_to_continue === true; throw error;}
  return result;
}
function error(message = "") {$("error").textContent = message; $("error").hidden = !message;}
function working(value, message = "") {
  busy = value; document.querySelectorAll("[data-lock]").forEach(el => el.disabled = value);
  $("progress").textContent = message; $("progress").hidden = !message;
}
function progress(message) {$("progress").hidden = false; $("progress").textContent = message;}
function render() {
  const confirmed = offers.filter(o => o.status === "verified");
  $("results-title").textContent = busy ? "Comparing your exact dish" : "Your comparison";
  $("results-summary").textContent = selectedDish ? `${requestedQuantity} × ${selectedDish} · Below 7 km · ${new Set(offers.map(o => o.restaurant_id)).size} matching restaurants` : "Connect Swiggy, then choose your dish and address.";
  offers.sort((a,b) => (a.status === "verified" ? 0 : 1) - (b.status === "verified" ? 0 : 1) || (a.total ?? Infinity) - (b.total ?? Infinity));
  $("results").innerHTML = offers.length ? offers.map((o,i) => {
    const complete = o.status === "verified";
    const state = o.status === "checking" ? "Checking bill…" : o.status === "failed" ? "Bill unavailable" : o.status === "skipped" ? "Needs choices" : "Awaiting check";
    const couponState = o.coupon_checks?.availability === "lookup_failed" ? "Coupon availability could not be checked" : "No coupon discount confirmed";
    return `<article class="offer ${complete && i === 0 ? "best" : ""}">${complete && i === 0 ? '<div class="ribbon">LOWEST AMONG RETURNED MCP QUOTES</div>' : ""}<div class="offer-body"><span class="avatar">${esc(o.restaurant[0])}</span><div><h3>${esc(o.restaurant)}</h3><p class="dish">${esc(o.dish)}</p><div class="meta">${esc(o.distance_km)} km · ${o.rating ? "★ " + esc(o.rating) + " · " : ""}${esc(o.portion)}</div></div><div class="price"><strong>${complete ? money(o.total) : "—"}</strong><small>${complete ? "MCP total for " + o.quantity : state}</small></div></div>${complete ? `<div class="charge-grid">${[["Items",o.item_total],["Delivery",o.delivery],["GST & other charges",o.other_charges],["Coupon savings",o.discount]].map(([label,n])=>`<div><span>${label}</span><strong>${money(n)}</strong></div>`).join("")}</div>` : ""}<div class="offer-foot"><span>${complete ? o.discount > 0 ? `${esc(o.coupon)} · ${money(o.discount)} confirmed` : esc(couponState + (o.free_delivery_applied ? " · Free delivery in MCP quote" : "")) : esc(o.bill_error || state)}</span>${complete ? `<button data-bill="${i}">View bill</button>` : ""}</div></article>`;
  }).join("") : '<div class="empty"><h3>Compare the same dish.</h3><p>We’ll confirm the exact name before searching nearby menus.</p></div>';
  if (run) {
    $("coverage").hidden = false;
    $("coverage").textContent = `${run.pages_checked} discovery pages checked · ${run.restaurants_seen} restaurant records · ${run.restaurants_matched} matching restaurants.\n${run.done ? run.partial ? "Discovery is incomplete. " : "Reached the end of the returned search pages. " : "Discovery is still in progress. "}${(run.warnings || []).join(" ")}`;
    $("footnote").textContent = `${confirmed.length} of ${offers.length} exact menu matches have full MCP quotes. ${run.scope || ""} Failed, stopped and customization-dependent checks are excluded from price ranking.`;
  }
}
async function startSearch(event) {
  event?.preventDefault(); if (busy) return; error();
  if (!config.connected) {error("Connect your Swiggy account first."); return;}
  if (!locationChoice) {await openAddresses(); return;}
  const dish = $("dish").value.trim(), quantity = Number($("quantity").value);
  if (!dish || !Number.isInteger(quantity) || quantity < 1 || quantity > 10) {error("Enter a dish and quantity from 1 to 10."); return;}
  requestedQuantity = quantity;
  working(true, "Checking the dish name…");
  try {
    const result = await api("dish/suggest", {dish});
    $("dish-choices").replaceChildren();
    for (const name of result.choices) {const button = document.createElement("button"); button.className = "choice"; button.textContent = name; button.onclick = () => {$("exact-dish").value = name;}; $("dish-choices").appendChild(button);}
    $("exact-dish").value = result.choices.length === 1 ? result.choices[0] : "";
    $("dish-note").textContent = result.note; $("dish-dialog").showModal();
  } catch (e) {error(e.message);} finally {working(false);}
}
async function confirmDish() {
  const dish = $("exact-dish").value.trim(); if (!dish) {$("exact-dish").focus(); return;}
  $("dish-dialog").close(); selectedDish = dish; offers = []; run = null; error();
  $("resume").hidden = true; working(true, "Starting a live search…"); render();
  try {run = await api("search", {confirmed_dish:dish, quantity:requestedQuantity, address_id:locationChoice.address_id}); await compare();}
  catch (e) {error(e.message);} finally {working(false);render();}
}
async function compare() {
  stopRequested = false; $("stop").hidden = false; $("resume").hidden = true; let canResume = true;
  try {
    while (!run.done && !stopRequested) {
      progress(`Searching menus and distances · ${run.pages_checked} pages checked · ${run.pending_pages} pages queued…`);
      run = await api("search/next", {search_id:run.search_id}); render();
    }
    if (!run.done) return;
    if (!offers.length) offers = run.offers.map(o => ({...o, status:o.customized ? "skipped" : "pending", bill_error:o.customization_reason || ""}));
    render();
    if (!offers.length) {error("No exact matches with confirmed distance below 7 km were returned. Try a more precise restaurant menu name."); return;}
    const candidates = offers.filter(o => ["pending","incomplete"].includes(o.status));
    for (const [index, offer] of candidates.entries()) {
      if (stopRequested) break;
      offer.status = "checking"; render();
      progress(`Checking full bill ${index+1} of ${candidates.length} · ${offer.restaurant} · ${offer.distance_km} km…`);
      try {
        const result = await api("quote", {search_id:run.search_id, offer_id:offer.id, consent:true, replace_existing_cart:true});
        if (!result.cart_empty) throw Error("Cart cleanup could not be confirmed. Review your cart before another comparison.");
        offers = offers.map(o => o.id === offer.id ? result.offer : o);
      } catch (e) {
        offer.status = "failed"; offer.bill_error = e.message;
        if (!e.safeToContinue) {canResume = false; error(e.message); break;}
      }
      render();
    }
  } finally {
    offers.forEach(o => {if (["pending","checking"].includes(o.status)) {o.status = "incomplete"; o.bill_error = "This bill has not been checked yet.";}});
    $("stop").hidden = true;
    $("resume").hidden = !canResume || (run.done && !offers.some(o => o.status === "incomplete"));
    render();
  }
}
async function resume() {if (busy || !run) return; error();working(true);try {await compare();}catch(e){error(e.message);}finally{working(false);render();}}
async function connect() {
  working(true, "Updating Swiggy connection…"); error();
  try {
    if (config.connected) {
      await api("auth/disconnect", {}); config.connected = false; locationChoice = null; offers = [];run = null;selectedDish = "";
      $("connect").textContent = "Connect Swiggy"; $("connection").textContent = "Live Swiggy comparison";$("location-label").textContent = "Choose a saved Swiggy address";$("coverage").hidden = true;$("resume").hidden = true;
    } else {const result = await api("auth/start", {}); window.location.assign(result.url);}
  } catch(e) {error(e.message);} finally {working(false);render();}
}
async function openAddresses() {
  if (busy) return; if (!config.connected) {error("Connect Swiggy first.");return;}
  $("map-dialog").showModal();addressPage = 1;$("saved-list").replaceChildren();await savedAddresses();
}
async function savedAddresses() {
  $("more-addresses").disabled = true;$("map-message").hidden = true;
  try {
    const result = await api("addresses?page="+addressPage);
    for (const address of result.addresses) {
      const button = document.createElement("button");button.className = "sample";button.textContent = (address.tag ? address.tag+" · " : "")+address.label;
      button.onclick = () => {locationChoice = {label:address.label,address_id:address.id};$("location-label").textContent = address.label;offers = [];run = null;$("resume").hidden = true;$("coverage").hidden = true;$("map-dialog").close();render();};$("saved-list").appendChild(button);
    }
    $("more-addresses").hidden = !result.has_more;if (result.has_more) addressPage++;
    if (!result.addresses.length) {$("map-message").textContent = "No more saved addresses were returned.";$("map-message").hidden = false;}
  } catch(e) {$("map-message").textContent = e.message;$("map-message").hidden = false;} finally {$("more-addresses").disabled = false;}
}
function bill(index) {
  const o = offers[index];
  $("bill-content").innerHTML = `<h3>${esc(o.restaurant)}</h3><p>${o.quantity} × ${esc(o.dish)} · ${esc(o.distance_km)} km</p>${[["Items",o.item_total],["Delivery",o.delivery],["GST & other charges",o.other_charges],["Confirmed coupon savings",o.discount]].map(([label,n])=>`<div class="bill-row"><span>${label}</span><span>${money(n)}</span></div>`).join("")}<div class="bill-row total"><span>MCP quoted total</span><span>${money(o.total)}</span></div><p>${esc(o.coupon_note)}</p><p class="muted">Returned by Swiggy MCP. Mobile checkout may differ. Prices can change.</p>`;$("bill-dialog").showModal();
}
document.querySelectorAll("[data-close]").forEach(button => button.onclick = () => $(button.dataset.close).close());
$("search-form").onsubmit = startSearch;$("confirm-dish").onclick = confirmDish;$("connect").onclick = connect;$("choose-location").onclick = openAddresses;$("more-addresses").onclick = savedAddresses;$("resume").onclick = resume;
$("stop").onclick = () => {stopRequested = true;progress("Stopping after the current request and any cart cleanup…");};
$("results").onclick = event => {const button = event.target.closest("[data-bill]");if (button) bill(Number(button.dataset.bill));};
(async () => {try {config = await api("config");if (config.connected) {$("connect").textContent = "Disconnect Swiggy";$("connection").textContent = "Swiggy connected";}$("ai-status").textContent = config.ai_enabled ? "AI helps clarify the dish; you confirm the exact name." : "Exact-name confirmation available. Configure Gemini to enable AI suggestions.";if (new URLSearchParams(location.search).has("auth_error")) error("Swiggy sign-in could not finish. Try connecting again.");history.replaceState({},"",location.pathname);}catch(e){error(e.message);}render();})();
