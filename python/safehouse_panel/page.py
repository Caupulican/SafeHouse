"""The page itself: one self-contained HTML document per screen.

**No external requests of any kind.** No CDN, no web font, no icon service, no analytics.
Partly because this page is a control surface for a filtering system and must not depend
on the thing it filters, and mostly because the moment it is most needed is the moment
the network is broken - a dashboard that renders blank when DNS is down is a dashboard
that cannot tell you DNS is down.

Everything is rendered client-side from `/api/status`, so the refresh is one fetch and
the server has one representation of the truth to keep correct.
"""

from __future__ import annotations

_CSS = """
:root{
  --bg:#f4f5f7; --card:#ffffff; --ink:#16181d; --muted:#5c6370; --line:#e2e5ea;
  --accent:#2a5bd7; --ok:#12703a; --ok-bg:#e3f5ea; --warn:#8a5a00; --warn-bg:#fdf1d8;
  --bad:#a3231d; --bad-bg:#fbe6e5; --unk:#4a4f8a; --unk-bg:#e9e9f7; --shadow:0 1px 2px rgba(0,0,0,.06);
}
@media (prefers-color-scheme:dark){
  :root{
    --bg:#14161a; --card:#1c1f25; --ink:#e8eaee; --muted:#a0a7b4; --line:#2c313a;
    --accent:#7aa2ff; --ok:#6cd39a; --ok-bg:#123424; --warn:#e8b755; --warn-bg:#3a2d10;
    --bad:#ff8a80; --bad-bg:#3a1a18; --unk:#b9b9ee; --unk-bg:#232449; --shadow:none;
  }
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
  font:15px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,Helvetica,Arial,sans-serif}
header{position:sticky;top:0;z-index:5;background:var(--card);border-bottom:1px solid var(--line);
  padding:10px 16px;display:flex;gap:12px;align-items:center;flex-wrap:wrap}
header h1{font-size:17px;margin:0;letter-spacing:.2px}
header .grow{flex:1}
main{max-width:1100px;margin:0 auto;padding:16px;display:grid;gap:16px}
section{background:var(--card);border:1px solid var(--line);border-radius:10px;
  padding:14px 16px;box-shadow:var(--shadow)}
h2{font-size:15px;margin:0 0 4px;letter-spacing:.3px;text-transform:uppercase;color:var(--muted)}
h2 .hint{text-transform:none;letter-spacing:0;font-weight:400}
.sub{color:var(--muted);font-size:13px;margin:0 0 12px}
.grid{display:grid;gap:12px;grid-template-columns:repeat(auto-fit,minmax(280px,1fr))}
.card{border:1px solid var(--line);border-radius:8px;padding:12px}
.pill{display:inline-block;padding:2px 9px;border-radius:999px;font-size:11px;font-weight:700;
  letter-spacing:.6px}
.s-ACTIVE{background:var(--ok-bg);color:var(--ok)}
.s-OFF{background:var(--warn-bg);color:var(--warn)}
.s-DEGRADED{background:var(--bad-bg);color:var(--bad)}
.s-UNKNOWN{background:var(--unk-bg);color:var(--unk)}
.s-BLOCKED{background:var(--ok-bg);color:var(--ok)}
.s-ALLOWED{background:var(--warn-bg);color:var(--warn)}
.s-PARTIAL,.s-REWRITTEN{background:var(--warn-bg);color:var(--warn)}
.row{display:flex;justify-content:space-between;gap:10px;padding:3px 0;font-size:14px}
.row span:last-child{color:var(--muted);text-align:right}
.note{font-size:13px;color:var(--muted);margin-top:8px;border-left:3px solid var(--line);
  padding-left:8px}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{text-align:left;padding:6px 8px;border-bottom:1px solid var(--line);vertical-align:top}
th{color:var(--muted);font-weight:600}
td.num{text-align:right;font-variant-numeric:tabular-nums}
code,.mono{font-family:ui-monospace,SFMono-Regular,Consolas,monospace;font-size:12px}
.ctl{border:1px solid var(--line);border-radius:8px;padding:12px;display:grid;gap:8px}
.ctl h3{margin:0;font-size:14px}
.ctl p{margin:0;font-size:12.5px;color:var(--muted)}
.ctl .live{display:flex;align-items:center;gap:10px}
input[type=range]{flex:1;accent-color:var(--accent);min-width:120px}
output{font-variant-numeric:tabular-nums;font-weight:700;min-width:64px;text-align:right}
button{font:inherit;padding:7px 12px;border-radius:7px;border:1px solid var(--line);
  background:var(--bg);color:var(--ink);cursor:pointer}
button.primary{background:var(--accent);border-color:var(--accent);color:#fff;font-weight:600}
button:disabled{opacity:.5;cursor:default}
.switch{display:flex;align-items:center;gap:10px}
input[type=checkbox]{width:18px;height:18px;accent-color:var(--accent)}
.gap{border-left:4px solid var(--warn);background:var(--warn-bg);border-radius:6px;
  padding:10px 12px;margin-bottom:10px}
.gap.high{border-left-color:var(--bad);background:var(--bad-bg)}
.gap h4{margin:0 0 4px;font-size:14px}
.gap p{margin:0;font-size:13px}
.gap .verify{margin-top:6px;font-size:12px}
#toast{position:fixed;left:50%;transform:translateX(-50%);bottom:18px;z-index:20;
  max-width:min(680px,92vw);display:grid;gap:8px}
.toast{padding:10px 14px;border-radius:8px;border:1px solid var(--line);background:var(--card);
  box-shadow:0 4px 16px rgba(0,0,0,.25);font-size:14px}
.toast.ok{border-left:4px solid var(--ok)}
.toast.bad{border-left:4px solid var(--bad)}
#modal{position:fixed;inset:0;background:rgba(0,0,0,.55);display:none;align-items:center;
  justify-content:center;z-index:30;padding:16px}
#modal.on{display:flex}
#modal .box{background:var(--card);border-radius:10px;padding:18px;max-width:420px;width:100%;
  display:grid;gap:10px}
input[type=password],input[type=text]{font:inherit;padding:9px 10px;border-radius:7px;
  border:1px solid var(--line);background:var(--bg);color:var(--ink);width:100%}
.center{max-width:420px;margin:12vh auto;padding:0 16px}
.err{color:var(--bad);font-size:13.5px;min-height:20px}
.stamp{color:var(--muted);font-size:12.5px}
"""

_HEAD = """<!doctype html>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="referrer" content="no-referrer">
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
    child is also on. So the password is set at the machine, elevated, by hand.
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
  if it could, whoever opened this page first would own the panel.</p>
</div>
""")


def render_login() -> str:
    return _doc("""
<div class="center">
  <h1>SafeHouse</h1>
  <p class="stamp">Parental controls for this PC. Sign in to see and change what is
  enforced.</p>
  <form id="f" autocomplete="off">
    <p><input type="password" id="pw" placeholder="Panel password" autofocus
              autocomplete="current-password"></p>
    <p class="err" id="err"></p>
    <p><button class="primary" type="submit">Sign in</button></p>
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
  <span class="stamp" id="host"></span>
  <span class="grow"></span>
  <span class="stamp" id="clock"></span>
  <button id="logout">Sign out</button>
</header>
<main>
  <section>
    <h2>Enforcement lanes <span class="hint">what is actually being enforced right now</span></h2>
    <div class="grid" id="lanes"></div>
  </section>

  <section>
    <h2>Settings <span class="hint">every change is written to disk and logged</span></h2>
    <p class="sub">Changes that make a protection <b>weaker</b> ask for the panel password
      again. Changes that tighten one do not.</p>
    <div class="grid" id="controls">

      <div class="ctl" data-ctl="roblox_limit_min">
        <h3>Roblox &mdash; minutes per day</h3>
        <div class="live">
          <input type="range" min="0" max="240" step="5" value="0">
          <output>0 min</output>
        </div>
        <p>0 means the game is closed within about a minute of launching. The standing
          policy on this PC is 0.</p>
        <div><button class="primary" data-apply>Apply</button></div>
      </div>

      <div class="ctl" data-ctl="roblox_bonus_min">
        <h3>Roblox &mdash; extra minutes today</h3>
        <div class="live">
          <input type="range" min="0" max="240" step="5" value="0">
          <output>0 min</output>
        </div>
        <p>A one-off grant for today only. It is written into the state file and expires
          at midnight, so it never becomes the new limit.</p>
        <div><button class="primary" data-apply>Grant</button></div>
      </div>

      <div class="ctl" data-ctl="browser_enabled">
        <h3>Web browsing &mdash; guard</h3>
        <div class="switch">
          <input type="checkbox"><span>Close any browser that opens</span>
        </div>
        <p>Turning this off has no expiry. For a timed pass, use the unlock below.</p>
        <div><button class="primary" data-apply>Apply</button></div>
      </div>

      <div class="ctl" data-ctl="browser_unlock_min">
        <h3>Web browsing &mdash; unlock for N minutes</h3>
        <div class="live">
          <input type="range" min="0" max="240" step="5" value="0">
          <output>0 min</output>
        </div>
        <p>Suspends the browser guard, then it resumes by itself. Use this before reading
          this panel <i>on the guarded PC</i> &mdash; otherwise the guard closes the
          browser you are reading it in, within about a minute.</p>
        <div><button class="primary" data-apply>Apply</button></div>
      </div>

      <div class="ctl" data-ctl="browser_limit_min">
        <h3>Web browsing &mdash; minutes per day</h3>
        <div class="live">
          <input type="range" min="0" max="240" step="5" value="0">
          <output>0 min</output>
        </div>
        <p><b>Stored, not enforced yet.</b> The browser lane has no metering today: it
          closes every browser it finds. This value is written to the config so the
          metering work has the parent's number to honour.</p>
        <div><button class="primary" data-apply>Apply</button></div>
      </div>

      <div class="ctl" data-ctl="browser_notify">
        <h3>Tell the child when a browser is closed</h3>
        <div class="switch">
          <input type="checkbox"><span>Show a message on screen</span>
        </div>
        <p>Currently <b>off</b>, at the parent's instruction: he sees the window close but
          is not told what closed it. Flip only on purpose.</p>
        <div><button class="primary" data-apply>Apply</button></div>
      </div>

      <div class="ctl" data-ctl="roblox_delay_sec">
        <h3>Roblox &mdash; pause before closing</h3>
        <div class="live">
          <input type="range" min="1" max="60" step="1" value="1" data-part="min">
          <output>1&ndash;5s</output>
          <input type="range" min="1" max="60" step="1" value="5" data-part="max">
        </div>
        <p>A random wait between seeing the game and ending it, so the moment cannot be
          timed &mdash; and so quitting during it is not logged as a kill.</p>
        <div><button class="primary" data-apply>Apply</button></div>
      </div>

      <div class="ctl" data-ctl="browser_delay_sec">
        <h3>Browser &mdash; pause before closing</h3>
        <div class="live">
          <input type="range" min="1" max="60" step="1" value="1" data-part="min">
          <output>1&ndash;5s</output>
          <input type="range" min="1" max="60" step="1" value="5" data-part="max">
        </div>
        <p>Same pause, for the browser lane. Capped at 60s: the task is terminated after
          5 minutes, and a pause the scheduler cuts off is a browser that survives.</p>
        <div><button class="primary" data-apply>Apply</button></div>
      </div>

      <div class="ctl" data-ctl="youtube_blocked">
        <h3>YouTube at the resolver</h3>
        <div class="switch">
          <input type="checkbox"><span>Blocked for every device using this resolver</span>
        </div>
        <p>Drives AdGuard's user rules. This is the only lane that also covers the Play
          Games VM, a phone or a TV. It takes YouTube Music with it.</p>
        <div><button class="primary" data-apply>Apply</button></div>
      </div>

    </div>
  </section>

  <section>
    <h2>DNS filtering</h2>
    <div id="dns"></div>
  </section>

  <section>
    <h2>Live verification <span class="hint">resolves real names and reads AdGuard's own verdict</span></h2>
    <p class="sub">The verdict is AdGuard's query-log <code>reason</code>, never the
      address that came back: a parental block answers with <code>94.140.14.35</code>,
      AdGuard's block page, so judging by IP reports a working block as a leak.</p>
    <div><button id="probe" class="primary">Run the probes</button>
      <span class="stamp" id="probe-when"></span></div>
    <div id="probes"></div>
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
  <div style="display:flex;gap:8px;justify-content:flex-end">
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
  if(res.status===403&&res.body.needs_password){
    const pw=await askPassword(res.body.error||'This change needs the panel password.');
    if(pw===null){toast('Cancelled - nothing was changed.',false);return false;}
    return apply(action,value,pw);
  }
  if(res.status===401&&res.body.needs_password){
    const pw=await askPassword(res.body.error||'Wrong password.');
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
  const key=card.dataset.ctl;
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
  const out=card.querySelector('output');
  if(box){ box.checked = key==='youtube_blocked' ? value==='BLOCKED' : !!value; return; }
  if(ranges.length===2){
    const v=value||[1,5];
    ranges[0].value=v[0]; ranges[1].value=v[1];
    out.textContent=v[0]+'-'+v[1]+'s'; return;
  }
  ranges[0].value = value==null?0:value;
  out.textContent = ranges[0].value+' min';
}

function refreshOutputs(card){
  const ranges=card.querySelectorAll('input[type=range]'), out=card.querySelector('output');
  if(!out) return;
  if(ranges.length===2) out.textContent=ranges[0].value+'-'+ranges[1].value+'s';
  else if(ranges.length===1) out.textContent=ranges[0].value+' min';
}

function laneCard(l){
  const facts=l.facts.map(f=>'<div class="row"><span>'+esc(f.label)+'</span><span>'+esc(f.value)+'</span></div>').join('');
  const notes=(l.notes||[]).map(n=>'<div class="note">'+esc(n)+'</div>').join('');
  const t=l.task, when=t.last_run?('last ran '+esc(t.last_run)):'never ran';
  return '<div class="card"><div class="row"><b>'+esc(l.label)+'</b>'+pill(l.status)+'</div>'
    +'<p class="sub">'+esc(l.how)+' &mdash; '+esc(l.reason)+'</p>'+facts
    +'<div class="row"><span>Task</span><span>'+esc(t.name)+' &middot; '+esc(t.state)+' &middot; '
    +when+' &middot; '+esc(t.last_result_text)+'</span></div>'
    +(l.log&&l.log.last_event?'<div class="row"><span>Last close</span><span>'+esc(l.log.last_event.at)+'</span></div>':'')
    +notes+'</div>';
}

function dnsBlock(d){
  const rows=(d.lists||[]).map(f=>'<tr><td>'+esc(f.name)+(f.kind==='allow'?' <i>(allow)</i>':'')
    +'<div class="mono">'+esc(f.url)+'</div></td><td>'+(f.enabled?'on':'off')
    +'</td><td class="num">'+esc(f.rules_count==null?'?':f.rules_count)+'</td><td>'+esc(f.last_updated||'never')+'</td></tr>').join('');
  const ss=d.safesearch||{}, engines=Object.keys(ss.engines||{}).map(k=>k+': '+(ss.engines[k]?'on':'off')).join(', ');
  return '<div class="row"><b>'+esc(d.base_url)+'</b>'+pill(d.status)+'</div>'
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
    +'<table><tr><th>Filter list</th><th>State</th><th class="num">Rules</th><th>Updated</th></tr>'+rows+'</table>';
}

function probesBlock(p){
  if(!p) return '<p class="sub">Not run yet in this session.</p>';
  $('#probe-when').textContent='last run '+p.ran_at+' against '+p.resolver;
  return '<table><tr><th>Probe</th><th>Domain</th><th>Expected</th><th>Verdict</th><th>Why / detail</th></tr>'
    +p.results.map(r=>'<tr><td>'+esc(r.label)+'</td><td class="mono">'+esc(r.domain||'&mdash;')
    +'</td><td>'+esc(r.expect)+'</td><td>'+pill(r.verdict)+(r.ok?'':' <b>&#9888;</b>')
    +'</td><td>'+esc(r.detail)+(r.rule?'<div class="mono">'+esc(r.rule)+'</div>':'')
    +'<div class="stamp">'+esc(r.why)+'</div></td></tr>').join('')+'</table>';
}

function tasksBlock(rows){
  return '<table><tr><th>Task</th><th>State</th><th>Last run</th><th>Last result</th><th>Next run</th></tr>'
    +rows.map(t=>'<tr><td class="mono">'+esc(t.name)+'</td><td>'
    +(t.registered===null?pill('UNKNOWN'):t.registered?esc(t.state):'<i>not registered</i>')
    +'</td><td>'+esc(t.last_run||'never')+'</td><td>'+esc(t.last_result_text)
    +'</td><td>'+esc(t.next_run||'&mdash;')+'</td></tr>').join('')+'</table>';
}

function policyBlock(p){
  if(!p.keys.length) return '<p class="sub">'+esc(p.reason)+'</p>';
  return '<div class="row"><span>'+esc(p.reason)+'</span>'+pill(p.status)+'</div>'
    +p.keys.map(k=>'<div class="card" style="margin-top:10px"><div class="row"><span class="mono">'
    +esc(k.key)+'</span>'+pill(k.status)+'</div><table>'
    +k.rows.map(r=>'<tr><td class="mono">'+esc(r.name)+'</td><td>'+esc(r.value==null?'not set':r.value)
    +'</td><td>'+(r.ok?'ok':'want '+esc(r.expected))+'</td></tr>').join('')
    +'</table></div>').join('');
}

function gapsBlock(gaps){
  return gaps.map(g=>'<div class="gap '+(g.severity==='high'?'high':'')+'"><h4>'+esc(g.title)
    +' <span class="pill s-'+(g.severity==='high'?'DEGRADED':'OFF')+'">'+esc(g.severity)+'</span></h4>'
    +'<p>'+esc(g.detail)+'</p><p class="verify mono">verify: '+esc(g.verify)+'</p></div>').join('');
}

function render(s){
  SNAP=s;
  $('#host').textContent=s.host.machine+' \\u00b7 '+s.host.root+' \\u00b7 listening on '
    +(s.host.bound.join(', ')||'?')+':'+s.host.port;
  $('#clock').textContent='updated '+s.now.stamp;
  $('#lanes').innerHTML=s.lanes.map(laneCard).join('');
  $('#dns').innerHTML=dnsBlock(s.dns);
  $('#probes').innerHTML=probesBlock(s.probes);
  $('#tasks').innerHTML=tasksBlock(s.tasks);
  $('#policy').innerHTML=policyBlock(s.policy);
  $('#gaps').innerHTML=gapsBlock(s.gaps);
  $('#foot').textContent='Ranges are enforced on the server as well as here. '
    +'Every change is written to '+s.host.root+'\\\\panel.log with the time and the address it came from.';
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
$('#logout').addEventListener('click',async ()=>{await post('/api/logout',{});location.reload();});

refresh();
setInterval(refresh,10000);
</script>
"""


def render_panel() -> str:
    return _doc(_BODY)
