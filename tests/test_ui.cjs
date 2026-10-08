// UI control-flow simulation; no browser layout or live provider calls.
const fs = require('fs');
const vm = require('vm');
const assert = require('assert');
const base = require('path').resolve(__dirname, '..') + '/';
const html = fs.readFileSync(base + 'templates/index.html', 'utf8');
class Element {
  constructor() { this.children=[]; this.hidden=false; this.disabled=false; this.value=''; this.textContent=''; this.innerHTML=''; this.dataset={}; }
  replaceChildren(...items) { this.children=items; }
  appendChild(child) { this.children.push(child); }
  showModal() { this.open=true; }
  close() { this.open=false; }
  focus() { this.focused=true; }
}
const elements={};
const locked=[];
for (const tag of html.matchAll(/<[^>]+\bid="([^"]+)"[^>]*>/g)) {
  const el = elements[tag[1]] = new Element();
  el.value = tag[0].match(/\bvalue="([^"]*)"/)?.[1] || '';
  el.hidden = /\bhidden\b/.test(tag[0]);
  if (/\bdata-lock\b/.test(tag[0])) locked.push(el);
}
let pages=0, quotes=0;
const sourceOffers=Array.from({length:7},(_,i)=>({id:`${i+1}:1`,restaurant_id:String(i+1),restaurant:`Restaurant ${i+1}`,dish:i%2?'Plain Dosa':'Plain Dosai',quantity:1,distance_km:1+i*.7,portion:'Portion size unknown',item_total:115,discount:0,customized:false,status:'estimate',rating:'4.4'}));
const response=()=>({search_id:'s',done:pages===2,partial:false,pages_checked:pages,pending_pages:2-pages,restaurants_seen:7,restaurants_matched:pages===2?7:0,offers:pages===2?sourceOffers:[],warnings:[],scope:'All eligible returned matches under 7 km.'});
const context=vm.createContext({Intl,URLSearchParams,console,
  document:{getElementById:id=>{assert(elements[id],`Missing DOM id: ${id}`);return elements[id];},querySelectorAll:selector=>selector==='[data-lock]'?locked:[],createElement:()=>new Element()},
  window:{location:{assign(){}}},location:{pathname:'/',search:''},history:{replaceState(){}},
  fetch:async(path,options)=>{
    const body=options.body?JSON.parse(options.body):null; let data;
    if(path==='/api/config') data={csrf:'test',connected:true,ai_enabled:true};
    else if(path==='/api/addresses?page=1') data={addresses:[{id:'addr',label:'Saved home'}],has_more:false};
    else if(path==='/api/dish/suggest') data={choices:['Plain Dosa','Masala Dosa'],note:'AI suggestions'};
    else if(path==='/api/search') {assert.equal(body.confirmed_dish,'Plain Dosa');data=response();}
    else if(path==='/api/search/next') {pages++;data=response();}
    else if(path==='/api/quote') {
      quotes++;assert.equal(body.replace_existing_cart,true);assert.equal(body.search_id,'s');
      const offer=sourceOffers.find(o=>o.id===body.offer_id);assert(offer);
      data={cart_empty:true,offer:{...offer,status:'verified',delivery:0,other_charges:31.08,total:146-Number(offer.restaurant_id),coupon_note:'No coupon codes returned.',coupon_checks:{availability:'none_returned'}}};
    } else throw Error(`Unexpected API call: ${path}`);
    return {ok:true,json:async()=>data};
  }
});
(async()=>{
  vm.runInContext(fs.readFileSync(base+'static/app.js','utf8'),context);
  await new Promise(resolve=>setImmediate(resolve));
  await elements['choose-location'].onclick();elements['saved-list'].children[0].onclick();
  await elements['search-form'].onsubmit({preventDefault(){}});
  assert(elements['dish-dialog'].open);
  elements['dish-choices'].children[0].onclick();
  await elements['confirm-dish'].onclick();
  assert.equal(quotes,7);assert.equal(pages,2);
  assert.equal((elements.results.innerHTML.match(/<article /g)||[]).length,7);
  assert.equal(elements.results.innerHTML.match(/<h3>(.*?)<\/h3>/)[1],'Restaurant 7');
  assert(elements.footnote.textContent.startsWith('7 of 7'));
  assert(elements.error.hidden);assert(!elements.find.disabled);
  assert(!/\bDemo\b/.test(html));
  elements.results.onclick({target:{closest:()=>({dataset:{bill:'0'}})}});
  assert(elements['bill-dialog'].open);assert(elements['bill-content'].innerHTML.includes('MCP quoted total'));
  assert(elements['bill-content'].innerHTML.includes('No coupon codes returned.'));
  console.log('PASS: UI flow simulation: exact-dish confirmation, paginated search, seven sequential full-bill requests, price sorting, bill dialog, and no Demo.');
})().catch(error=>{console.error(error);process.exitCode=1;});
