"""The page itself: one self-contained HTML document per screen.

**No external requests of any kind.** No CDN, no web font, no icon service, no analytics.
Partly because this page is a control surface for a filtering system and must not depend
on the thing it filters, and mostly because the moment it is most needed is the moment
the network is broken - a dashboard that renders blank when DNS is down is a dashboard
that cannot tell you DNS is down.

Everything is rendered client-side from `/api/status`, so the refresh is one fetch and
the server has one representation of the truth to keep correct.

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

/* ---- header ---- */
header{position:sticky;top:0;z-index:5;background:var(--card);
  border-bottom:1px solid var(--line);padding:10px 14px;
  display:grid;grid-template-columns:1fr auto;gap:4px 12px;align-items:center}
header h1{font-size:17px;margin:0;letter-spacing:.2px;font-weight:700}
header .meta{grid-column:1/-1;display:flex;flex-wrap:wrap;gap:4px 14px;
  color:var(--muted);font-size:12px}
@media (min-width:720px){
  header{grid-template-columns:auto 1fr auto;padding:10px 20px}
  header .meta{grid-column:auto;justify-content:flex-end}
}

/* ---- shell ---- */
main{max-width:1120px;margin:0 auto;padding:14px;display:grid;gap:14px}
@media (min-width:720px){ main{padding:20px;gap:18px} }
section{background:var(--card);border:1px solid var(--line);border-radius:var(--r);
  padding:14px;box-shadow:var(--shadow)}
@media (min-width:720px){ section{padding:18px 20px} }
h2{font-size:12px;margin:0 0 3px;letter-spacing:.7px;text-transform:uppercase;
  color:var(--muted);font-weight:700}
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
.err{color:var(--bad);font-size:13px;min-height:18px}
.stamp{color:var(--muted);font-size:12.5px}
"""

_HEAD = """<!doctype html>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="referrer" content="no-referrer">
<meta name="color-scheme" content="light dark">
<title>SafeHouse control panel</title>
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16'><text y='14' font-size='14'>&#127968;</text></svg>">
<style>__CSS__</style>
"""


def _doc(body: str) -> str:
    return _HEAD.replace("__CSS__", _CSS) + body


def render_setup() -> str:
    """Shown when no password has been set yet. Deliberately not a way to set one.

    The panel is started by a SYSTEM scheduled task at boot, where there is no console to
    print a generated password to and nobody to read it. Letting the first visitor pick
    the password instead would mean whoever reaches the page first owns it - on a LAN the
    child is also on. So the password is set at the machine, elevated, by hand; after
    that it can be changed from inside the panel, which needs the old one.
    """
    return _doc("""
<div class="center">
  <h1>SafeHouse control panel</h1>
  <p>No password has been set yet, so the controls are not available.</p>
  <p>On the machine itself, in an <b>elevated</b> prompt:</p>
  <p><code>C:\\SafeHouse\\python\\python.exe -m safehouse_panel set-password --generate</code></p>
  <p>That writes the new password to
  <code>C:\\ProgramData\\SafeHouse\\panel-password.txt</code>, which only Administrators
  can read. Store it, then delete that file.</p>
  <p class="stamp">The panel keeps only a hash. Nothing on this page can set the password -
  if it could, whoever opened this page first would own the panel. Once you are signed in
  you can change it under <b>Panel access</b>, which asks for the current one.</p>
</div>
""")


def render_login() -> str:
    return _doc("""
<div class="center">
  <h1>SafeHouse</h1>
  <p class="stamp">Parental controls for this PC. Sign in to see and change what is
  enforced.</p>
  <form id="f" class="stack" autocomplete="off">
    <input type="password" id="pw" placeholder="Panel password" autofocus
           autocomplete="current-password" enterkeyhint="go">
    <p class="err" id="err"></p>
    <button class="primary wide" type="submit">Sign in</button>
  </form>
  <p class="stamp">This connection is not encrypted (no HTTPS). Use it on the house
  network only.</p>
</div>
<script>
const f=document.getElementById('f'),pw=document.getElementById('pw'),err=document.getElementById('err');
f.addEventListener('submit',async e=>{
  e.preventDefault(); err.textContent='';
  const r=await fetch('/api/login',{method:'POST',headers:{'Content-Type':'application/json'},
    credentials:'same-origin',body:JSON.stringify({password:pw.value})});
  const j=await r.json().catch(()=>({}));
  if(r.ok){location.reload();return;}
  err.textContent=j.error||('Sign-in failed ('+r.status+').');
  pw.select();
});
</script>
""")


_BODY = """
<header>
  <h1>SafeHouse</h1>
  <button id="logout">Sign out</button>
  <div class="meta"><span id="host"></span><span id="clock"></span></div>
</header>
<main>
  <section>
    <h2>Right now <span class="hint">every lane, in one line each</span></h2>
    <div class="chips" id="chips"></div>
  </section>

  <section>
    <h2>Filtering resolver <span class="hint">the WSL machine every DNS block lives in</span></h2>
    <p class="sub">When this is down nothing is filtered <b>and nothing looks wrong</b>:
      the relay falls back to the router, so names keep resolving and browsing keeps
      working with no blocking behind it.</p>
    <div id="resolver"></div>
  </section>

  <section>
    <h2>Enforcement lanes <span class="hint">what is actually being enforced right now</span></h2>
    <div class="grid wide" id="lanes"></div>
  </section>

  <section>
    <h2>Settings <span class="hint">every change is written to disk and logged</span></h2>
    <p class="sub">Changes that make a protection <b>weaker</b> ask for the panel password
      again. Changes that tighten one do not.</p>
    <div class="grid" id="controls">

      <div class="ctl" data-ctl="roblox_limit_min">
        <div class="ctl-head"><h3>Roblox &mdash; per day</h3><output>0 min</output></div>
        <div class="slide"><input type="range" min="0" max="240" step="5" value="0"
             aria-label="Roblox minutes per day"></div>
        <p>0 means the game is closed within about a minute of launching. The standing
          policy on this PC is 0.</p>
        <div class="act"><button class="primary wide" data-apply>Apply</button></div>
      </div>

      <div class="ctl" data-ctl="roblox_bonus_min">
        <div class="ctl-head"><h3>Roblox &mdash; extra today</h3><output>0 min</output></div>
        <div class="slide"><input type="range" min="0" max="240" step="5" value="0"
             aria-label="Roblox extra minutes today"></div>
        <p>A one-off grant for today only. It is written into the state file and expires
          at midnight, so it never becomes the new limit.</p>
        <div class="act"><button class="primary wide" data-apply>Grant</button></div>
      </div>

      <div class="ctl" data-ctl="browser_enabled">
        <div class="ctl-head"><h3>Web browsing &mdash; guard</h3></div>
        <label class="switch"><input type="checkbox"><span>Close any browser that
          opens</span></label>
        <p>Turning this off has no expiry. For a timed pass, use the unlock below.</p>
        <div class="act"><button class="primary wide" data-apply>Apply</button></div>
      </div>

      <div class="ctl" data-ctl="browser_unlock_min">
        <div class="ctl-head"><h3>Web browsing &mdash; unlock</h3><output>0 min</output></div>
        <div class="slide"><input type="range" min="0" max="240" step="5" value="0"
             aria-label="Unlock browsing for minutes"></div>
        <p>Suspends the browser guard, then it resumes by itself. Use this before reading
          this panel <i>on the guarded PC</i> &mdash; otherwise the guard closes the
          browser you are reading it in, within about a minute.</p>
        <div class="act"><button class="primary wide" data-apply>Apply</button></div>
      </div>

      <div class="ctl" data-ctl="browser_limit_min">
        <div class="ctl-head"><h3>Web browsing &mdash; per day</h3><output>0 min</output></div>
        <div class="slide"><input type="range" min="0" max="240" step="5" value="0"
             aria-label="Browser minutes per day"></div>
        <p><b>Stored, not enforced yet.</b> The browser lane has no metering today: it
          closes every browser it finds. This value is written to the config so the
          metering work has the parent's number to honour.</p>
        <div class="act"><button class="primary wide" data-apply>Apply</button></div>
      </div>

      <div class="ctl" data-ctl="browser_notify">
        <div class="ctl-head"><h3>Tell the child</h3></div>
        <label class="switch"><input type="checkbox"><span>Show a message when a browser
          is closed</span></label>
        <p>Currently <b>off</b>, at the parent's instruction: he sees the window close but
          is not told what closed it. Flip only on purpose.</p>
        <div class="act"><button class="primary wide" data-apply>Apply</button></div>
      </div>

      <div class="ctl" data-ctl="roblox_delay_sec">
        <div class="ctl-head"><h3>Roblox &mdash; pause</h3><output>1&ndash;5s</output></div>
        <div class="dual">
          <div class="slide"><span class="lbl">soonest</span>
            <input type="range" min="1" max="60" step="1" value="1" data-part="min"
                   aria-label="Roblox shortest pause, seconds">
            <span class="val" data-val="min">1s</span></div>
          <div class="slide"><span class="lbl">latest</span>
            <input type="range" min="1" max="60" step="1" value="5" data-part="max"
                   aria-label="Roblox longest pause, seconds">
            <span class="val" data-val="max">5s</span></div>
        </div>
        <p>A random wait between seeing the game and ending it, so the moment cannot be
          timed &mdash; and so quitting during it is not logged as a kill.</p>
        <div class="act"><button class="primary wide" data-apply>Apply</button></div>
      </div>

      <div class="ctl" data-ctl="browser_delay_sec">
        <div class="ctl-head"><h3>Browser &mdash; pause</h3><output>1&ndash;5s</output></div>
        <div class="dual">
          <div class="slide"><span class="lbl">soonest</span>
            <input type="range" min="1" max="60" step="1" value="1" data-part="min"
                   aria-label="Browser shortest pause, seconds">
            <span class="val" data-val="min">1s</span></div>
          <div class="slide"><span class="lbl">latest</span>
            <input type="range" min="1" max="60" step="1" value="5" data-part="max"
                   aria-label="Browser longest pause, seconds">
            <span class="val" data-val="max">5s</span></div>
        </div>
        <p>Same pause, for the browser lane. Capped at 60s: the task is terminated after
          5 minutes, and a pause the scheduler cuts off is a browser that survives.</p>
        <div class="act"><button class="primary wide" data-apply>Apply</button></div>
      </div>

      <div class="ctl" data-ctl="youtube_blocked">
        <div class="ctl-head"><h3>YouTube at the resolver</h3></div>
        <label class="switch"><input type="checkbox"><span>Blocked for every device using
          this resolver</span></label>
        <p>Drives AdGuard's user rules. This is the only lane that also covers the Play
          Games VM, a phone or a TV. It takes YouTube Music with it.</p>
        <div class="act"><button class="primary wide" data-apply>Apply</button></div>
      </div>

    </div>
  </section>

  <section>
    <h2>DNS filtering</h2>
    <div id="dns"></div>
  </section>

  <section>
    <h2>Live verification <span class="hint">resolves real names, reads AdGuard's verdict</span></h2>
    <p class="sub">The verdict is AdGuard's query-log <code>reason</code>, never the
      address that came back: a parental block answers with <code>94.140.14.35</code>,
      AdGuard's block page, so judging by IP reports a working block as a leak.</p>
    <div class="actions" style="justify-content:flex-start;align-items:center">
      <button id="probe" class="primary">Run the probes</button>
      <span class="stamp" id="probe-when"></span></div>
    <div id="probes" style="margin-top:12px"></div>
  </section>

  <section>
    <h2>Scheduled tasks</h2>
    <div id="tasks"></div>
  </section>

  <section>
    <h2>Browser policy <span class="hint">machine policy in HKLM, out of reach of browser settings</span></h2>
    <div id="policy"></div>
  </section>

  <section>
    <h2>Panel access</h2>
    <p class="sub">Changing the password signs every other device out, including any phone
      that is still holding a session. It needs the current password, so a session left
      open on an unlocked phone cannot be used to lock you out of your own controls.</p>
    <form id="pwform" class="stack" autocomplete="off" style="max-width:420px">
      <label class="field">Current password
        <input type="password" id="pw-cur" autocomplete="current-password"></label>
      <label class="field">New password
        <input type="password" id="pw-new" autocomplete="new-password"></label>
      <label class="field">New password again
        <input type="password" id="pw-again" autocomplete="new-password"></label>
      <p class="err" id="pw-err"></p>
      <div><button class="primary" type="submit">Change password</button></div>
    </form>
  </section>

  <section>
    <h2>Known gaps <span class="hint">accepted, not errors &mdash; and deliberately not hidden</span></h2>
    <div id="gaps"></div>
  </section>
  <p class="stamp" id="foot"></p>
</main>
<div id="toast"></div>
<div id="modal"><form class="box" id="mform">
  <h3 id="mtitle">This change weakens a protection</h3>
  <p class="stamp" id="mtext"></p>
  <input type="password" id="mpw" placeholder="Panel password" autocomplete="current-password">
  <p class="err" id="merr"></p>
  <div class="actions">
    <button type="button" id="mcancel">Cancel</button>
    <button type="submit" class="primary">Confirm</button>
  </div>
</form></div>
<script>
const $=s=>document.querySelector(s), $$=s=>[...document.querySelectorAll(s)];
let SNAP=null;

/* A control the parent is touching is never overwritten by the 10s refresh: dragging a
   slider while a poll lands would yank it back under the finger. `dirty` is set on the
   first input and cleared when the value is applied or the card is reset. */
const dirty=new Set();

function pill(s){return '<span class="pill s-'+(s||'UNKNOWN')+'">'+(s||'UNKNOWN')+'</span>';}
function esc(v){return String(v==null?'':v).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));}
/* Every cell carries its column name, which the stylesheet promotes to a label when the
   table folds into cards under 720px. A table without these is unreadable on a phone. */
function td(label,html,cls){return '<td'+(cls?' class="'+cls+'"':'')+' data-label="'+esc(label)
  +'"><div class="cv">'+html+'</div></td>';}
function table(cols,rows){
  if(!rows.length) return '<p class="sub">Nothing to show.</p>';
  return '<div class="tw"><table class="rt"><thead><tr>'
    +cols.map(c=>'<th'+(c.num?' class="num"':'')+'>'+esc(c.label)+'</th>').join('')
    +'</tr></thead><tbody>'+rows.join('')+'</tbody></table></div>';
}

function toast(msg,ok){
  const t=document.createElement('div'); t.className='toast '+(ok?'ok':'bad');
  t.textContent=msg; $('#toast').appendChild(t);
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
    const pw=await askPassword(res.body.error||'This change needs the panel password.');
    if(pw===null){toast('Cancelled - nothing was changed.',false);return false;}
    return apply(action,value,pw);
  }
  const ok=res.status===200&&res.body.ok!==false;
  toast(res.body.message||res.body.error||('Failed ('+res.status+').'),ok);
  dirty.delete(action);
  await refresh();
  return ok;
}

function readControl(card){
  const box=card.querySelector('input[type=checkbox]');
  if(box) return box.checked;
  const parts=card.querySelectorAll('input[type=range]');
  if(parts.length===2) return {min_sec:+parts[0].value,max_sec:+parts[1].value};
  return +parts[0].value;
}

function paintControl(card,value){
  const key=card.dataset.ctl;
  const box=card.querySelector('input[type=checkbox]');
  const ranges=card.querySelectorAll('input[type=range]');
  if(box){ box.checked = key==='youtube_blocked' ? value==='BLOCKED' : !!value; return; }
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
  if(ranges.length===2){
    const lo=card.querySelector('[data-val=min]'), hi=card.querySelector('[data-val=max]');
    if(lo) lo.textContent=ranges[0].value+'s';
    if(hi) hi.textContent=ranges[1].value+'s';
    if(out) out.textContent=ranges[0].value+'\\u2013'+ranges[1].value+'s';
  } else if(ranges.length===1 && out){
    out.textContent=ranges[0].value+' min';
  }
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

function chips(s){
  const lane=k=>s.lanes.find(l=>l.key===k)||{};
  const rb=lane('roblox'), br=lane('browser');
  const items=[
    {k:'Roblox',s:rb.status,v:rb.reason},
    {k:'Browsing',s:br.status,v:br.reason},
    {k:'Resolver',s:s.resolver.status,v:s.resolver.reason},
    {k:'DNS filtering',s:s.dns.status,v:s.dns.reason},
    {k:'YouTube',s:(s.dns.youtube||{}).user_rules,v:'at the resolver, for every device'},
    {k:'Panel',s:'ACTIVE',v:(s.host.bound||[]).join(', ')+':'+s.host.port},
  ];
  return items.map(i=>'<div class="chip"><div class="k">'+esc(i.k)+'</div>'
    +'<div>'+pill(i.s)+'</div><div class="v">'+esc(i.v)+'</div></div>').join('');
}

function resolverBlock(r){
  const facts=r.facts.map(f=>'<div class="row"><span>'+esc(f.label)+'</span><span>'
    +esc(f.value)+'</span></div>').join('');
  const notes=(r.notes||[]).map(n=>'<div class="note">'+esc(n)+'</div>').join('');
  const t=r.task;
  return '<div class="row head"><b>'+esc(r.label)+'</b>'+pill(r.status)+'</div>'
    +'<p class="sub">'+esc(r.reason)+'</p>'
    +'<div class="grid"><div class="card">'+facts+'</div>'
    +'<div class="card"><div class="row"><span>Watchdog task</span><span>'
      +esc(t.name)+'</span></div>'
      +'<div class="row"><span>State</span><span>'
      +(t.registered===null?'unknown':t.registered?esc(t.state):'not registered')+'</span></div>'
      +'<div class="row"><span>Last run</span><span>'+esc(t.last_run||'never')+'</span></div>'
      +'<div class="row"><span>Last result</span><span>'+esc(t.last_result_text)+'</span></div>'
      +'<div style="margin-top:10px"><button class="primary wide" id="fixdns">Start it now</button></div>'
    +'</div></div>'+notes;
}

function laneCard(l){
  const facts=l.facts.map(f=>'<div class="row"><span>'+esc(f.label)+'</span><span>'+esc(f.value)+'</span></div>').join('');
  const notes=(l.notes||[]).map(n=>'<div class="note">'+esc(n)+'</div>').join('');
  const t=l.task, when=t.last_run?('last ran '+esc(t.last_run)):'never ran';
  return '<div class="card"><div class="row head"><b>'+esc(l.label)+'</b>'+pill(l.status)+'</div>'
    +'<p class="sub">'+esc(l.how)+' &mdash; '+esc(l.reason)+'</p>'+facts
    +'<div class="row"><span>Task</span><span>'+esc(t.name)+' &middot; '+esc(t.state)+' &middot; '
    +when+' &middot; '+esc(t.last_result_text)+'</span></div>'
    +(l.log&&l.log.last_event?'<div class="row"><span>Last close</span><span>'+esc(l.log.last_event.at)+'</span></div>':'')
    +notes+'</div>';
}

function dnsBlock(d){
  const rows=(d.lists||[]).map(f=>'<tr>'
    +td('Filter list',esc(f.name)+(f.kind==='allow'?' <i>(allow)</i>':'')
        +'<div class="mono">'+esc(f.url)+'</div>')
    +td('State',f.enabled?'on':'off')
    +td('Rules',esc(f.rules_count==null?'?':f.rules_count),'num')
    +td('Updated',esc(f.last_updated||'never'))+'</tr>');
  const ss=d.safesearch||{}, engines=Object.keys(ss.engines||{}).map(k=>k+': '+(ss.engines[k]?'on':'off')).join(', ');
  return '<div class="row head"><b>'+esc(d.base_url)+'</b>'+pill(d.status)+'</div>'
    +'<p class="sub">'+esc(d.reason)+(d.version?' &middot; AdGuard '+esc(d.version):'')+'</p>'
    +'<div class="grid">'
    +'<div class="card">'
      +'<div class="row"><span>Protection</span><span>'+esc(d.protection_enabled)+'</span></div>'
      +'<div class="row"><span>Filtering</span><span>'+esc(d.filtering_enabled)+'</span></div>'
      +'<div class="row"><span>Parental control</span>'+pill((d.parental||{}).status)+'</div>'
      +'<div class="row"><span>Safe Browsing</span>'+pill((d.safebrowsing||{}).status)+'</div>'
      +'<div class="row"><span>Safe search</span>'+pill(ss.status)+'</div>'
      +(engines?'<div class="row"><span>Engines</span><span>'+esc(engines)+'</span></div>':'')
      +'<div class="row"><span>User rules</span><span>'+esc(d.user_rules_count==null?'unknown':d.user_rules_count)+'</span></div>'
      +'<div class="row"><span>Lists enabled</span><span>'+esc(d.lists_enabled)+' ('+esc(d.rules_total)+' rules)</span></div>'
    +'</div>'
    +'<div class="card">'
      +'<div class="row"><span>YouTube &mdash; user rules</span>'+pill((d.youtube||{}).user_rules)+'</div>'
      +'<div class="row"><span>YouTube &mdash; blocked service</span>'+pill((d.youtube||{}).service)+'</div>'
      +'<div class="row"><span>Pi-hole ('+esc((d.pihole||{}).server)+')</span>'+pill((d.pihole||{}).status)+'</div>'
      +'<div class="row"><span></span><span>'+esc((d.pihole||{}).detail)+'</span></div>'
      +'<div class="note">The two YouTube rows are independent lanes. The panel drives the'
      +' user rules; the watch-time budget and <code>toggle youtube</code> drive the blocked service.</div>'
    +'</div></div>'
    +'<div style="margin-top:12px">'+table(
      [{label:'Filter list'},{label:'State'},{label:'Rules',num:true},{label:'Updated'}],
      rows)+'</div>';
}

function probesBlock(p){
  if(!p) return '<p class="sub">Not run yet in this session.</p>';
  $('#probe-when').textContent='last run '+p.ran_at+' against '+p.resolver;
  const rows=p.results.map(r=>'<tr>'
    +td('Probe',esc(r.label))
    +td('Domain','<span class="mono">'+esc(r.domain||'&mdash;')+'</span>')
    +td('Expected',esc(r.expect))
    +td('Verdict',pill(r.verdict)+(r.ok?'':' <b>&#9888;</b>'))
    +td('Detail',esc(r.detail)+(r.rule?'<div class="mono">'+esc(r.rule)+'</div>':'')
        +'<div class="stamp">'+esc(r.why)+'</div>')+'</tr>');
  return table([{label:'Probe'},{label:'Domain'},{label:'Expected'},{label:'Verdict'},
                {label:'Detail'}],rows);
}

function tasksBlock(rows){
  return table([{label:'Task'},{label:'State'},{label:'Last run'},{label:'Last result'},
                {label:'Next run'}],
    rows.map(t=>'<tr>'
      +td('Task','<span class="mono">'+esc(t.name)+'</span>')
      +td('State',t.registered===null?pill('UNKNOWN')
          :t.registered?esc(t.state):'<i>not registered</i>')
      +td('Last run',esc(t.last_run||'never'))
      +td('Last result',esc(t.last_result_text))
      +td('Next run',esc(t.next_run||'&mdash;'))+'</tr>'));
}

function policyBlock(p){
  if(!p.keys.length) return '<p class="sub">'+esc(p.reason)+'</p>';
  return '<div class="row head"><span>'+esc(p.reason)+'</span>'+pill(p.status)+'</div>'
    +p.keys.map(k=>'<div class="card" style="margin-top:10px"><div class="row head">'
    +'<span class="mono">'+esc(k.key)+'</span>'+pill(k.status)+'</div>'
    +table([{label:'Policy'},{label:'Value'},{label:'Wanted'}],
      k.rows.map(r=>'<tr>'
        +td('Policy','<span class="mono">'+esc(r.name)+'</span>')
        +td('Value',esc(r.value==null?'not set':r.value))
        +td('Wanted',r.ok?'ok':'want '+esc(r.expected))+'</tr>'))
    +'</div>').join('');
}

function gapsBlock(gaps){
  return gaps.map(g=>'<div class="gap '+(g.severity==='high'?'high':'')+'"><h4>'+esc(g.title)
    +' <span class="pill s-'+(g.severity==='high'?'DEGRADED':'OFF')+'">'+esc(g.severity)+'</span></h4>'
    +'<p>'+esc(g.detail)+'</p><p class="verify mono">verify: '+esc(g.verify)+'</p></div>').join('');
}

function render(s){
  SNAP=s;
  $('#host').textContent=s.host.machine+' \\u00b7 listening on '
    +(s.host.bound.join(', ')||'?')+':'+s.host.port;
  $('#clock').textContent='updated '+s.now.stamp;
  $('#chips').innerHTML=chips(s);
  $('#resolver').innerHTML=resolverBlock(s.resolver);
  $('#lanes').innerHTML=s.lanes.map(laneCard).join('');
  $('#dns').innerHTML=dnsBlock(s.dns);
  $('#probes').innerHTML=probesBlock(s.probes);
  $('#tasks').innerHTML=tasksBlock(s.tasks);
  $('#policy').innerHTML=policyBlock(s.policy);
  $('#gaps').innerHTML=gapsBlock(s.gaps);
  $('#foot').textContent='Ranges are enforced on the server as well as here. '
    +'Every change is written to '+s.host.root+'\\\\panel.log with the time and the address it came from.';
  const btn=$('#fixdns');
  if(btn) btn.addEventListener('click',async ()=>{
    btn.disabled=true; btn.textContent='Starting...';
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
  card.addEventListener('input',()=>{dirty.add(card.dataset.ctl);refreshOutputs(card);});
  card.querySelector('[data-apply]').addEventListener('click',async ()=>{
    const btn=card.querySelector('[data-apply]'); btn.disabled=true;
    try{ await apply(card.dataset.ctl,readControl(card)); } finally { btn.disabled=false; }
  });
});
$('#probe').addEventListener('click',async ()=>{
  const b=$('#probe'); b.disabled=true; b.textContent='Resolving...';
  const res=await post('/api/probe',{});
  b.disabled=false; b.textContent='Run the probes';
  if(res.status===200){ $('#probes').innerHTML=probesBlock(res.body.probes); }
  else toast(res.body.error||'Probes failed.',false);
});
$('#pwform').addEventListener('submit',async e=>{
  e.preventDefault();
  const err=$('#pw-err'); err.textContent='';
  const res=await post('/api/password',{current:$('#pw-cur').value,
    new:$('#pw-new').value,confirm:$('#pw-again').value});
  if(res.status===200){
    ['#pw-cur','#pw-new','#pw-again'].forEach(i=>{$(i).value='';});
    toast(res.body.message||'Password changed.',true);
    if(res.body.advice) toast(res.body.advice,false);
  } else {
    err.textContent=res.body.error||('Failed ('+res.status+').');
  }
});
$('#logout').addEventListener('click',async ()=>{await post('/api/logout',{});location.reload();});

refresh();
setInterval(refresh,10000);
</script>
"""


def render_panel() -> str:
    return _doc(_BODY)
