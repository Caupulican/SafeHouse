"""The page itself: one self-contained HTML document per screen.

**No external requests of any kind.** No CDN, no web font, no icon service, no analytics.
Partly because this page is a control surface for a filtering system and must not depend
on the thing it filters, and mostly because the moment it is most needed is the moment
the network is broken - a dashboard that renders blank when DNS is down is a dashboard
that cannot tell you DNS is down.

Everything is rendered client-side from `/api/status`, so the refresh is one fetch and
the server has one representation of the truth to keep correct.

## Four tabs, not one scroll

The dashboard is one page split into four client-side tabs - no reload, the active tab
remembered in localStorage, `#inicio` / `#regras` / `#dns` / `#sistema` deep-linkable:

* **Início** - the "is everything OK" screen: the chips, the filtering-resolver card
  (with its start button), the known gaps.
* **Regras** - one panel per enforcement lane (Roblox, browser, YouTube), each merging
  that lane's live status with its own controls. This is deliberate de-duplication: the
  old page said "Roblox" in the chips, in a lanes section AND in three control cards.
* **DNS** - the AdGuard detail, the filter lists, Pi-hole, and the on-demand probes.
* **Sistema** - the full scheduled-task table, the browser policy, the password form.

## Two languages, one catalog

The page renders in Brazilian Portuguese by default with an EN/PT toggle in the header
(persisted in localStorage, wrapped in try/catch - private windows throw). All text
comes from `i18n.CATALOG`, serialised into the page: static chrome via `data-i18n`
attributes, and everything the server says via `{"k": key, "p": params}` messages
rendered by `fmt()`. Params are HTML-escaped; catalog templates are trusted and may
carry `<b>`/`<code>`. Status CODES (ACTIVE, BLOCKED, ...) stay English in the data - the
CSS pill classes key off them - and only their display words are translated.

## Laid out for a phone first

This is read from a phone in another room far more often than from a desk, so the layout
is built at 360px and allowed to grow, not built at 1100px and squeezed. Three rules do
most of the work, and each one fixes something that was actually broken here:

* **Every grid track is `minmax(min(WIDTH, 100%), 1fr)`.** A bare `minmax(280px, 1fr)`
  does not shrink below 280px, so on a 360px phone with padding the cards overflowed the
  viewport and the whole page scrolled sideways.
* **Tables become cards under 720px**, with the column heading moved into each cell via
  `data-label`. A five-column table of task names and timestamps is unreadable on a
  phone at any font size, and horizontal scrolling inside a page hides the columns that
  matter. Wide content still gets an `overflow-x` container for the in-between widths.
* **A slider owns its own full-width row**, with its value in the card's heading rather
  than fighting it for space. The two-ended delay control had two sliders and a readout
  sharing one flex row: at 360px each slider was about 90px wide, which is not a
  control, it is a decoration.
"""

from __future__ import annotations

import json

from .i18n import CATALOG

_CSS = """
:root{
  --bg:#f1f3f6; --card:#ffffff; --sunk:#f7f8fa; --ink:#15181e; --muted:#5b6472;
  --line:#e0e4ea; --accent:#2a5bd7; --accent-ink:#ffffff;
  --ok:#0f6b38; --ok-bg:#e2f4ea; --warn:#845400; --warn-bg:#fcf0d6;
  --bad:#9e211c; --bad-bg:#fbe5e4; --unk:#454a86; --unk-bg:#e8e8f7;
  --shadow:0 1px 2px rgba(16,20,30,.06),0 4px 12px rgba(16,20,30,.04);
  --r:12px; --r-sm:9px;
}
@media (prefers-color-scheme:dark){
  :root{
    --bg:#101216; --card:#191c22; --sunk:#14171c; --ink:#e7eaef; --muted:#9aa2b1;
    --line:#282d37; --accent:#7aa2ff; --accent-ink:#0b1020;
    --ok:#5fcf93; --ok-bg:#0f3122; --warn:#e6b451; --warn-bg:#38290e;
    --bad:#ff8b81; --bad-bg:#3a1917; --unk:#b7b7ee; --unk-bg:#212247;
    --shadow:none;
  }
}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--ink);
  font:15px/1.55 system-ui,-apple-system,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
  overflow-wrap:break-word}

/* ---- header + tabs ---- */
header{position:sticky;top:0;z-index:5;background:var(--card);
  border-bottom:1px solid var(--line);padding:8px 14px 10px}
@media (min-width:720px){ header{padding:10px 20px 12px} }
.hrow{display:flex;align-items:center;gap:10px}
.hrow h1{font-size:17px;margin:0 auto 0 0;letter-spacing:.2px;font-weight:700}
header .meta{display:flex;flex-wrap:wrap;gap:2px 14px;color:var(--muted);
  font-size:12px;margin-top:3px}
.lang{display:flex;border:1px solid var(--line);border-radius:9px;overflow:hidden}
.lang button{min-height:34px;min-width:40px;padding:4px 10px;border:0;border-radius:0;
  font-size:12px}
.lang button[aria-pressed=true]{background:var(--accent);color:var(--accent-ink)}
.tabs{display:flex;gap:6px;margin-top:9px}
.tabs button{flex:1;min-height:40px;padding:6px 4px;font-size:13.5px;border-radius:9px}
.tabs button[aria-selected=true]{background:var(--accent);border-color:var(--accent);
  color:var(--accent-ink)}

/* ---- shell ---- */
main{max-width:1120px;margin:0 auto;padding:14px}
@media (min-width:720px){ main{padding:20px} }
main>.tab{display:none}
main>.tab.on{display:grid;gap:14px}
@media (min-width:720px){ main>.tab.on{gap:18px} }
section{background:var(--card);border:1px solid var(--line);border-radius:var(--r);
  padding:14px;box-shadow:var(--shadow)}
@media (min-width:720px){ section{padding:18px 20px} }
h2{font-size:12px;margin:0 0 3px;letter-spacing:.7px;text-transform:uppercase;
  color:var(--muted);font-weight:700;display:flex;align-items:center;gap:8px;
  flex-wrap:wrap}
h2 .hint{text-transform:none;letter-spacing:0;font-weight:400;color:var(--muted)}
.sub{color:var(--muted);font-size:13px;margin:0 0 12px}
.grid{display:grid;gap:12px;grid-template-columns:repeat(auto-fit,minmax(min(270px,100%),1fr))}
.grid.wide{grid-template-columns:repeat(auto-fit,minmax(min(330px,100%),1fr))}
.card{border:1px solid var(--line);border-radius:var(--r-sm);padding:12px;
  background:var(--sunk);min-width:0}

/* ---- the at-a-glance strip ---- */
.chips{display:grid;gap:8px;grid-template-columns:repeat(auto-fit,minmax(min(150px,100%),1fr))}
.chip{border:1px solid var(--line);border-radius:var(--r-sm);padding:9px 11px;
  background:var(--sunk);display:grid;gap:3px;min-width:0}
.chip .k{font-size:11px;letter-spacing:.5px;text-transform:uppercase;color:var(--muted);
  font-weight:700}
.chip .v{font-size:13px;color:var(--muted)}

/* ---- status pills ---- */
.pill{display:inline-block;padding:2px 9px;border-radius:999px;font-size:11px;
  font-weight:700;letter-spacing:.5px;white-space:nowrap}
.s-ACTIVE,.s-BLOCKED{background:var(--ok-bg);color:var(--ok)}
.s-OFF,.s-ALLOWED,.s-PARTIAL,.s-REWRITTEN{background:var(--warn-bg);color:var(--warn)}
.s-DEGRADED{background:var(--bad-bg);color:var(--bad)}
.s-UNKNOWN{background:var(--unk-bg);color:var(--unk)}

/* ---- label/value rows: wrap, never squash ---- */
.row{display:flex;flex-wrap:wrap;justify-content:space-between;gap:2px 14px;
  padding:3px 0;font-size:14px;min-width:0}
.row>span:last-child,.row>b:last-child{color:var(--muted);text-align:right;min-width:0}
.row>b:first-child,.row>span:first-child{min-width:0}
.head{align-items:center;gap:8px;padding-bottom:6px;flex-wrap:nowrap}
.head b{font-size:15px}
.note{font-size:12.5px;color:var(--muted);margin-top:8px;
  border-left:3px solid var(--line);padding-left:9px}
details.det{font-size:12.5px;color:var(--muted);margin:8px 0 0}
details.det summary{cursor:pointer;font-weight:600;padding:2px 0;min-height:24px}
details.det .body{margin:6px 0 0;border-left:3px solid var(--line);padding-left:9px}

/* ---- tables: real columns wide, cards narrow ---- */
.tw{overflow-x:auto;-webkit-overflow-scrolling:touch;margin:0 -2px}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{text-align:left;padding:7px 8px;border-bottom:1px solid var(--line);
  vertical-align:top}
th{color:var(--muted);font-weight:700;font-size:11px;letter-spacing:.4px;
  text-transform:uppercase;white-space:nowrap}
td.num,th.num{text-align:right;font-variant-numeric:tabular-nums}
tr:last-child td{border-bottom:0}
.cv{min-width:0;overflow-wrap:anywhere}
@media (max-width:719px){
  .tw{overflow-x:visible}
  table.rt,table.rt tbody,table.rt tr,table.rt td{display:block;width:100%}
  table.rt thead{display:none}
  table.rt tr{border:1px solid var(--line);border-radius:var(--r-sm);padding:9px 11px;
    margin-bottom:9px;background:var(--sunk)}
  table.rt tr:last-child{margin-bottom:0}
  table.rt td{border:0;padding:3px 0;display:flex;gap:14px;align-items:baseline;
    justify-content:space-between}
  table.rt td::before{content:attr(data-label);color:var(--muted);font-weight:700;
    font-size:11px;letter-spacing:.4px;text-transform:uppercase;flex:0 0 auto}
  table.rt td .cv{text-align:right}
  table.rt td.num{text-align:left}
}
code,.mono{font-family:ui-monospace,SFMono-Regular,Consolas,monospace;font-size:12px;
  overflow-wrap:anywhere}

/* ---- controls ---- */
.ctl{border:1px solid var(--line);border-radius:var(--r-sm);padding:13px;
  background:var(--sunk);display:flex;flex-direction:column;gap:10px;min-width:0}
.ctl-head{display:flex;align-items:baseline;justify-content:space-between;gap:10px}
.ctl h3{margin:0;font-size:14.5px;font-weight:700}
.ctl p{margin:0;font-size:12.5px;color:var(--muted)}
/* Pushes every Apply button onto the same line at the bottom of its card, so a row of
   cards with different amounts of prose does not look like a broken staircase. */
.ctl .act{margin-top:auto;padding-top:2px}
output{font-variant-numeric:tabular-nums;font-weight:700;font-size:14px;
  white-space:nowrap;color:var(--accent)}
.slide{display:flex;align-items:center;gap:10px}
.slide .lbl{font-size:11px;letter-spacing:.4px;text-transform:uppercase;
  color:var(--muted);font-weight:700;flex:0 0 62px}
.slide .val{font-variant-numeric:tabular-nums;font-size:12.5px;color:var(--muted);
  flex:0 0 40px;text-align:right}
.dual{display:grid;gap:8px}
input[type=range]{flex:1 1 auto;min-width:0;width:100%;accent-color:var(--accent);
  height:26px;margin:0}
.switch{display:flex;align-items:center;gap:11px;font-size:14px}
input[type=checkbox]{width:20px;height:20px;flex:0 0 auto;accent-color:var(--accent);
  margin:0}

/* ---- the YouTube schedule editor ---- */
.modes{display:grid;gap:8px}
.mode{display:flex;align-items:center;gap:11px;font-size:14px}
input[type=radio]{width:20px;height:20px;flex:0 0 auto;accent-color:var(--accent);
  margin:0}
.win{border:1px solid var(--line);border-radius:var(--r-sm);padding:9px 10px;
  background:var(--card);display:grid;gap:8px;margin-bottom:8px}
.win .days{display:flex;gap:4px;flex-wrap:wrap}
.win .day{min-height:34px;min-width:40px;padding:2px 6px;font-size:12px;
  border-radius:7px;font-weight:600}
.win .day[aria-pressed=true]{background:var(--accent);border-color:var(--accent);
  color:var(--accent-ink)}
.win .times{display:flex;align-items:center;gap:8px;flex-wrap:wrap}
input[type=time]{font:inherit;min-height:38px;padding:4px 8px;border-radius:8px;
  border:1px solid var(--line);background:var(--card);color:var(--ink)}
.win .w-del{min-height:34px;padding:2px 12px;font-size:12px;margin-left:auto}

/* ---- buttons + fields (44px targets: this is used on a phone) ---- */
button{font:inherit;min-height:40px;padding:8px 15px;border-radius:9px;
  border:1px solid var(--line);background:var(--card);color:var(--ink);cursor:pointer;
  font-weight:600}
button:hover:not(:disabled){border-color:var(--accent);color:var(--accent)}
button.primary{background:var(--accent);border-color:var(--accent);color:var(--accent-ink)}
button.primary:hover:not(:disabled){filter:brightness(1.08);color:var(--accent-ink)}
button:disabled{opacity:.45;cursor:default}
button.wide{width:100%}
input[type=password],input[type=text]{font:inherit;min-height:42px;padding:9px 11px;
  border-radius:9px;border:1px solid var(--line);background:var(--card);color:var(--ink);
  width:100%}
input:focus-visible,button:focus-visible,[type=range]:focus-visible{outline:2px solid
  var(--accent);outline-offset:2px}
form.stack{display:grid;gap:10px}
label.field{display:grid;gap:5px;font-size:12.5px;color:var(--muted);font-weight:600}

/* ---- known gaps ---- */
.gap{border-left:4px solid var(--warn);background:var(--warn-bg);border-radius:var(--r-sm);
  padding:10px 13px;margin-bottom:9px;color:var(--ink)}
.gap:last-child{margin-bottom:0}
.gap.high{border-left-color:var(--bad);background:var(--bad-bg)}
.gap h4{margin:0 0 4px;font-size:14px;display:flex;flex-wrap:wrap;gap:8px;
  align-items:center}
.gap p{margin:0;font-size:13px}
.gap .verify{margin-top:6px;font-size:11.5px}

/* ---- toast + modal ---- */
#toast{position:fixed;left:50%;transform:translateX(-50%);bottom:14px;z-index:20;
  width:min(660px,calc(100vw - 24px));display:grid;gap:8px}
.toast{padding:11px 14px;border-radius:10px;border:1px solid var(--line);
  background:var(--card);box-shadow:0 6px 24px rgba(0,0,0,.28);font-size:13.5px}
.toast.ok{border-left:4px solid var(--ok)}
.toast.bad{border-left:4px solid var(--bad)}
#modal{position:fixed;inset:0;background:rgba(6,8,14,.6);display:none;
  align-items:center;justify-content:center;z-index:30;padding:16px}
#modal.on{display:flex}
#modal .box{background:var(--card);border-radius:var(--r);padding:18px;
  max-width:420px;width:100%;display:grid;gap:11px;box-shadow:0 12px 40px rgba(0,0,0,.4)}
#modal h3{margin:0;font-size:16px}
.actions{display:flex;gap:8px;justify-content:flex-end;flex-wrap:wrap}

/* ---- sign-in ---- */
.center{max-width:400px;margin:0 auto;padding:32px 16px 48px;
  min-height:100dvh;display:flex;flex-direction:column;justify-content:center;gap:12px}
.center h1{margin:0;font-size:22px}
.center p{margin:0}
.center .lang{align-self:flex-end}
.err{color:var(--bad);font-size:13px;min-height:18px}
.stamp{color:var(--muted);font-size:12.5px}
"""

_HEAD = """<!doctype html>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="referrer" content="no-referrer">
<meta name="color-scheme" content="light dark">
<title>SafeHouse</title>
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16'><text y='14' font-size='14'>&#127968;</text></svg>">
<style>__CSS__</style>
"""

# The language toggle, shared by every screen. `aria-pressed` is set by applyLang().
_LANG = """<div class="lang" role="group" aria-label="Idioma / language">
  <button type="button" data-lang="pt">PT</button>
  <button type="button" data-lang="en">EN</button>
</div>"""

# The i18n runtime, shared by every screen. The catalog is the ONE in i18n.CATALOG,
# serialised in; `fmt` renders {"k":...,"p":{...}} messages with params HTML-escaped and
# templates trusted; `applyLang` walks data-i18n/-ph/-aria attributes for static chrome.
# localStorage is wrapped in try/catch throughout: private windows and locked-down
# browsers throw on access, and a panel that fails to render over a language preference
# would be failing at its one job.
_I18N_JS = """
const CAT=__CATALOG__;
let LANG='pt';
try{const v=localStorage.getItem('sh-lang');if(v==='en'||v==='pt')LANG=v;}catch(e){}
function esc(v){return String(v==null?'':v).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));}
function tr(k){const c=CAT[LANG]||{};const t=c[k];if(t!=null)return t;
  return CAT.en[k]!=null?CAT.en[k]:k;}
function _fmt(m,e){
  if(m==null)return '';
  if(Array.isArray(m))return m.map(x=>_fmt(x,e)).filter(Boolean).join(' ');
  if(typeof m!=='object')return e(String(m));
  return tr(m.k).replace(/\\{(\\w+)\\}/g,(w,n)=>{const p=(m.p||{})[n];
    return (p!==null&&typeof p==='object')?_fmt(p,e):e(p==null?'':String(p));});
}
function fmt(m){return _fmt(m,esc);}
function fmtT(m){return _fmt(m,String).replace(/<[^>]*>/g,'');}
function applyLang(){
  document.documentElement.lang=LANG==='pt'?'pt-BR':'en';
  document.querySelectorAll('[data-i18n]').forEach(el=>{el.innerHTML=tr(el.dataset.i18n);});
  document.querySelectorAll('[data-i18n-ph]').forEach(el=>{el.placeholder=fmtT({k:el.dataset.i18nPh});});
  document.querySelectorAll('[data-i18n-aria]').forEach(el=>{el.setAttribute('aria-label',fmtT({k:el.dataset.i18nAria}));});
  document.querySelectorAll('.lang button').forEach(b=>{b.setAttribute('aria-pressed',b.dataset.lang===LANG?'true':'false');});
}
function initLang(onchange){
  document.querySelectorAll('.lang button').forEach(b=>b.addEventListener('click',()=>{
    if(b.dataset.lang===LANG)return;
    LANG=b.dataset.lang;
    try{localStorage.setItem('sh-lang',LANG);}catch(e){}
    applyLang();
    if(onchange)onchange();
  }));
  applyLang();
}
"""


def _doc(body: str) -> str:
    page = _HEAD.replace("__CSS__", _CSS) + body
    return page.replace("__CATALOG__", json.dumps(CATALOG, ensure_ascii=False))


def render_setup() -> str:
    """Shown when no password has been set yet. Deliberately not a way to set one.

    The panel is started by a SYSTEM scheduled task at boot, where there is no console to
    print a generated password to and nobody to read it. Letting the first visitor pick
    the password instead would mean whoever reaches the page first owns it - on a LAN the
    child is also on. So the password is set at the machine, elevated, by hand; after
    that it can be changed from inside the panel, which needs the old one.

    The command lines are left exactly as typed in both languages.
    """
    return _doc("""
<div class="center">
  __LANG__
  <h1 data-i18n="setup.title">Painel de controle SafeHouse</h1>
  <p data-i18n="setup.nopw"></p>
  <p data-i18n="setup.elev"></p>
  <p><code>C:\\SafeHouse\\python\\python.exe -m safehouse_panel set-password --generate</code></p>
  <p data-i18n="setup.writes"></p>
  <p class="stamp" data-i18n="setup.hash"></p>
</div>
<script>__I18N__
initLang();
</script>
""".replace("__LANG__", _LANG).replace("__I18N__", _I18N_JS))


def render_login() -> str:
    return _doc("""
<div class="center">
  __LANG__
  <h1>SafeHouse</h1>
  <p class="stamp" data-i18n="login.sub"></p>
  <form id="f" class="stack" autocomplete="off">
    <input type="password" id="pw" data-i18n-ph="login.ph" autofocus
           autocomplete="current-password" enterkeyhint="go">
    <p class="err" id="err"></p>
    <button class="primary wide" type="submit" data-i18n="b.signin">Entrar</button>
  </form>
  <p class="stamp" data-i18n="login.http"></p>
</div>
<script>__I18N__
initLang();
const f=document.getElementById('f'),pw=document.getElementById('pw'),err=document.getElementById('err');
f.addEventListener('submit',async e=>{
  e.preventDefault(); err.textContent='';
  const r=await fetch('/api/login',{method:'POST',headers:{'Content-Type':'application/json'},
    credentials:'same-origin',body:JSON.stringify({password:pw.value})});
  const j=await r.json().catch(()=>({}));
  if(r.ok){location.reload();return;}
  err.textContent=j.error?fmtT(j.error):fmtT({k:'login.failed',p:{s:r.status}});
  pw.select();
});
</script>
""".replace("__LANG__", _LANG).replace("__I18N__", _I18N_JS))


_BODY = """
<header>
  <div class="hrow">
    <h1>SafeHouse</h1>
    __LANG__
    <button id="logout" data-i18n="b.signout">Sair</button>
  </div>
  <div class="meta"><span id="host"></span><span id="clock"></span></div>
  <nav class="tabs" id="tabs" role="tablist">
    <button role="tab" data-tab="home" data-i18n="tab.home">In&iacute;cio</button>
    <button role="tab" data-tab="rules" data-i18n="tab.rules">Regras</button>
    <button role="tab" data-tab="dns" data-i18n="tab.dns">DNS</button>
    <button role="tab" data-tab="system" data-i18n="tab.system">Sistema</button>
  </nav>
</header>
<main>

  <div class="tab" id="tab-home">
    <section>
      <h2><span data-i18n="h.now">Agora</span>
        <span class="hint" data-i18n="h.now.hint"></span></h2>
      <div class="chips" id="chips"></div>
      <p class="stamp" style="margin:9px 0 0" data-i18n="n.unknown"></p>
    </section>
    <section>
      <h2><span data-i18n="h.resolver"></span>
        <span class="hint" data-i18n="h.resolver.hint"></span></h2>
      <p class="sub" data-i18n="resolver.sub"></p>
      <div id="resolver"></div>
    </section>
    <section>
      <h2><span data-i18n="h.gaps"></span>
        <span class="hint" data-i18n="h.gaps.hint"></span></h2>
      <div id="gaps"></div>
    </section>
  </div>

  <div class="tab" id="tab-rules">
    <p class="sub" style="margin:0" data-i18n="h.rules.sub"></p>

    <section>
      <h2><span data-i18n="lane.roblox">Roblox</span><span id="pill-roblox"></span></h2>
      <div id="lane-roblox"></div>
      <div class="grid" style="margin-top:12px">

        <div class="ctl" data-ctl="roblox_limit_min">
          <div class="ctl-head"><h3 data-i18n="c.roblox_limit">Por dia</h3>
            <output>0 min</output></div>
          <div class="slide"><input type="range" min="0" max="240" step="5" value="0"
               data-i18n-aria="field.roblox_limit"></div>
          <p data-i18n="hint.roblox_limit"></p>
          <div class="act"><button class="primary wide" data-apply
            data-i18n="b.apply">Aplicar</button></div>
        </div>

        <div class="ctl" data-ctl="roblox_bonus_min">
          <div class="ctl-head"><h3 data-i18n="c.roblox_bonus">Extra hoje</h3>
            <output>0 min</output></div>
          <div class="slide"><input type="range" min="0" max="240" step="5" value="0"
               data-i18n-aria="field.roblox_bonus"></div>
          <p data-i18n="hint.roblox_bonus"></p>
          <div class="act"><button class="primary wide" data-apply
            data-i18n="b.grant">Conceder</button></div>
        </div>

        <div class="ctl" data-ctl="roblox_delay_sec">
          <div class="ctl-head"><h3 data-i18n="c.delay">Pausa antes de fechar</h3>
            <output>1&ndash;5s</output></div>
          <div class="dual">
            <div class="slide"><span class="lbl" data-i18n="lbl.soonest"></span>
              <input type="range" min="1" max="60" step="1" value="1" data-part="min"
                     data-i18n-aria="lbl.soonest">
              <span class="val" data-val="min">1s</span></div>
            <div class="slide"><span class="lbl" data-i18n="lbl.latest"></span>
              <input type="range" min="1" max="60" step="1" value="5" data-part="max"
                     data-i18n-aria="lbl.latest">
              <span class="val" data-val="max">5s</span></div>
          </div>
          <p data-i18n="hint.delay"></p>
          <div class="act"><button class="primary wide" data-apply
            data-i18n="b.apply">Aplicar</button></div>
        </div>

      </div>
    </section>

    <section>
      <h2><span data-i18n="lane.games">Jogos do Roblox</span>
        <span id="pill-games"></span></h2>
      <div id="lane-games"></div>
      <div class="grid" style="margin-top:12px">

        <div class="ctl" data-ctl="games_enabled">
          <div class="ctl-head"><h3 data-i18n="c.games_enabled">Filtro de jogos</h3></div>
          <label class="switch"><input type="checkbox">
            <span data-i18n="sw.games"></span></label>
          <p data-i18n="hint.games_enabled"></p>
          <div class="act"><button class="primary wide" data-apply
            data-i18n="b.apply">Aplicar</button></div>
        </div>

        <div class="ctl" data-ctl="games_mode">
          <div class="ctl-head"><h3 data-i18n="c.games_mode">Modo</h3></div>
          <label class="switch"><input type="radio" name="gmode" value="allow">
            <span data-i18n="games.v.allow"></span></label>
          <label class="switch"><input type="radio" name="gmode" value="deny">
            <span data-i18n="games.v.deny"></span></label>
          <p data-i18n="hint.games_mode"></p>
          <div class="act"><button class="primary wide" data-apply
            data-i18n="b.apply">Aplicar</button></div>
        </div>

        <div class="ctl" data-ctl="games_list" style="grid-column:1/-1">
          <div class="ctl-head"><h3 data-i18n="c.games_list">A lista</h3>
            <output>0/0</output></div>
          <div id="games-list"></div>
          <p data-i18n="hint.games_list"></p>
          <p class="note" data-i18n="games.note.samegame"></p>
          <p class="note" data-i18n="games.note.ceiling"></p>
          <div class="act"><button class="primary wide" data-apply
            data-i18n="games.save">Salvar a lista</button></div>
        </div>

      </div>
    </section>

    <section>
      <h2><span data-i18n="lane.browser">Navegador</span><span id="pill-browser"></span></h2>
      <div id="lane-browser"></div>
      <div class="grid" style="margin-top:12px">

        <div class="ctl" data-ctl="browser_enabled">
          <div class="ctl-head"><h3 data-i18n="c.browser_enabled">Prote&ccedil;&atilde;o</h3></div>
          <label class="switch"><input type="checkbox">
            <span data-i18n="sw.browser"></span></label>
          <p data-i18n="hint.browser_enabled"></p>
          <div class="act"><button class="primary wide" data-apply
            data-i18n="b.apply">Aplicar</button></div>
        </div>

        <div class="ctl" data-ctl="browser_unlock_min">
          <div class="ctl-head"><h3 data-i18n="c.unlock">Liberar por tempo</h3>
            <output>0 min</output></div>
          <div class="slide"><input type="range" min="0" max="240" step="5" value="0"
               data-i18n-aria="field.unlock"></div>
          <p data-i18n="hint.unlock"></p>
          <div class="act"><button class="primary wide" data-apply
            data-i18n="b.apply">Aplicar</button></div>
        </div>

        <div class="ctl" data-ctl="browser_limit_min">
          <div class="ctl-head"><h3 data-i18n="c.browser_limit">Por dia</h3>
            <output>0 min</output></div>
          <div class="slide"><input type="range" min="0" max="240" step="5" value="0"
               data-i18n-aria="field.browser_limit"></div>
          <p data-i18n="hint.browser_limit"></p>
          <div class="act"><button class="primary wide" data-apply
            data-i18n="b.apply">Aplicar</button></div>
        </div>

        <div class="ctl" data-ctl="browser_notify">
          <div class="ctl-head"><h3 data-i18n="c.notify">Avisar a crian&ccedil;a</h3></div>
          <label class="switch"><input type="checkbox">
            <span data-i18n="sw.notify"></span></label>
          <p data-i18n="hint.notify"></p>
          <div class="act"><button class="primary wide" data-apply
            data-i18n="b.apply">Aplicar</button></div>
        </div>

        <div class="ctl" data-ctl="browser_delay_sec">
          <div class="ctl-head"><h3 data-i18n="c.delay">Pausa antes de fechar</h3>
            <output>1&ndash;5s</output></div>
          <div class="dual">
            <div class="slide"><span class="lbl" data-i18n="lbl.soonest"></span>
              <input type="range" min="1" max="60" step="1" value="1" data-part="min"
                     data-i18n-aria="lbl.soonest">
              <span class="val" data-val="min">1s</span></div>
            <div class="slide"><span class="lbl" data-i18n="lbl.latest"></span>
              <input type="range" min="1" max="60" step="1" value="5" data-part="max"
                     data-i18n-aria="lbl.latest">
              <span class="val" data-val="max">5s</span></div>
          </div>
          <p data-i18n="hint.delay"></p>
          <div class="act"><button class="primary wide" data-apply
            data-i18n="b.apply">Aplicar</button></div>
        </div>

      </div>
    </section>

    <section>
      <h2><span data-i18n="lane.youtube">YouTube</span><span id="pill-youtube"></span></h2>
      <div id="lane-youtube"></div>
      <div class="grid" style="margin-top:12px">

        <div class="ctl" data-ctl="youtube_schedule">
          <div class="ctl-head"><h3 data-i18n="c.youtube">No resolvedor</h3>
            <output id="yt-now"></output></div>
          <div class="modes" role="radiogroup" data-i18n-aria="field.youtube_mode">
            <label class="mode"><input type="radio" name="ytmode" value="blocked">
              <span data-i18n="yt.mode.blocked"></span></label>
            <label class="mode"><input type="radio" name="ytmode" value="schedule">
              <span data-i18n="yt.mode.schedule"></span></label>
            <label class="mode"><input type="radio" name="ytmode" value="allowed">
              <span data-i18n="yt.mode.allowed"></span></label>
          </div>
          <div id="yt-editor" hidden>
            <div id="yt-wins"></div>
            <p class="stamp" data-i18n="yt.win.allday" style="margin:0 0 8px"></p>
            <button type="button" id="yt-addwin" class="wide"
              data-i18n="yt.win.add">Adicionar janela</button>
          </div>
          <div id="yt-ovr" hidden>
            <div class="actions" style="justify-content:flex-start">
              <button type="button" id="yt-allow-now"
                data-i18n="b.yt.allownow">Liberar agora</button>
              <button type="button" id="yt-block-now"
                data-i18n="b.yt.blocknow">Bloquear agora</button>
              <button type="button" id="yt-clear-ovr" hidden
                data-i18n="b.yt.backsched">Voltar ao hor&aacute;rio</button>
            </div>
            <p class="stamp" id="yt-ovr-note" style="margin:6px 0 0"></p>
          </div>
          <p class="stamp" id="yt-next" style="margin:0"></p>
          <p data-i18n="hint.youtube"></p>
          <div class="act"><button class="primary wide" data-apply
            data-i18n="b.apply">Aplicar</button></div>
        </div>

      </div>
    </section>
  </div>

  <div class="tab" id="tab-dns">
    <section>
      <h2 data-i18n="h.dns">Filtragem DNS</h2>
      <div id="dns"></div>
    </section>
    <section>
      <h2><span data-i18n="h.probes"></span>
        <span class="hint" data-i18n="h.probes.hint"></span></h2>
      <details class="det"><summary data-i18n="d.why"></summary>
        <p class="body" data-i18n="det.probes"></p></details>
      <div class="actions" style="justify-content:flex-start;align-items:center;margin-top:10px">
        <button id="probe" class="primary" data-i18n="b.probe">Rodar as sondas</button>
        <span class="stamp" id="probe-when"></span></div>
      <div id="probes" style="margin-top:12px"></div>
    </section>
  </div>

  <div class="tab" id="tab-system">
    <section>
      <h2><span data-i18n="h.tasks"></span>
        <span class="hint" data-i18n="h.tasks.hint"></span></h2>
      <div id="tasks"></div>
    </section>
    <section>
      <h2><span data-i18n="h.policy"></span>
        <span class="hint" data-i18n="h.policy.hint"></span></h2>
      <div id="policy"></div>
    </section>
    <section>
      <h2 data-i18n="h.access">Acesso ao painel</h2>
      <p class="sub" data-i18n="access.sub"></p>
      <form id="pwform" class="stack" autocomplete="off" style="max-width:420px">
        <label class="field"><span data-i18n="f.current"></span>
          <input type="password" id="pw-cur" autocomplete="current-password"></label>
        <label class="field"><span data-i18n="f.new"></span>
          <input type="password" id="pw-new" autocomplete="new-password"></label>
        <label class="field"><span data-i18n="f.again"></span>
          <input type="password" id="pw-again" autocomplete="new-password"></label>
        <p class="err" id="pw-err"></p>
        <div><button class="primary" type="submit"
          data-i18n="b.changepw">Alterar senha</button></div>
      </form>
    </section>
  </div>

  <p class="stamp" id="foot"></p>
</main>
<div id="toast"></div>
<div id="modal"><form class="box" id="mform">
  <h3 id="mtitle" data-i18n="m.title"></h3>
  <p class="stamp" id="mtext"></p>
  <input type="password" id="mpw" data-i18n-ph="m.ph" autocomplete="current-password">
  <p class="err" id="merr"></p>
  <div class="actions">
    <button type="button" id="mcancel" data-i18n="b.cancel">Cancelar</button>
    <button type="submit" class="primary" data-i18n="b.confirm">Confirmar</button>
  </div>
</form></div>
<script>__I18N__
const $=s=>document.querySelector(s), $$=s=>[...document.querySelectorAll(s)];
let SNAP=null;

/* A control the parent is touching is never overwritten by the 10s refresh: dragging a
   slider while a poll lands would yank it back under the finger. `dirty` is set on the
   first input and cleared when the value is applied or the card is reset. */
const dirty=new Set();

/* ---- tabs: client-side, remembered, deep-linkable ---- */
const TABHASH={home:'inicio',rules:'regras',dns:'dns',system:'sistema'};
const HASHTAB={inicio:'home',home:'home',regras:'rules',rules:'rules',
  dns:'dns',sistema:'system',system:'system'};
function showTab(t,setHash){
  if(!TABHASH[t])t='home';
  $$('#tabs button').forEach(b=>b.setAttribute('aria-selected',
    b.dataset.tab===t?'true':'false'));
  $$('main>.tab').forEach(d=>d.classList.toggle('on',d.id==='tab-'+t));
  try{localStorage.setItem('sh-tab',t);}catch(e){}
  if(setHash!==false){try{history.replaceState(null,'','#'+TABHASH[t]);}catch(e){}}
}
$$('#tabs button').forEach(b=>b.addEventListener('click',()=>showTab(b.dataset.tab)));
window.addEventListener('hashchange',()=>{
  const t=HASHTAB[location.hash.slice(1).toLowerCase()];
  if(t)showTab(t,false);
});
(function(){
  let t=HASHTAB[location.hash.slice(1).toLowerCase()];
  if(!t){try{t=localStorage.getItem('sh-tab');}catch(e){}}
  showTab(t||'home',!!location.hash);
})();

/* ---- rendering helpers ---- */
/* The status CODE stays in the class (s-ACTIVE etc); only the printed word changes
   with the language. */
function stw(s){s=s||'UNKNOWN';const k='st.'+s;
  return esc((CAT[LANG]&&CAT[LANG][k])||CAT.en[k]||s);}
function pill(s){s=s||'UNKNOWN';
  return '<span class="pill s-'+esc(s)+'">'+stw(s)+'</span>';}
function tstate(t){
  if(t.registered===null)return tr('ts.Unknown');
  if(t.registered===false)return tr('ts.NotRegistered');
  const k='ts.'+t.state;
  return (CAT.en[k]!=null)?tr(k):esc(t.state);
}
function row(label,value){return '<div class="row"><span>'+label+'</span><span>'
  +value+'</span></div>';}
function boolword(v){return v==null?tr('v.unknown'):(v?tr('v.yes'):tr('v.no'));}
/* Every cell carries its column name, which the stylesheet promotes to a label when the
   table folds into cards under 720px. A table without these is unreadable on a phone. */
function td(label,html,cls){return '<td'+(cls?' class="'+cls+'"':'')+' data-label="'+esc(label)
  +'"><div class="cv">'+html+'</div></td>';}
function table(cols,rows){
  if(!rows.length) return '<p class="sub">'+tr('t.empty')+'</p>';
  return '<div class="tw"><table class="rt"><thead><tr>'
    +cols.map(c=>'<th'+(c.num?' class="num"':'')+'>'+esc(c.label)+'</th>').join('')
    +'</tr></thead><tbody>'+rows.join('')+'</tbody></table></div>';
}

function toast(m,ok){
  const t=document.createElement('div'); t.className='toast '+(ok?'ok':'bad');
  t.innerHTML=fmt(m); $('#toast').appendChild(t);
  setTimeout(()=>t.remove(), ok?6000:14000);
}

function askPassword(text){
  return new Promise(resolve=>{
    const m=$('#modal'); $('#mtext').textContent=text; $('#merr').textContent='';
    $('#mpw').value=''; m.classList.add('on'); setTimeout(()=>$('#mpw').focus(),30);
    const done=v=>{m.classList.remove('on');$('#mform').onsubmit=null;$('#mcancel').onclick=null;resolve(v);};
    $('#mform').onsubmit=e=>{e.preventDefault();done($('#mpw').value);};
    $('#mcancel').onclick=()=>done(null);
  });
}

async function post(path,body){
  const r=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json'},
    credentials:'same-origin',body:JSON.stringify(body||{})});
  let j={}; try{j=await r.json();}catch(e){}
  if(r.status===401&&j.login){location.reload();}
  return {status:r.status,body:j};
}

async function apply(action,value,password){
  const res=await post('/api/set',password?{action,value,password}:{action,value});
  if((res.status===403||res.status===401)&&res.body.needs_password){
    const pw=await askPassword(res.body.error?fmtT(res.body.error):fmtT({k:'err.needspw'}));
    if(pw===null){toast({k:'toast.cancel'},false);return false;}
    return apply(action,value,pw);
  }
  const ok=res.status===200&&res.body.ok!==false;
  toast(res.body.message||res.body.error||{k:'toast.failed',p:{s:res.status}},ok);
  dirty.delete(action);
  await refresh();
  return ok;
}

function readControl(card){
  if(card.dataset.ctl==='youtube_schedule') return readYtSchedule(card);
  /* Both of these would be misread by the generic reader below - the mode card's radios
     and the list card's checkboxes both look like "a checkbox card" to it. */
  if(card.dataset.ctl==='games_mode'){
    const on=card.querySelector('input[name=gmode]:checked');
    return on?on.value:'allow';
  }
  if(card.dataset.ctl==='games_list')
    return $$('#games-list input[type=checkbox]:checked').map(b=>+b.value);
  const box=card.querySelector('input[type=checkbox]');
  if(box) return box.checked;
  const parts=card.querySelectorAll('input[type=range]');
  if(parts.length===2) return {min_sec:+parts[0].value,max_sec:+parts[1].value};
  return +parts[0].value;
}

function paintControl(card,value){
  const key=card.dataset.ctl;
  if(key==='youtube_schedule'){ paintYtSchedule(card); return; }
  if(key==='games_mode'){
    const on=card.querySelector('input[name=gmode][value="'+(value||'allow')+'"]');
    if(on) on.checked=true;
    return;
  }
  /* The list is drawn from the snapshot's catalog, not from this value: the ticks and
     the rows they sit on arrive together, so painting them apart would flicker. */
  if(key==='games_list'){ refreshOutputs(card); return; }
  const box=card.querySelector('input[type=checkbox]');
  const ranges=card.querySelectorAll('input[type=range]');
  if(box){ box.checked = !!value; return; }
  if(ranges.length===2){
    const v=value||[1,5];
    ranges[0].value=v[0]; ranges[1].value=v[1];
  } else if(ranges.length===1){
    ranges[0].value = value==null?0:value;
  }
  refreshOutputs(card);
}

function refreshOutputs(card){
  const ranges=card.querySelectorAll('input[type=range]'), out=card.querySelector('output');
  if(card.dataset.ctl==='games_list'){
    if(out) out.textContent=card.querySelectorAll('input[type=checkbox]:checked').length
      +'/'+card.querySelectorAll('input[type=checkbox]').length;
    return;
  }
  if(ranges.length===2){
    const lo=card.querySelector('[data-val=min]'), hi=card.querySelector('[data-val=max]');
    if(lo) lo.textContent=ranges[0].value+'s';
    if(hi) hi.textContent=ranges[1].value+'s';
    if(out) out.textContent=ranges[0].value+'\\u2013'+ranges[1].value+'s';
  } else if(ranges.length===1 && out){
    out.textContent=ranges[0].value+' min';
  }
}

/* ---- the YouTube schedule editor ----
   The windows live in the DOM (one .win row each); readYtSchedule serialises them back
   into the {mode, windows} shape the server validates. Days use Python's numbering,
   0 = Monday, so the two ends can never disagree about what day 5 means. */
const DAYKEYS=['day.0','day.1','day.2','day.3','day.4','day.5','day.6'];
function ytDirty(){dirty.add('youtube_schedule');}
function ytMode(card){
  const on=card.querySelector('input[name=ytmode]:checked');
  return on?on.value:'blocked';
}
function ytToggleEditor(card){$('#yt-editor').hidden = ytMode(card)!=='schedule';}
function ytWinRow(win){
  const rowEl=document.createElement('div'); rowEl.className='win';
  const days=document.createElement('div'); days.className='days';
  for(let d=0;d<7;d++){
    const b=document.createElement('button'); b.type='button'; b.className='day';
    b.dataset.day=d; b.textContent=fmtT({k:DAYKEYS[d]});
    b.setAttribute('aria-pressed',(win.days||[]).indexOf(d)>=0?'true':'false');
    b.addEventListener('click',()=>{b.setAttribute('aria-pressed',
      b.getAttribute('aria-pressed')==='true'?'false':'true'); ytDirty();});
    days.appendChild(b);
  }
  const times=document.createElement('div'); times.className='times';
  const start=document.createElement('input'); start.type='time';
  start.className='w-start'; start.value=win.start||'';
  const dash=document.createElement('span'); dash.textContent='\\u2013';
  const end=document.createElement('input'); end.type='time';
  end.className='w-end'; end.value=win.end||'';
  const del=document.createElement('button'); del.type='button'; del.className='w-del';
  del.textContent=fmtT({k:'yt.win.remove'});
  del.addEventListener('click',()=>{rowEl.remove(); ytDirty();});
  times.append(start,dash,end,del);
  rowEl.append(days,times);
  return rowEl;
}
function readYtSchedule(card){
  const wins=[...card.querySelectorAll('.win')].map(w=>{
    const win={days:[...w.querySelectorAll('.day')]
      .filter(b=>b.getAttribute('aria-pressed')==='true').map(b=>+b.dataset.day)};
    /* Empty time fields mean the whole day; the keys are omitted so the server sees
       exactly the shape it validates. */
    const s=w.querySelector('.w-start').value, e=w.querySelector('.w-end').value;
    if(s)win.start=s;
    if(e)win.end=e;
    return win;
  });
  return {mode:ytMode(card),windows:wins};
}
function paintYtSchedule(card){
  const sched=(SNAP&&SNAP.youtube_schedule)||{};
  const live=(SNAP&&SNAP.dns&&SNAP.dns.youtube||{}).user_rules;
  /* No schedule stored yet: pre-select what is actually live, but nothing enforces it
     until Apply is pressed. */
  const mode=sched.mode||(live==='BLOCKED'?'blocked':'allowed');
  const radio=card.querySelector('input[name=ytmode][value="'+mode+'"]');
  if(radio) radio.checked=true;
  const box=$('#yt-wins'); box.innerHTML='';
  (sched.windows||[]).forEach(w=>box.appendChild(ytWinRow(w)));
  ytToggleEditor(card);
  $('#yt-now').textContent = sched.desired?stw(sched.desired):(live?stw(live):'');
  $('#yt-next').innerHTML = sched.next?fmt(sched.next):'';
  /* The manual hold, shown against the STORED schedule (not the draft): the buttons
     act immediately, so they must reflect what is actually running. */
  const ovr=sched.override||null;
  $('#yt-ovr').hidden = sched.mode!=='schedule';
  $('#yt-allow-now').hidden = !!ovr&&ovr.state==='ALLOWED';
  $('#yt-block-now').hidden = !!ovr&&ovr.state==='BLOCKED';
  $('#yt-clear-ovr').hidden = !ovr;
  $('#yt-ovr-note').innerHTML = ovr&&ovr.note?fmt(ovr.note):'';
}

/* The slider bounds come from the server, which is also what clamps them. Two copies of
   a maximum drift, and the copy in the HTML is the one nobody remembers to change. */
function applyRanges(rs){
  if(!rs) return;
  $$('[data-ctl]').forEach(card=>{
    const key=card.dataset.ctl;
    const b=rs[key]||(key.endsWith('_delay_sec')?rs.delay_sec:null);
    if(!b) return;
    card.querySelectorAll('input[type=range]').forEach(r=>{r.min=b[0];r.max=b[1];});
  });
}

/* ---- Início ---- */
function ytChipContext(s){
  const sched=s.youtube_schedule||{};
  if(sched.mode==='schedule')
    return esc(fmtT({k:'chip.youtube.sched'}))+(sched.next?' \\u00b7 '+fmt(sched.next):'');
  return tr('chip.youtube.v');
}
function chips(s){
  const lane=k=>s.lanes.find(l=>l.key===k)||{};
  const rb=lane('roblox'), br=lane('browser'), gm=lane('games');
  const items=[
    {k:tr('chip.roblox'),s:rb.status,v:fmt(rb.reason)},
    {k:tr('chip.games'),s:gm.status,v:fmt(gm.reason)},
    {k:tr('chip.browser'),s:br.status,v:fmt(br.reason)},
    {k:tr('chip.resolver'),s:s.resolver.status,v:fmt(s.resolver.reason)},
    {k:tr('chip.dns'),s:s.dns.status,v:fmt(s.dns.reason)},
    {k:tr('chip.youtube'),s:(s.dns.youtube||{}).user_rules,v:ytChipContext(s)},
    {k:tr('chip.panel'),s:'ACTIVE',v:esc((s.host.bound||[]).join(', ')+':'+s.host.port)},
  ];
  return items.map(i=>'<div class="chip"><div class="k">'+esc(i.k)+'</div>'
    +'<div>'+pill(i.s)+'</div><div class="v">'+i.v+'</div></div>').join('');
}

function resolverBlock(r){
  const facts=r.facts.map(f=>row(fmt(f.label),fmt(f.value))).join('');
  const notes=(r.notes||[]).map(n=>'<div class="note">'+fmt(n)+'</div>').join('');
  const t=r.task;
  return '<div class="row head"><b>'+fmt(r.label)+'</b>'+pill(r.status)+'</div>'
    +'<p class="sub">'+fmt(r.reason)+'</p>'
    +'<div class="grid"><div class="card">'+facts+'</div>'
    +'<div class="card">'+row(tr('k.watchdog'),'<span class="mono">'+esc(t.name)+'</span>')
      +row(tr('k.state'),tstate(t))
      +row(tr('k.lastrun'),t.last_run?esc(t.last_run):tr('v.never'))
      +row(tr('k.lastresult'),fmt(t.last_result_text))
      +'<div style="margin-top:10px"><button class="primary wide" id="fixdns">'
      +tr('b.fixdns')+'</button></div>'
    +'</div></div>'+notes;
}

function gapsBlock(gaps){
  return gaps.map(g=>'<div class="gap '+(g.severity==='high'?'high':'')+'"><h4>'
    +fmt(g.title)
    +' <span class="pill s-'+(g.severity==='high'?'DEGRADED':'OFF')+'">'
    +esc((CAT[LANG]&&CAT[LANG]['sev.'+g.severity])||CAT.en['sev.'+g.severity]||g.severity)
    +'</span></h4>'
    +'<p>'+fmt(g.detail)+'</p><p class="verify mono">'+tr('g.verify')+' '
    +esc(g.verify)+'</p></div>').join('');
}

/* ---- Regras: one panel per lane, status + controls together ---- */
/* ---- the game list ----
   One row per experience the guard has ever seen, newest first, ticked when it is on
   the list. A row whose universe the client log never gave up is shown but cannot be
   ticked: an allow list has nothing to key it on, and hiding it would leave the parent
   wondering why Roblox closed on a game that is not in front of him. */
function gamesRows(g){
  if(!g||!g.catalog||!g.catalog.length)
    return '<div class="note">'+esc(fmtT({k:'games.empty'}))+'</div>';
  return g.catalog.map(r=>{
    const name=r.name||('universe '+esc(r.key));
    const seen=esc((r.last||'').replace('T',' '));
    /* The rating rides in the row rather than in a tooltip: the whole point is that it
       is in front of the parent at the moment he decides, not one tap away. */
    const mat=esc(fmtT({k:r.maturity?('games.mat.'+r.maturity):'games.mat.unknown'}));
    const badge='<b class="'+(r.over?'s-DEGRADED':'s-ACTIVE')+'">'+mat+'</b> \u00b7 ';
    const meta=badge+esc(fmtT({k:'games.joins',p:{n:r.joins}}))+(seen?' \u00b7 '+seen:'');
    if(r.id===null)
      return '<label class="switch"><input type="checkbox" disabled>'
        +'<span>'+esc(name)+' <span class="hint">'
        +esc(fmtT({k:'games.unidentified'}))+'</span></span></label>';
    return '<label class="switch"><input type="checkbox" value="'+r.id+'"'
      +(r.listed?' checked':'')+'><span>'+esc(name)
      +' <span class="hint">'+meta+'</span></span></label>';
  }).join('');
}

function lanePanel(l){
  const facts=l.facts.map(f=>row(fmt(f.label),fmt(f.value))).join('');
  const t=l.task, when=t.last_run?esc(t.last_run):tr('v.never');
  const taskline=row(tr('k.task'),'<span class="mono">'+esc(t.name)+'</span> &middot; '
    +tstate(t)+' &middot; '+when+' &middot; '+fmt(t.last_result_text));
  const last=(l.log&&l.log.last_event)
    ?row(tr('k.lastclose'),esc(l.log.last_event.at)):'';
  const notes=(l.notes||[]).map(n=>'<div class="note">'+fmt(n)+'</div>').join('');
  return '<p class="sub" style="margin-bottom:6px">'+fmt(l.reason)+'</p>'
    +'<div class="card">'+facts+taskline+last+'</div>'+notes;
}

function ytPanel(d,sched){
  const y=d.youtube||{};
  sched=sched||{};
  const schedRow=sched.mode?row(tr('k.yt.sched'),
    esc(tr('yt.mode.'+sched.mode))
    +(sched.mode==='schedule'&&sched.next?' \\u00b7 '+fmt(sched.next):'')):'';
  return '<div class="card">'
    +row(tr('k.yt.rules'),pill(y.user_rules))
    +row(tr('k.yt.service'),pill(y.service))
    +schedRow
    +'</div>'
    +'<details class="det"><summary>'+tr('d.why')+'</summary>'
    +'<p class="body">'+tr('det.ytlanes')+'</p></details>';
}

/* ---- DNS ---- */
function dnsBlock(d){
  const rows=(d.lists||[]).map(f=>'<tr>'
    +td(tr('t.list'),esc(f.name)+(f.kind==='allow'?' <i>'+esc(fmtT({k:'v.allow'}))+'</i>':'')
        +'<div class="mono">'+esc(f.url)+'</div>')
    +td(tr('t.state'),f.enabled?tr('v.on'):tr('v.off'))
    +td(tr('t.rules'),esc(f.rules_count==null?'?':f.rules_count),'num')
    +td(tr('t.updated'),f.last_updated?esc(f.last_updated):tr('v.never'))+'</tr>');
  const ss=d.safesearch||{};
  const engines=Object.keys(ss.engines||{})
    .map(k=>k+': '+(ss.engines[k]?tr('v.on'):tr('v.off'))).join(', ');
  return '<div class="row head"><b>'+esc(d.base_url)+'</b>'+pill(d.status)+'</div>'
    +'<p class="sub">'+fmt(d.reason)
    +(d.version?' &middot; '+fmt({k:'v.adguard',p:{v:d.version}}):'')+'</p>'
    +'<div class="grid">'
    +'<div class="card">'
      +row(tr('k.protection'),boolword(d.protection_enabled))
      +row(tr('k.filtering'),boolword(d.filtering_enabled))
      +row(tr('k.parental'),pill((d.parental||{}).status))
      +row(tr('k.safebrowsing'),pill((d.safebrowsing||{}).status))
      +row(tr('k.safesearch'),pill(ss.status))
      +(engines?row(tr('k.engines'),esc(engines)):'')
    +'</div>'
    +'<div class="card">'
      +row(tr('k.userrules'),d.user_rules_count==null?tr('v.unknown'):esc(d.user_rules_count))
      +row(tr('k.listsenabled'),fmt({k:'v.rules',p:{n:d.lists_enabled,rules:d.rules_total}}))
      +row(fmt({k:'k.pihole',p:{server:(d.pihole||{}).server}}),pill((d.pihole||{}).status))
      +row('',fmt((d.pihole||{}).detail))
    +'</div></div>'
    +'<div style="margin-top:12px">'+table(
      [{label:tr('t.list')},{label:tr('t.state')},{label:tr('t.rules'),num:true},
       {label:tr('t.updated')}],
      rows)+'</div>';
}

function probesBlock(p){
  if(!p){$('#probe-when').textContent='';
    return '<p class="sub">'+tr('probe.notrun')+'</p>';}
  $('#probe-when').innerHTML=fmt({k:'probe.when',p:{at:p.ran_at,server:p.resolver}});
  const rows=p.results.map(r=>'<tr>'
    +td(tr('t.probe'),fmt(r.label))
    +td(tr('t.domain'),'<span class="mono">'+(r.domain?esc(r.domain):'&mdash;')+'</span>')
    +td(tr('t.expected'),stw(r.expect))
    +td(tr('t.verdict'),pill(r.verdict)+(r.ok?'':' <b>&#9888;</b>'))
    +td(tr('t.detail'),fmt(r.detail)+(r.rule?'<div class="mono">'+esc(r.rule)+'</div>':'')
        +'<div class="stamp">'+fmt(r.why)+'</div>')+'</tr>');
  return table([{label:tr('t.probe')},{label:tr('t.domain')},{label:tr('t.expected')},
                {label:tr('t.verdict')},{label:tr('t.detail')}],rows);
}

/* ---- Sistema ---- */
function tasksBlock(rows){
  return table([{label:tr('t.task')},{label:tr('t.state')},{label:tr('k.lastrun')},
                {label:tr('k.lastresult')},{label:tr('k.nextrun')}],
    rows.map(t=>'<tr>'
      +td(tr('t.task'),'<span class="mono">'+esc(t.name)+'</span>')
      +td(tr('t.state'),t.registered===null?pill('UNKNOWN')
          :t.registered?tstate(t):'<i>'+tr('ts.NotRegistered')+'</i>')
      +td(tr('k.lastrun'),t.last_run?esc(t.last_run):tr('v.never'))
      +td(tr('k.lastresult'),fmt(t.last_result_text))
      +td(tr('k.nextrun'),t.next_run?esc(t.next_run):'&mdash;')+'</tr>'));
}

function policyBlock(p){
  if(!p.keys.length) return '<p class="sub">'+fmt(p.reason)+'</p>';
  return '<div class="row head"><span>'+fmt(p.reason)+'</span>'+pill(p.status)+'</div>'
    +p.keys.map(k=>'<div class="card" style="margin-top:10px"><div class="row head">'
    +'<span class="mono">'+esc(k.key)+'</span>'+pill(k.status)+'</div>'
    +table([{label:tr('t.policy')},{label:tr('t.value')},{label:tr('t.wanted')}],
      k.rows.map(r=>'<tr>'
        +td(tr('t.policy'),'<span class="mono">'+esc(r.name)+'</span>')
        +td(tr('t.value'),r.value==null?tr('v.notset'):esc(r.value))
        +td(tr('t.wanted'),r.ok?tr('v.ok'):fmt({k:'v.want',p:{x:r.expected}}))+'</tr>'))
    +'</div>').join('');
}

/* ---- the one render ---- */
function render(s){
  SNAP=s;
  $('#host').innerHTML=fmt({k:'hm.host',
    p:{m:s.host.machine,a:(s.host.bound.join(', ')||'?')+':'+s.host.port}});
  $('#clock').innerHTML=fmt({k:'hm.updated',p:{t:s.now.stamp}});
  $('#chips').innerHTML=chips(s);
  $('#resolver').innerHTML=resolverBlock(s.resolver);
  $('#gaps').innerHTML=gapsBlock(s.gaps);
  const lane=k=>s.lanes.find(l=>l.key===k)||{facts:[],task:{},notes:[]};
  const rb=lane('roblox'), br=lane('browser'), gm=lane('games');
  $('#pill-roblox').innerHTML=pill(rb.status);
  $('#lane-roblox').innerHTML=lanePanel(rb);
  $('#pill-games').innerHTML=pill(gm.status);
  $('#lane-games').innerHTML=lanePanel(gm);
  /* Redrawing the rows under a parent who is mid-edit would throw their ticks away, so
     a dirty list keeps what is on screen until it is applied or the page is reloaded. */
  if(!dirty.has('games_list')){
    $('#games-list').innerHTML=gamesRows(s.games);
    refreshOutputs($('[data-ctl=games_list]'));
  }
  $('#pill-browser').innerHTML=pill(br.status);
  $('#lane-browser').innerHTML=lanePanel(br);
  $('#pill-youtube').innerHTML=pill((s.dns.youtube||{}).user_rules);
  $('#lane-youtube').innerHTML=ytPanel(s.dns,s.youtube_schedule);
  $('#dns').innerHTML=dnsBlock(s.dns);
  $('#probes').innerHTML=probesBlock(s.probes);
  $('#tasks').innerHTML=tasksBlock(s.tasks);
  $('#policy').innerHTML=policyBlock(s.policy);
  $('#foot').innerHTML=fmt({k:'foot',p:{path:s.host.root+'\\\\panel.log'}});
  const btn=$('#fixdns');
  if(btn) btn.addEventListener('click',async ()=>{
    btn.disabled=true; btn.textContent=fmtT({k:'b.fixdns.busy'});
    await apply('resolver_start',true);
  });
  applyRanges(s.controls.ranges);
  const v=s.controls.values;
  $$('[data-ctl]').forEach(card=>{
    if(dirty.has(card.dataset.ctl)) return;
    paintControl(card,v[card.dataset.ctl]);
  });
}

async function refresh(){
  try{
    const r=await fetch('/api/status',{credentials:'same-origin'});
    if(r.status===401){location.reload();return;}
    render(await r.json());
  }catch(e){ /* a poll that fails leaves the last good numbers on screen */ }
}

$$('[data-ctl]').forEach(card=>{
  const touched=()=>{dirty.add(card.dataset.ctl);refreshOutputs(card);
    if(card.dataset.ctl==='youtube_schedule'){
      ytToggleEditor(card);
      /* The canonical case is "weekends are open": switching to schedule mode with
         nothing drafted seeds one all-day weekend window, ready to Apply. */
      if(ytMode(card)==='schedule'&&!card.querySelector('.win'))
        $('#yt-wins').appendChild(ytWinRow({days:[5,6]}));
    }};
  card.addEventListener('input',touched);
  card.addEventListener('change',touched);
  card.querySelector('[data-apply]').addEventListener('click',async ()=>{
    const btn=card.querySelector('[data-apply]'); btn.disabled=true;
    try{ await apply(card.dataset.ctl,readControl(card)); } finally { btn.disabled=false; }
  });
});
$('#yt-addwin').addEventListener('click',()=>{
  $('#yt-wins').appendChild(ytWinRow({days:[5,6]}));
  ytDirty();
});
[['#yt-allow-now','ALLOWED'],['#yt-block-now','BLOCKED'],['#yt-clear-ovr',null]]
  .forEach(([sel,state])=>{
    $(sel).addEventListener('click',async ()=>{
      const btn=$(sel); btn.disabled=true;
      try{ await apply('youtube_override',{state:state}); }
      finally { btn.disabled=false; }
    });
  });
$('#probe').addEventListener('click',async ()=>{
  const b=$('#probe'); b.disabled=true; b.textContent=fmtT({k:'b.probe.busy'});
  const res=await post('/api/probe',{});
  b.disabled=false; b.textContent=fmtT({k:'b.probe'});
  if(res.status===200){
    if(SNAP)SNAP.probes=res.body.probes;
    $('#probes').innerHTML=probesBlock(res.body.probes);
  }
  else toast(res.body.error||{k:'err.probes'},false);
});
$('#pwform').addEventListener('submit',async e=>{
  e.preventDefault();
  const err=$('#pw-err'); err.textContent='';
  const res=await post('/api/password',{current:$('#pw-cur').value,
    new:$('#pw-new').value,confirm:$('#pw-again').value});
  if(res.status===200){
    ['#pw-cur','#pw-new','#pw-again'].forEach(i=>{$(i).value='';});
    toast(res.body.message||{k:'ok.pwchanged'},true);
    if(res.body.advice) toast(res.body.advice,false);
  } else {
    err.textContent=res.body.error?fmtT(res.body.error)
      :fmtT({k:'toast.failed',p:{s:res.status}});
  }
});
$('#logout').addEventListener('click',async ()=>{await post('/api/logout',{});location.reload();});

/* Switching language re-renders the whole snapshot; dirty controls keep their values
   because paintControl skips them and text inputs are never rewritten. */
initLang(()=>{if(SNAP)render(SNAP);});
refresh();
setInterval(refresh,10000);
</script>
""".replace("__LANG__", _LANG).replace("__I18N__", _I18N_JS)


def render_panel() -> str:
    return _doc(_BODY)
