"use strict";
const $=id=>document.getElementById(id);
let config={},mode="demo",offers=[],locationChoice={label:"Adyar, Chennai",source:"sample"},lastSearch={},busy=false,stopRequested=false,addressPage=1;
const money=n=>n==null?"Unknown":new Intl.NumberFormat("en-IN",{style:"currency",currency:"INR",maximumFractionDigits:2}).format(n).replace(/\.00$/,"");
const esc=value=>String(value??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
async function api(path,data){const response=await fetch("/api/"+path,{method:data===undefined?"GET":"POST",headers:data===undefined?{}:{"Content-Type":"application/json","X-CSRF-Token":config.csrf},body:data===undefined?undefined:JSON.stringify(data)});const result=await response.json();if(!response.ok){const failure=Error(result.error||"Request failed.");failure.safeToContinue=result.safe_to_continue===true;throw failure;}return result;}
function error(message=""){$("error").textContent=message;$("error").hidden=!message;}
function working(value,message=""){busy=value;document.querySelectorAll("[data-lock]").forEach(el=>el.disabled=value);$("progress").hidden=!message;$("progress").textContent=message;}
function chooseLocation(value){locationChoice=value;offers=[];$("location-label").textContent=value.label;$("location-source").textContent=value.source==="sample"?"Sample area · choose another":value.address_id?"Saved Swiggy delivery address":"Saved Swiggy delivery address";$("map-dialog").close();render();}
function switchMode(value){if(busy)return;mode=value;offers=[];error();$("demo-mode").classList.toggle("active",value==="demo");$("live-mode").classList.toggle("active",value==="live");$("notice").textContent=value==="demo"?"Sample data: fictional restaurants, prices, and offers.":"Find my deal checks live delivery, taxes, other charges, and eligible coupon savings automatically.";$("live-cart-note").hidden=value!=="live";if(value==="demo"){chooseLocation({label:"Adyar, Chennai",source:"sample"});void search();}else{locationChoice=null;$("location-label").textContent="Choose a saved Swiggy address";$("location-source").textContent="Connect Swiggy, then pick one";render();}}
function render(){
 const verified=offers.some(o=>o.status==="verified");
 $("results-title").textContent=mode==="demo"?"Your sample comparison":verified?"Your full bill comparison":"Checking your deals";
 $("results-summary").textContent=offers.length?`${offers.length} options · ${lastSearch.quantity} items · ${lastSearch.area}`:"Choose your dish and delivery address";
 document.querySelector(".sort").textContent=mode==="demo"?"Lowest sample total first":"Lowest confirmed payable total first";
 $("results").innerHTML=offers.length?offers.map((o,i)=>{
 const complete=o.status==="verified"||o.status==="demo";
 const state=o.status==="failed"?"Bill unavailable":o.status==="skipped"?"Needs item choices":o.status==="incomplete"?"Not checked":o.status==="checking"?"Checking full bill…":"Waiting for bill check…";
 return `<article class="offer ${i===0&&complete?"best":""}">${i===0&&complete?`<div class="ribbon">${mode==="demo"?"LOWEST SAMPLE TOTAL":"LOWEST AMONG CONFIRMED BILLS"}</div>`:""}<div class="offer-body"><span class="avatar">${esc(o.restaurant[0])}</span><div><h3>${esc(o.restaurant)}</h3><p class="dish">${esc(o.dish)}</p><div class="meta">${o.rating?"★ "+esc(o.rating)+" · ":""}${esc(o.portion)}</div></div><div class="price"><strong>${complete?money(o.total):"—"}</strong><small>${complete?"total for "+o.quantity:state}</small></div></div>${complete?`<div class="offer-body"><div>Items ${money(o.item_total)} · Delivery ${money(o.delivery)}<br>Taxes & other charges ${money(o.other_charges)} · Coupon savings ${money(o.discount)}</div></div>`:""}<div class="offer-foot"><span>${complete?(o.discount>0?`${money(o.discount)} off · ${esc(o.coupon)}`:o.free_delivery_applied?"Free delivery offer applied":"No coupon discount confirmed"):esc(o.bill_error||state)}</span>${complete?`<button data-bill="${i}">View bill</button>`:""}</div></article>`;
 }).join(""):"<div class='empty'><h3>Your next meal, compared.</h3><p>Enter a dish and choose a delivery location to begin.</p></div>";
}
async function search(event){event?.preventDefault();error();if(busy)return;const dish=$("dish").value.trim(),quantity=Number($("quantity").value);if(!dish||!Number.isInteger(quantity)||quantity<1||quantity>10){error("Enter a dish and a quantity from 1 to 10.");return;}if(!locationChoice){await openMap();return;}if(mode==="live"&&!config.connected){error("Connect your own Swiggy account first.");return;}
 working(true,"Finding matching dishes…");offers=[];render();try{const result=await api("search",{mode,dish,quantity,area:locationChoice.label,address_id:locationChoice.address_id});offers=result.offers;lastSearch={dish,quantity,area:locationChoice.label};render();if(!offers.length)error("No matching available dishes were returned. Try another dish or address.");$("footnote").textContent=result.scope+" Portions may differ between restaurants.";if(mode==="live"&&offers.length)await checkBills();}catch(e){error(e.message);}finally{working(false);}}
async function connect(){try{error();working(true,"Opening Swiggy sign-in…");if(config.connected){const result=await api("auth/disconnect",{});config.connected=false;$("connect").textContent="Connect Swiggy";$("connection").textContent="Your personal Swiggy comparison";if(mode==="live"){locationChoice=null;offers=[];$("location-label").textContent="Choose a saved Swiggy address";$("location-source").textContent="Connect Swiggy, then pick one";render();}if(!result.revoked)error("Disconnected locally. Swiggy session revocation could not be confirmed.");}else{const result=await api("auth/start",{});window.location.assign(result.url);}}catch(e){error(e.message);}finally{working(false);}}
function bill(index){const o=offers[index];$("bill-content").innerHTML=`<h3>${esc(o.restaurant)}</h3><p>${o.quantity} × ${esc(o.dish)}</p>${[["Items",o.item_total],["Delivery",o.delivery],["Taxes and other charges",o.other_charges],["Confirmed coupon discount",o.status==="estimate"?null:o.discount]].map(([label,n])=>`<div class="bill-row"><span>${label}</span><span>${money(n)}</span></div>`).join("")}<div class="bill-row total"><span>Total payable</span><span>${money(o.total)}</span></div><p class="muted">${o.status==="demo"?"Illustrative sample amounts, not a Swiggy quote.":o.status==="estimate"?"Full bill not checked; coupon savings and fees are unknown.":esc(o.coupon_note||"Total returned by Swiggy. Prices can change.")}</p>`;$("bill-dialog").showModal();}
function sortBills(){offers.sort((a,b)=>(a.status==="verified"?0:1)-(b.status==="verified"?0:1)||(a.total??Infinity)-(b.total??Infinity));}
async function checkBills(){
 stopRequested=false;$("stop").hidden=false;
 const candidates=offers.filter(o=>!o.customized);let checked=0;
 offers=offers.map(o=>o.customized?{...o,status:"skipped",bill_error:"Choose this item's size or add-ons in Swiggy to get a full bill."}:{...o,status:"pending"});render();
 try{
  for(const [i,offer] of candidates.entries()){
   if(stopRequested)break;
   offers=offers.map(o=>o.id===offer.id?{...o,status:"checking"}:o);render();
   $("progress").hidden=false;$("progress").textContent=`Checking full bill ${i+1} of ${candidates.length} · ${offer.restaurant}…`;
   try{
    const result=await api("quote",{offer_id:offer.id,consent:true});
    if(!result.cart_empty)throw Error("Cart cleanup could not be confirmed. Check Swiggy before continuing.");
    offers=offers.map(o=>o.id===offer.id?result.offer:o);checked++;
   }catch(e){
    offers=offers.map(o=>o.id===offer.id?{...o,status:"failed",bill_error:e.message}:o);
    if(!e.safeToContinue){error(e.message);break;}
   }
   sortBills();render();
  }
 }finally{
  offers=offers.map(o=>["pending","checking"].includes(o.status)?{...o,status:"incomplete",bill_error:stopRequested?"Stopped before this bill was checked.":"Comparison stopped before this bill could be checked."}:o);
  sortBills();render();$("stop").hidden=true;
  $("footnote").textContent=`${checked} of ${offers.length} options have confirmed full bills. ${checked?"Temporary carts for completed checks were cleared. ":""}Only confirmed totals are ranked. Taxes and other charges are grouped as returned by Swiggy. Prices and coupons can change.`;
 }
}

async function openMap(){error();$("map-dialog").showModal();$("sample-areas").hidden=mode!=="demo";$("saved-list").replaceChildren();$("more-addresses").hidden=true;$("map-message").hidden=true;if(mode==="live"){if(!config.connected){mapError("Click Connect Swiggy first, then choose one of your saved addresses.");return;}await savedAddresses();}}
function mapError(message){$("map-message").hidden=false;$("map-message").textContent=message;}
async function savedAddresses(more=false){if(!more){addressPage=1;$("saved-list").replaceChildren();}$("more-addresses").disabled=true;try{const result=await api("addresses?page="+addressPage);if(!result.addresses.length&&!more)mapError("No saved addresses found. Add a delivery address in the Swiggy app, then reopen this window.");for(const address of result.addresses){const button=document.createElement("button");button.className="sample";button.textContent=(address.tag?address.tag+" · ":"")+address.label;button.onclick=()=>chooseLocation({label:address.label,source:"swiggy",address_id:address.id});$("saved-list").appendChild(button);}$("more-addresses").hidden=!result.has_more;if(result.has_more)addressPage++;}catch(e){mapError(e.message);}finally{$("more-addresses").disabled=false;}}

document.querySelectorAll("[data-close]").forEach(b=>b.onclick=()=>$(b.dataset.close).close());
document.querySelectorAll("[data-area]").forEach(b=>b.onclick=()=>chooseLocation({label:b.dataset.area,source:"sample"}));
document.querySelectorAll("[data-dish]").forEach(b=>b.onclick=()=>{$("dish").value=b.dataset.dish;});
$("search-form").onsubmit=search;$("connect").onclick=connect;$("demo-mode").onclick=()=>switchMode("demo");$("live-mode").onclick=()=>switchMode("live");$("choose-location").onclick=openMap;
$("results").onclick=event=>{const button=event.target.closest("[data-bill]");if(button)bill(Number(button.dataset.bill));};
$("stop").onclick=()=>{stopRequested=true;$("progress").textContent="Stopping after the current bill check and cleanup…";};
$("more-addresses").onclick=()=>savedAddresses(true);
(async()=>{try{config=await api("config");if(config.connected){$("connect").textContent="Disconnect Swiggy";$("connection").textContent="Swiggy connected · this browser only";}const params=new URLSearchParams(window.location.search);if(params.has("connected")){switchMode("live");}else{await search();}if(params.has("auth_error"))error("Swiggy sign-in could not finish. Check the callback URL and try Connect Swiggy again.");history.replaceState({},"",location.pathname);}catch(e){error(e.message);render();}})();
