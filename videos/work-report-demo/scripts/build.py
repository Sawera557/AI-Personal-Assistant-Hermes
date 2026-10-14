"""Generate index.html + compositions/frames/*.html for the work-report demo.
Timing source of truth: assets/vo/words.json (real voice). Re-run after any VO change."""
import json, os, re, html

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
W = json.load(open(os.path.join(ROOT, "assets/vo/words.json")))
FRAMES = os.path.join(ROOT, "compositions", "frames"); os.makedirs(FRAMES, exist_ok=True)
IMG = "assets/footage/"   # resolved against index.html
XFADE = 0.6

# ---------------------------------------------------------------- timeline
# (sid, file, label, [(vo, local_start)], dur)
def vd(k): return W[k]["dur"]
SCENES = [
    ("s01", "01-hook",     [("line01", 1.2)], 1.2 + vd("line01") + 0.7),
    ("s02", "02-pattern",  [("line02", 0.5)], 0.5 + vd("line02") + 0.6),
    ("s03", "03-hub",      [("line03", 0.5)], 0.5 + vd("line03") + 0.6),
    ("s04", "04-gif",      [("line04", 0.5)], 0.5 + vd("line04") + 0.6),
    ("s05", "05-gak",      [("line05", 0.5)], 0.5 + vd("line05") + 0.6),
    ("s06", "06-nour-hammam", [("line06", 0.5)], 0.5 + vd("line06") + 0.6),
    ("s07", "07-afrofarms", [("line07", 0.5)], 0.5 + vd("line07") + 0.6),
    ("s08", "08-billing",  [("line08", 0.5)], 0.5 + vd("line08") + 0.6),
    ("s09", "09-savings",  [("line09a", 0.5), ("line09b", 0.5 + vd("line09a") + 1.8)], 0.5 + vd("line09a") + 1.8 + vd("line09b") + 0.9),
    ("s10", "10-infrastructure", [("line10", 0.5)], 0.5 + vd("line10") + 0.6),
    ("s11", "11-close",    [("line11", 0.5)], 0.5 + vd("line11") + 4.2),
]
START = {}; t = 0.0
for sid, f, vos, d in SCENES: START[sid] = round(t, 3); t += d
TOTAL = round(t, 3)
DUR = {sid: round(d, 3) for sid, f, vos, d in SCENES}

# ---------------------------------------------------------------- shared css
CSS = """
#root{position:absolute;inset:0;overflow:hidden;color:#FFFFF4;font-family:'Montserrat',sans-serif;}
.abs{position:absolute;}
.kicker{font-family:'IBM Plex Mono',monospace;font-weight:700;font-size:22px;letter-spacing:.22em;color:#E2B04A;text-transform:uppercase;}
.serif{font-family:'EB Garamond',serif;}
.mono{font-family:'IBM Plex Mono',monospace;}
.h1{font-family:'EB Garamond',serif;font-weight:400;font-size:88px;line-height:1.02;letter-spacing:-.01em;color:#FFFFF4;}
.h2{font-family:'EB Garamond',serif;font-weight:400;font-size:60px;line-height:1.05;color:#FFFFF4;}
.gold{color:#E2B04A;} .mint{color:#93DBB3;} .brick{color:#E06A4E;}
.scenelabel{position:absolute;left:60px;top:34px;font-family:'IBM Plex Mono',monospace;font-weight:700;font-size:20px;letter-spacing:.18em;color:#93DBB3;}
.card{position:absolute;background:#173b29;border:2px solid rgba(255,255,244,.16);border-radius:14px;padding:30px 34px;box-shadow:0 24px 60px rgba(0,0,0,.35);}
.card .ct{font-family:'IBM Plex Mono',monospace;font-weight:700;font-size:20px;letter-spacing:.2em;color:#E2B04A;margin-bottom:18px;}
.card .ch{font-family:'EB Garamond',serif;font-size:40px;line-height:1.1;margin-bottom:14px;}
.li{display:flex;align-items:center;gap:16px;font-size:28px;font-weight:700;line-height:1.25;margin:14px 0;color:#FFFFF4;}
.li .tk{flex:0 0 34px;height:34px;border-radius:50%;border:2px solid #93DBB3;color:#93DBB3;font-size:20px;display:flex;align-items:center;justify-content:center;font-family:'IBM Plex Mono',monospace;}
.chip{display:inline-flex;align-items:center;gap:10px;padding:10px 18px;border-radius:999px;background:#173b29;border:2px solid rgba(255,255,244,.22);font-family:'IBM Plex Mono',monospace;font-weight:700;font-size:20px;letter-spacing:.06em;color:#FFFFF4;}
.chip.on{border-color:#E2B04A;color:#E2B04A;}
.stamp{position:absolute;font-family:'IBM Plex Mono',monospace;font-weight:700;letter-spacing:.14em;border:5px solid #E06A4E;color:#E06A4E;padding:14px 26px;border-radius:10px;background:rgba(20,50,33,.9);}
/* browser frame */
.bf{position:absolute;left:210px;top:96px;width:1500px;height:773px;border-radius:18px;overflow:hidden;background:#0f2619;border:2px solid rgba(255,255,244,.18);box-shadow:0 30px 80px rgba(0,0,0,.45);}
.bf-bar{position:absolute;left:0;top:0;width:1500px;height:34px;background:#0f2619;display:flex;align-items:center;gap:8px;padding:0 16px;}
.bf-bar i{width:11px;height:11px;border-radius:50%;background:rgba(255,255,244,.25);display:block;}
.bf-url{margin-left:14px;font-family:'IBM Plex Mono',monospace;font-size:16px;color:rgba(255,255,244,.7);}
.bf-priv{margin-left:auto;font-family:'IBM Plex Mono',monospace;font-size:14px;letter-spacing:.12em;color:#93DBB3;}
.bf-view{position:absolute;left:0;top:34px;width:1500px;height:739px;overflow:hidden;background:#fffff4;}
.zo{position:absolute;left:0;top:0;width:1500px;height:739px;transform-origin:50% 50%;}
.zi{position:absolute;left:0;top:0;width:1500px;height:739px;}
.shot{position:absolute;left:0;top:0;width:1500px;height:739px;display:block;opacity:0;}
.scrim{position:absolute;left:0;top:34px;width:1500px;height:739px;background:#143221;opacity:0;}
.scrimR{position:absolute;left:0;top:34px;width:1500px;height:739px;opacity:0;background:linear-gradient(to left,rgba(20,50,33,.94) 0%,rgba(20,50,33,.86) 40%,rgba(20,50,33,0) 66%);}
"""

def tmpl(sid, body, js):
    return f"""<!doctype html>
<html lang="en"><head><meta charset="UTF-8" /></head>
<body>
<template>
<style>{CSS.replace('#root', '#' + sid + '-root')}</style>
<div id="{sid}-root" data-composition-id="{sid}" data-width="1920" data-height="1080" data-duration="{DUR[sid] + XFADE}">
{body}
</div>
<script>
(function(){{
const SID = "{sid}";
const tl = gsap.timeline({{ paused: true }});
const $ = (s) => "#" + SID + "-" + s;
const VW = 1500, VH = 739, SC = VW / 1920;
function cam(t, cx, cy, S, dur, ease) {{
  const fx = cx * SC, fy = cy * SC;
  let ox = fx - VW / 2, oy = fy - VH / 2;
  const mx = VW / 2 - VW / (2 * S), my = VH / 2 - VH / (2 * S);
  ox = Math.max(-mx, Math.min(mx, ox)); oy = Math.max(-my, Math.min(my, oy));
  tl.to($("zo"), {{ scale: S, duration: dur, ease: ease || "sine.inOut" }}, t);
  tl.to($("zi"), {{ x: -ox, y: -oy, duration: dur, ease: ease || "sine.inOut" }}, t);
}}
function show(t, id, d) {{ tl.fromTo($(id), {{ opacity: 0 }}, {{ opacity: 1, duration: d || 0.5, ease: "power1.inOut" }}, t); }}
function inUp(t, id, d, dy) {{ tl.fromTo($(id), {{ opacity: 0, y: dy == null ? 26 : dy }}, {{ opacity: 1, y: 0, duration: d || 0.5, ease: "power3.out" }}, t); }}
function inLeft(t, id, d) {{ tl.fromTo($(id), {{ opacity: 0, x: -30 }}, {{ opacity: 1, x: 0, duration: d || 0.45, ease: "power3.out" }}, t); }}
function pop(t, id, d) {{ tl.fromTo($(id), {{ opacity: 0, scale: 0.6 }}, {{ opacity: 1, scale: 1, duration: d || 0.45, ease: "back.out(2)" }}, t); }}
function slam(t, id, rot) {{ tl.fromTo($(id), {{ opacity: 0, scale: 1.8, rotation: rot || -6 }}, {{ opacity: 1, scale: 1, rotation: rot || -6, duration: 0.35, ease: "power4.in" }}, t); }}
function out(t, id, d) {{ tl.to($(id), {{ opacity: 0, duration: d || 0.35, ease: "power1.in" }}, t); }}
function scrim(t, id, a, d) {{ tl.to($(id), {{ opacity: a, duration: d || 0.5, ease: "power1.inOut" }}, t); }}
function count(t, id, from, to, d, fmt) {{ const o = {{ v: from }}; const el = document.getElementById(SID + "-" + id);
  tl.to(o, {{ v: to, duration: d, ease: "power2.out", onUpdate: () => {{ el.textContent = fmt(o.v); }} }}, t); }}
{js}
window.__timelines["{sid}"] = tl;
}})();
</script>
</template>
</body></html>
"""

def bframe(sid, label, shots):
    seen = {}; parts = []
    for k, src in shots:
        seen[src] = seen.get(src, 0) + 1
        q = "" if seen[src] == 1 else "_b"   # physical copy -> no duplicate media discovery
        parts.append(f'<img class="shot" id="{sid}-{k}" src="{IMG}{src}{q}.png" alt="" />')
    imgs = "\n".join(parts)
    return f"""<div class="bf" id="{sid}-bf">
  <div class="bf-bar"><i></i><i></i><i></i><span class="bf-url" data-layout-allow-overlap>{label}</span><span class="bf-priv">CLIENT DATA BLURRED FOR PRIVACY</span></div>
  <div class="bf-view"><div class="zo" id="{sid}-zo" data-layout-allow-overflow><div class="zi" id="{sid}-zi">
{imgs}
  </div></div></div>
  <div class="scrimR" id="{sid}-scrimR"></div>
  <div class="scrim" id="{sid}-scrim"></div>
</div>"""

def li(sid, k, text, mark="✓"):  # noqa
    return f'<div class="li" id="{sid}-{k}"><span class="tk">{mark}</span><span>{text}</span></div>'

def wt(line, word, nth=1, lead=0.5):
    """local scene time of the nth occurrence of word (prefix match, case-insensitive)."""
    c = 0
    for w in W[line]["words"]:
        if re.sub(r"[^a-z0-9'-]", "", w["text"].lower()).startswith(word.lower()):
            c += 1
            if c == nth: return round(w["start"] + lead, 2)
    raise KeyError(f"{line}:{word}#{nth}")

FRAMEIN = 'tl.fromTo($("bf"), {opacity:0, y:30}, {opacity:1, y:0, duration:0.6, ease:"power3.out"}, 0.1);'

# ================================================================ S01 HOOK
def s01():
    sid = "s01"; L = lambda w, n=1: wt("line01", w, n, 1.2)
    cells = []
    for r in range(6):
        for c in range(6):
            cells.append(f'<div class="cell" id="{sid}-c{r}{c}" style="left:{c*130}px;top:{r*70}px"></div>')
    body = f"""
<style>
#s01-grid{{left:1060px;top:250px;width:780px;height:420px;}}
#s01-grid .cell{{position:absolute;width:130px;height:70px;border:1px solid rgba(255,255,244,.22);}}
.hw{{position:absolute;font-family:'IBM Plex Mono',monospace;font-weight:700;font-size:24px;color:#FFFFF4;background:#1d4632;padding:6px 12px;border-radius:6px;}}
.stat{{position:absolute;top:0;width:410px;}}
.stat .n{{font-family:'EB Garamond',serif;font-size:110px;line-height:1;color:#FFFFF4;}}
.stat .l{{font-family:'IBM Plex Mono',monospace;font-weight:700;font-size:22px;letter-spacing:.14em;color:#93DBB3;margin-top:10px;text-transform:uppercase;}}
#s01-stats{{left:140px;top:740px;width:1640px;height:170px;border-top:2px solid rgba(255,255,244,.25);padding-top:24px;}}
.vr{{position:absolute;top:24px;width:2px;height:140px;background:rgba(255,255,244,.18);}}
</style>
<div class="abs kicker" id="{sid}-kick" style="left:140px;top:150px">Automation &amp; dashboard work · Sawera Khadium</div>
<div class="abs h1" id="{sid}-before" style="left:140px;top:250px;width:860px">Kept alive by hand, in spreadsheets.</div>
<div class="abs h1" id="{sid}-head" style="left:140px;top:250px;width:860px">Spreadsheets that <span class="gold">run themselves.</span></div>
<div class="abs" id="{sid}-verbs" style="left:140px;top:500px;width:900px;display:flex;gap:14px;flex-wrap:wrap">
  <span class="chip" id="{sid}-v1">↻ update themselves</span><span class="chip" id="{sid}-v2">✓ check their own work</span><span class="chip" id="{sid}-v3">▦ report on live dashboards</span>
</div>
<div class="abs" id="{sid}-grid">{''.join(cells)}
  <div class="hw" id="{sid}-w1" style="left:14px;top:18px">rent</div>
  <div class="hw" id="{sid}-w2" style="left:274px;top:88px">farm operations</div>
  <div class="hw" id="{sid}-w3" style="left:144px;top:228px">a spa</div>
  <div class="hw" id="{sid}-w4" style="left:404px;top:298px">dozens of bills</div>
  <div class="abs" id="{sid}-sweep" style="left:0;top:0;width:780px;height:420px;background:#93DBB3;opacity:0"></div>
</div>
<div class="abs" id="{sid}-stats" style="opacity:0">
  <div class="stat" id="{sid}-st1" style="left:0"><div class="n">5</div><div class="l">businesses</div></div>
  <div class="vr" style="left:390px"></div>
  <div class="stat" id="{sid}-st2" style="left:420px"><div class="n">8</div><div class="l">scheduled tasks</div></div>
  <div class="vr" style="left:810px"></div>
  <div class="stat" id="{sid}-st3" style="left:840px"><div class="n">40+</div><div class="l">apps connected</div></div>
  <div class="vr" style="left:1230px"></div>
  <div class="stat" id="{sid}-st4" style="left:1260px"><div class="n gold"><span id="{sid}-cnt">$0K</span></div><div class="l">saved per year</div></div>
</div>"""
    js = f"""
inUp(0.3, "kick", 0.6, 14);
tl.fromTo($("grid"), {{opacity:0}}, {{opacity:1, duration:1.0, ease:"power1.out"}}, 0.6);
tl.set($("head"), {{opacity:0}}, 0); tl.set($("verbs"), {{opacity:1}}, 0);
["v1","v2","v3"].forEach(v => tl.set($(v), {{opacity:0}}, 0));
pop({L('rent')}, "w1"); pop({L('farm')}, "w2"); pop({L('spa')}, "w3"); pop({L('dozens')}, "w4");
inUp({L('by')}, "before", 0.6);
// the turn: grid sweeps mint, headline swaps
tl.to($("sweep"), {{opacity:0.35, duration:0.3, ease:"power2.out"}}, {L('turned')});
tl.to($("sweep"), {{opacity:0, duration:0.8, ease:"power2.in"}}, {L('turned')} + 0.3);
["w1","w2","w3","w4"].forEach((w,i) => tl.to($(w), {{backgroundColor:"#E2B04A", color:"#143221", duration:0.3}}, {L('turned')} + 0.1*i));
tl.to($("before"), {{opacity:0, y:-20, duration:0.35, ease:"power2.in"}}, {L('turned')});
inUp({L('turned')} + 0.3, "head", 0.6);
pop({L('update')}, "v1"); pop({L('check')}, "v2"); pop({L('report')}, "v3");
show({L('along')} - 0.2, "stats", 0.3); inUp({L('along')}, "st1", 0.5); inUp({L('along')} + 0.25, "st2", 0.5); inUp({L('along')} + 0.5, "st3", 0.5); inUp({L('cut')}, "st4", 0.5);
count({L('thirty-three')} - 0.2, "cnt", 0, 33, 1.4, v => "$" + Math.round(v) + "K");
tl.fromTo($("grid"), {{y:0}}, {{y:-14, duration:{DUR[sid]}, ease:"none"}}, 0);
"""
    return body, js

# ================================================================ S02 PATTERN
STATIONS = [("SOURCE", "email · QuickBooks · bills"), ("VERIFY", "a scheduled task checks it"),
            ("SHEET", "the source of truth"), ("DASHBOARD", "live, never a copy"), ("HUMAN", "only when it's needed")]
def s02():
    sid = "s02"; L = lambda w, n=1: wt("line02", w, n)
    cues = [L("Source"), L("automation"), L("Google"), L("live"), L("person")]
    boxes = []
    for i, (n, d) in enumerate(STATIONS):
        x = 140 + i * 340
        boxes.append(f'<div class="abs st" id="{sid}-b{i}" style="left:{x}px;top:430px"><div class="num">0{i+1}</div><div class="nm" id="{sid}-n{i}">{n}</div><div class="ds">{d}</div></div>')
        if i < 4: boxes.append(f'<div class="abs ln" id="{sid}-l{i}" style="left:{x+280}px;top:524px"></div>')
    body = f"""
<style>
.st{{width:280px;height:200px;border:2px solid rgba(255,255,244,.2);border-radius:14px;background:rgba(23,59,41,.6);padding:24px;}}
.st .num{{font-family:'IBM Plex Mono',monospace;font-size:20px;color:rgba(255,255,244,.55);}}
.st .nm{{font-family:'IBM Plex Mono',monospace;font-weight:700;font-size:30px;letter-spacing:.08em;margin:18px 0 12px;color:#FFFFF4;}}
.st .ds{{font-size:22px;font-weight:400;line-height:1.3;color:rgba(255,255,244,.8);}}
.ln{{width:60px;height:4px;background:#E2B04A;transform-origin:0 50%;}}
</style>
<div class="abs kicker" id="{sid}-kick" style="left:140px;top:170px">The pattern</div>
<div class="abs h2" id="{sid}-head" style="left:140px;top:220px;width:1500px">Every system follows the same pattern.</div>
{''.join(boxes)}
<div class="abs mono" id="{sid}-foot" style="left:140px;top:720px;font-size:24px;color:#93DBB3;letter-spacing:.06em">Source data → automation → sheet → live dashboard → a person, only for real decisions.</div>"""
    js = "inUp(0.2,'kick',0.5,14); inUp(0.4,'head',0.6);\n"
    for i in range(5):
        js += f"inUp({cues[i] - 0.15}, 'b{i}', 0.45);\n"
        js += f"tl.to($('b{i}'), {{borderColor:'#E2B04A', backgroundColor:'#1d4632', duration:0.3}}, {cues[i]});\n"
        js += f"tl.to($('n{i}'), {{color:'#E2B04A', duration:0.3}}, {cues[i]});\n"
        if i > 0: js += f"tl.fromTo($('l{i-1}'), {{scaleX:0}}, {{scaleX:1, duration:0.35, ease:'power2.out'}}, {cues[i] - 0.3});\n"
    js += f"inUp({L('decision') + 0.4}, 'foot', 0.5, 14);\n"
    return body, js

# ================================================================ S03 HUB
def s03():
    sid = "s03"; L = lambda w, n=1: wt("line03", w, n)
    body = f"""
<div class="scenelabel" id="{sid}-lab" data-layout-allow-overlap>ONE DOOR</div>
{bframe(sid, "Business Artifacts Navigation Hub", [("i1", "hub")])}
<div class="abs" id="{sid}-chips" style="left:250px;top:780px;display:flex;gap:14px">
  <span class="chip on" id="{sid}-c1">8 scheduled tasks</span><span class="chip on" id="{sid}-c2">5 businesses</span><span class="chip" id="{sid}-c3">1 hub</span></div>"""
    js = f"""{FRAMEIN} inLeft(0.2,"lab"); show(0, "i1", 0.01);
cam(0, 960, 473, 1.0, 0.01); cam(0.2, 960, 420, 1.06, 5.0);
pop({L('Eight')}, "c1"); pop({L('five')}, "c2"); pop({L('one', 1)}, "c3");
cam({L('Every') - 0.3}, 788, 138, 1.75, 1.2, "power3.inOut");
"""
    return body, js

# ================================================================ S04 GIF
def s04():
    sid = "s04"; L = lambda w, n=1: wt("line04", w, n)
    tU, tV, tS = L("Updater") - 0.3, L("Verifier") - 0.3, L("One") - 0.2
    body = f"""
<div class="scenelabel" id="{sid}-lab" data-layout-allow-overlap>01 · GIF PROPERTIES</div>
{bframe(sid, "GIF Properties — rent, arrears &amp; utility tracker", [("i1","gif_now"),("i2","gif_arrears"),("i3","gif_issues"),("i4","gif_sheet")])}
<div class="abs" id="{sid}-tag" style="left:250px;top:780px;display:flex;gap:14px"><span class="chip on" id="{sid}-t1">2 weekly automations</span><span class="chip" id="{sid}-t2">rent · arrears · utilities</span></div>
<div class="card" data-layout-allow-overlap id="{sid}-up" style="left:1130px;top:200px;width:540px">
  <div class="ct">UPDATER · WEEKLY</div><div class="ch">Keeps the data current</div>
  {li(sid,'u1','Records rent payments')}{li(sid,'u2','Rolls arrears forward')}{li(sid,'u3','Refreshes utility readings')}{li(sid,'u4','Updates tenant &amp; unit status')}
</div>
<div class="card" data-layout-allow-overlap id="{sid}-ve" style="left:1130px;top:200px;width:540px">
  <div class="ct">VERIFIER · WEEKLY</div><div class="ch">Audits before anything changes</div>
  {li(sid,'v1','Missing or duplicate rent','!')}{li(sid,'v2','Balances that don’t reconcile','!')}{li(sid,'v3','Stale utility readings','!')}{li(sid,'v4','Missing lease data','!')}
  <div class="chip on" id="{sid}-ro" style="margin-top:16px">READ-ONLY · flags, never edits</div>
</div>
<div class="card" id="{sid}-k1" style="left:300px;top:330px;width:620px;text-align:left"><div class="ct">UPDATER</div><div class="h2">keeps it <span class="mint">current.</span></div></div>
<div class="card" id="{sid}-k2" style="left:1000px;top:330px;width:620px"><div class="ct">VERIFIER</div><div class="h2">keeps it <span class="gold">trustworthy.</span></div></div>"""
    js = f"""{FRAMEIN} inLeft(0.2,"lab");
show(0,"i1",0.01); cam(0,960,473,1,0.01); cam(0.3,700,420,1.08,6.5);
pop({L('Two')},"t1"); pop({L('tracks')},"t2");
// updater on the arrears view
show({tU},"i2",0.5); cam({tU},960,473,1.0,0.5,"power2.inOut"); cam({tU}+0.6,600,380,1.12,6.0);
tl.set($("up"),{{opacity:0}},0); tl.set($("ve"),{{opacity:0}},0); tl.set($("k1"),{{opacity:0}},0); tl.set($("k2"),{{opacity:0}},0);
out({tU},"tag",0.3);
scrim({tU},"scrimR",1); inUp({tU}+0.1,"up",0.5);
["u1","u2","u3","u4"].forEach(k=>tl.set($(k),{{opacity:0}},0));
inLeft({L('records')},"u1"); inLeft({L('rolls')},"u2"); inLeft({L('refreshes')},"u3"); inLeft({L('tenant')},"u4");
// verifier on the issues view
out({tV}-0.2,"up",0.3); show({tV},"i3",0.5); cam({tV},960,473,1.0,0.5,"power2.inOut"); cam({tV}+0.6,700,560,1.1,7.0);
inUp({tV}+0.1,"ve",0.5);
["v1","v2","v3","v4","ro"].forEach(k=>tl.set($(k),{{opacity:0}},0));
inLeft({L('missing',1)},"v1"); inLeft({L('balances')},"v2"); inLeft({L('stale')},"v3"); inLeft({L('missing',2)},"v4"); pop({L('flags')},"ro");
// the split: current vs trustworthy
out({tS}-0.2,"ve",0.3); show({tS},"i4",0.5); cam({tS},960,473,1.0,0.5); scrim({tS},"scrimR",0,0.4); scrim({tS},"scrim",0.72,0.5);
inUp({L('current')} - 0.3,"k1",0.5); inUp({L('other')},"k2",0.5);
"""
    return body, js

# ================================================================ S05 GAK
def s05():
    sid = "s05"; L = lambda w, n=1: wt("line05", w, n)
    tSrc, tF, tR, tG = L("Every", 1), L("emails") - 0.2, L("A", 1) if False else L("reply") - 0.2, L("Every", 2) - 0.2
    body = f"""
<div class="scenelabel" id="{sid}-lab" data-layout-allow-overlap>02 · GAK PROPERTIES</div>
{bframe(sid, "GAK Properties — rent, arrears &amp; utility tracker", [("i1","gak_over"),("i2","gak_sheet"),("i3","gak_over")])}
<div class="abs" id="{sid}-tag" style="left:250px;top:780px;display:flex;gap:14px"><span class="chip on" id="{sid}-t1">every Friday</span><span class="chip" id="{sid}-t2">reply-driven updates</span></div>
<div class="card" data-layout-allow-overlap id="{sid}-src" style="left:1130px;top:170px;width:540px">
  <div class="ct">WHAT THE VERIFIER READS</div>
  {li(sid,'r1','All 15 workbook tabs','15')}{li(sid,'r2','Tenant notices','→')}{li(sid,'r3','Utility bills','→')}{li(sid,'r4','QuickBooks statements','→')}{li(sid,'r5','Bank alerts','→')}{li(sid,'r6','From a dedicated inbox','@')}
</div>
<div class="card" data-layout-allow-overlap id="{sid}-mail" style="left:380px;top:150px;width:1160px">
  <div class="mono" style="font-size:20px;color:rgba(255,255,244,.65);letter-spacing:.06em">FROM&nbsp; GAK Verifier &nbsp;·&nbsp; SUBJECT&nbsp; This week’s findings — numbered, reply to each</div>
  <div style="height:2px;background:rgba(255,255,244,.15);margin:18px 0 8px"></div>
  <div class="li" id="{sid}-f1"><span class="tk">1</span><span class="gold mono" style="width:190px">PROPOSAL</span><span>Found, looks correct — can be added</span></div>
  <div class="li" id="{sid}-f2"><span class="tk">2</span><span class="brick mono" style="width:190px">CONFLICT</span><span>The sheet and the source disagree</span></div>
  <div class="li" id="{sid}-f3"><span class="tk">3</span><span class="mint mono" style="width:190px">NOTICE</span><span>Important — needs your attention</span></div>
  <div class="mono" id="{sid}-rl" style="font-size:20px;letter-spacing:.2em;color:#E2B04A;margin:26px 0 12px">YOUR REPLY DRIVES THE UPDATER</div>
  <div style="display:flex;gap:16px">
    <span class="chip on" id="{sid}-y1">YES → adds the figure</span><span class="chip on" id="{sid}-y2">CORRECT → writes the fixed value</span><span class="chip on" id="{sid}-y3">OK → acknowledges</span></div>
</div>
<div class="abs" id="{sid}-g" style="left:300px;top:300px;width:1320px;display:flex;flex-wrap:wrap;gap:22px;justify-content:center">
  <span class="chip" id="{sid}-g1" style="font-size:28px;padding:18px 28px">✓ deduplicated</span><span class="chip" id="{sid}-g2" style="font-size:28px;padding:18px 28px">✓ cross-checked</span>
  <span class="chip" id="{sid}-g3" style="font-size:28px;padding:18px 28px">✓ logged with evidence · verification_log</span>
  <span class="chip on" id="{sid}-g4" style="font-size:34px;padding:22px 34px">🔒 locked to GAK — never touches GIF</span></div>"""
    js = f"""{FRAMEIN} inLeft(0.2,"lab");
show(0,"i1",0.01); cam(0,960,473,1,0.01); cam(0.5,610,330,1.35,2.6,"power2.inOut");
pop({L('Properties')},"t1"); pop({L('further')},"t2");
["src","mail","g","r1","r2","r3","r4","r5","r6","f1","f2","f3","rl","y1","y2","y3","g1","g2","g3","g4"].forEach(k=>tl.set($(k),{{opacity:0}},0));
// sources
out({tSrc},"tag",0.3); show({tSrc},"i2",0.5); cam({tSrc},960,473,1,0.5,"power2.inOut");
cam({L('fifteen')}-0.4,960,927,1.8,1.0,"power3.inOut"); cam({L('tenant')},420,560,1.15,1.4,"power2.inOut");
scrim({L('fifteen')},"scrimR",1); inUp({L('fifteen')},"src",0.5);
inLeft({L('fifteen')}+0.1,"r1"); inLeft({L('tenant')},"r2"); inLeft({L('utility')},"r3"); inLeft({L('QuickBooks')},"r4"); inLeft({L('bank')},"r5"); inLeft({L('dedicated')},"r6");
// findings email
out({tF}-0.1,"src",0.3); show({tF},"i3",0.5); cam({tF},960,473,1,0.6); scrim({tF},"scrimR",0,0.4); scrim({tF},"scrim",0.78,0.5);
inUp({tF}+0.1,"mail",0.55);
inLeft({L('proposals')},"f1"); inLeft({L('conflicts')},"f2"); inLeft({L('notices')},"f3");
inUp({tR},"rl",0.4,10); pop({L('Yes')},"y1"); pop({L('Correct')},"y2"); pop({L('OK')},"y3");
// guarantees
out({tG},"mail",0.3); show({tG},"g",0.3);
pop({L('deduplicated')},"g1"); pop({L('cross-checked')},"g2"); pop({L('logged')},"g3"); pop({L('locked')},"g4",0.55);
"""
    return body, js

# ================================================================ S06 NOUR HAMMAM
def s06():
    sid = "s06"; L = lambda w, n=1: wt("line06", w, n)
    tTabs, tRule, tGuess, tRef, tTri = L("It", 1) - 0.2, L("only") - 0.2, L("When") - 0.2, L("owner's") - 0.4, L("Refresh", 2) - 0.2
    tabs = [("05 REGISTER", "register"), ("06 PEOPLE", "people"), ("02 DECISIONS", "decisions"), ("03 GATES", "gates"), ("08 MONEY", "money"), ("12 CHANGE LOG", "change")]
    tabchips = "".join(f'<span class="chip" id="{sid}-tb{i}">{n}</span>' for i, (n, w) in enumerate(tabs))
    body = f"""
<div class="scenelabel" id="{sid}-lab" data-layout-allow-overlap>03 · NOUR HAMMAM</div>
{bframe(sid, "Nour Hammam — owner command dashboard &amp; operations tracker", [("i1","nh_queue"),("i2","nhs_rules"),("i3","nh_queue"),("i4","nh_reg")])}
<div class="abs" id="{sid}-tag" style="left:250px;top:780px;display:flex;gap:14px"><span class="chip on" id="{sid}-t1">daily registrar</span><span class="chip" id="{sid}-t2">operations workbook</span></div>
<div class="abs" id="{sid}-tabs" style="left:250px;top:690px;display:flex;gap:12px">{tabchips}</div>
<div class="card" data-layout-allow-overlap id="{sid}-log" style="left:1130px;top:180px;width:540px">
  <div class="ct">12 CHANGE LOG · EVERY CHANGED CELL</div><div class="ch">Writes only what a source supports</div>
  {li(sid,'l1','Old value','was')}{li(sid,'l2','New value','now')}{li(sid,'l3','Evidence','src')}
</div>
<div class="card" data-layout-allow-overlap id="{sid}-gs" style="left:1130px;top:200px;width:540px">
  <div class="ct">MISSING · UNSUPPORTED · CONTRADICTORY</div><div class="ch">No guess. No write.</div>
  <div class="mono" style="font-size:22px;color:rgba(255,255,244,.75)">The change is withheld and reported for a ruling.</div>
</div>
<div class="stamp" id="{sid}-nr" style="left:1180px;top:520px;font-size:44px">NEEDS RULING</div>
<div class="card" data-layout-allow-overlap id="{sid}-rf" style="left:1130px;top:180px;width:540px">
  <div class="ct">OWNER DASHBOARD</div>
  <div class="chip on" id="{sid}-btn" style="font-size:28px;padding:16px 26px">⟳ REFRESH DATA</div>
  {li(sid,'rf1','Pulls all six tabs','6')}{li(sid,'rf2','Connection goes LIVE','●')}{li(sid,'rf3','Task IDs re-mapped to current rows','#')}{li(sid,'rf4','Edits land in the right place','✓')}
</div>
<div class="abs" id="{sid}-tri" style="left:360px;top:370px;width:1200px;display:flex;gap:30px;justify-content:center;align-items:center">
  <span class="h2" id="{sid}-x1">Refresh</span><span class="h2 gold" id="{sid}-a1">→</span><span class="h2" id="{sid}-x2">Review</span><span class="h2 gold" id="{sid}-a2">→</span><span class="h2" id="{sid}-x3">Edit</span></div>"""
    tabjs = "".join(f"pop({L(w)},'tb{i}',0.35); tl.to($('tb{i}'),{{borderColor:'#E2B04A',color:'#E2B04A',duration:0.2}},{L(w)});\n" for i, (n, w) in enumerate(tabs))
    js = f"""{FRAMEIN} inLeft(0.2,"lab");
["tabs","log","gs","nr","rf","tri","l1","l2","l3","rf1","rf2","rf3","rf4","btn","x1","x2","x3","a1","a2"].forEach(k=>tl.set($(k),{{opacity:0}},0));
["tb0","tb1","tb2","tb3","tb4","tb5"].forEach(k=>tl.set($(k),{{opacity:0}},0));
show(0,"i1",0.01); cam(0,960,473,1,0.01); cam(0.3,960,300,1.07,6.0);
pop({L('daily')},"t1"); pop({L('operations')},"t2");
// six tabs on the sheet
out({tTabs},"tag",0.3); show({tTabs},"i2",0.5); cam({tTabs},960,473,1,0.4); cam({tTabs}+0.4,560,927,1.9,0.9,"power3.inOut");
show({tTabs}+0.2,"tabs",0.3);
{tabjs}
// rules column: only what a source supports
out({tRule},"tabs",0.3); cam({tRule},250,430,1.75,1.2,"power3.inOut");
scrim({L('every')},"scrimR",1); inUp({L('every')},"log",0.5);
inLeft({L('old')},"l1"); inLeft({L('new')},"l2"); inLeft({L('evidence')},"l3");
// no guessing
out({tGuess},"log",0.3); show({tGuess},"i3",0.5); cam({tGuess},960,473,1,0.4); cam({tGuess}+0.4,118,620,1.6,1.2,"power3.inOut");
inUp({tGuess}+0.2,"gs",0.5); slam({L('needs')},"nr",-6);
// refresh data
out({tRef},"gs",0.3); out({tRef},"nr",0.3); show({tRef},"i4",0.5); cam({tRef},960,473,1,0.5); cam({tRef}+0.5,960,300,1.08,6);
inUp({tRef}+0.1,"rf",0.5); pop({L('Refresh',1)},"btn");
tl.to($("btn"),{{scale:0.92,duration:0.08,yoyo:true,repeat:1}},{L('Refresh',1)}+0.5);
inLeft({L('six',2)},"rf1"); inLeft({L('live')},"rf2"); inLeft({L('re-maps')},"rf3"); inLeft({L('edits')},"rf4");
// triad
out({tTri},"rf",0.3); scrim({tTri},"scrimR",0,0.3); scrim({tTri},"scrim",0.8,0.4); show({tTri},"tri",0.2);
inUp({L('Refresh',2)},"x1",0.4); show({L('Review')}-0.2,"a1",0.2); inUp({L('Review')},"x2",0.4); show({L('Edit',2)}-0.2,"a2",0.2); inUp({L('Edit',2)},"x3",0.4);
"""
    return body, js

# ================================================================ S07 AFROFARMS
def s07():
    sid = "s07"; L = lambda w, n=1: wt("line07", w, n)
    body = f"""
<div class="scenelabel" id="{sid}-lab" data-layout-allow-overlap>04 · AFROFARMS</div>
{bframe(sid, "AfroFarm Command Deck", [("i1","afro_brief"),("i2","afro_trust")])}
<div class="abs" id="{sid}-tag" style="left:250px;top:780px;display:flex;gap:14px;align-items:center">
  <span class="chip on" id="{sid}-t1">daily update</span><span class="chip" id="{sid}-t2">MCP · AFK OS</span><span class="chip" id="{sid}-t3">→ command deck</span></div>"""
    js = f"""{FRAMEIN} inLeft(0.2,"lab");
show(0,"i1",0.01); cam(0,960,473,1,0.01); cam(0.3,800,300,1.12,6.5);
pop({L('daily')},"t1"); pop({L('M')},"t2"); pop({L('system')},"t3");
show({L('so')},"i2",0.5); cam({L('so')},960,330,1.3,0.5,"power2.inOut"); cam({L('so')}+0.5,960,700,1.3,4.5);
"""
    return body, js

# ================================================================ S08 BILLING
def s08():
    sid = "s08"; L = lambda w, n=1: wt("line08", w, n)
    tMail, tPlace, tChk, tNE, tCEO, tUrg = L("Every") - 0.2, L("updates"), L("It", 1) - 0.1, L("rule") - 0.4, L("CEO") - 0.3, L("separate") - 0.5
    body = f"""
<div class="scenelabel" id="{sid}-lab" data-layout-allow-overlap>05 · BILLS &amp; SUBSCRIPTIONS</div>
{bframe(sid, "Bills &amp; Subscriptions — CEO dashboard &amp; register", [("i1","bill_brief"),("i2","bill_sheet"),("i3","bill_all"),("i4","bill_dec"),("i5","bill_brief")])}
<div class="abs" id="{sid}-stats" style="left:250px;top:700px;display:flex;gap:18px">
  <div class="card" id="{sid}-n1" style="position:relative;padding:18px 28px"><span class="serif" style="font-size:64px">6</span> <span class="mono mint" style="font-size:20px">ENTITIES</span></div>
  <div class="card" id="{sid}-n2" style="position:relative;padding:18px 28px"><span class="serif gold" style="font-size:64px">71</span> <span class="mono mint" style="font-size:20px">RECURRING BILLS</span></div>
  <div class="card" id="{sid}-n3" style="position:relative;padding:18px 28px"><span class="serif" style="font-size:64px">1</span> <span class="mono mint" style="font-size:20px">REGISTER</span></div></div>
<div class="card" data-layout-allow-overlap id="{sid}-mb" style="left:1130px;top:170px;width:540px">
  <div class="ct">EVERY TWO WEEKS · 5 MAILBOXES</div><div class="ch">Searched for</div>
  {li(sid,'m1','Invoices','→')}{li(sid,'m2','Renewals','→')}{li(sid,'m3','Payment confirmations','→')}{li(sid,'m4','Cancellations','→')}
  <div class="chip on" id="{sid}-m5" style="margin-top:10px">register updated in place</div>
</div>
<div class="card" data-layout-allow-overlap id="{sid}-ck" style="left:1130px;top:200px;width:540px">
  <div class="ct">EVERY RUN</div>
  {li(sid,'k1','Marks bills paid or cancelled')}{li(sid,'k2','Adds new bills · next Ref#')}{li(sid,'k3','Keeps DEADLINES current')}{li(sid,'k4','Stores evidence for each change')}
</div>
<div class="stamp" id="{sid}-ne" style="left:560px;top:260px;font-size:52px">NOTHING IS ESTIMATED</div>
<div class="stamp" id="{sid}-uv" style="left:1180px;top:640px;font-size:30px;border-color:#E2B04A;color:#E2B04A">UNVERIFIED</div>
<div class="abs" id="{sid}-scr" style="left:250px;top:610px;width:1420px;display:flex;gap:18px">
  <div class="card" id="{sid}-sc1" style="position:relative;flex:1;padding:20px 24px"><div class="ct">1 · BRIEF</div><div style="font-size:22px">money out · outstanding · avoidable</div></div>
  <div class="card" id="{sid}-sc2" style="position:relative;flex:1;padding:20px 24px"><div class="ct">2 · DECISIONS</div><div style="font-size:22px">what needs a call from you</div></div>
  <div class="card" id="{sid}-sc3" style="position:relative;flex:1;padding:20px 24px"><div class="ct">3 · DATES</div><div style="font-size:22px">upcoming billing deadlines</div></div>
  <div class="card" id="{sid}-sc4" style="position:relative;flex:1;padding:20px 24px"><div class="ct">4 · BILLS</div><div style="font-size:22px">all 71, filterable</div></div></div>
<div class="card" id="{sid}-ur" style="left:560px;top:250px;width:800px;border-color:#E06A4E">
  <div class="ct brick">URGENT BILLING ALERTS · SEPARATE TASK</div><div class="h2" style="font-size:52px">Only what needs attention — emailed directly.</div></div>"""
    js = f"""{FRAMEIN} inLeft(0.2,"lab");
["n1","n2","n3","mb","m1","m2","m3","m4","m5","ck","k1","k2","k3","k4","ne","uv","scr","sc1","sc2","sc3","sc4","ur"].forEach(k=>tl.set($(k),{{opacity:0}},0));
show(0,"i1",0.01); cam(0,960,473,1,0.01); cam(0.3,800,350,1.06,6);
inUp({L('six')},"n1",0.45); inUp({L('seventy-one')},"n2",0.45); inUp({L('one')},"n3",0.45);
// mailboxes
out({tMail},"stats",0.3); show({tMail},"i2",0.5); cam({tMail},960,473,1,0.5); cam({tMail}+0.5,960,927,1.6,1.0,"power3.inOut"); cam({L('invoices')},700,330,1.3,3,"power2.inOut");
scrim({L('five')},"scrimR",1); inUp({L('five')},"mb",0.5);
inLeft({L('invoices')},"m1"); inLeft({L('renewals')},"m2"); inLeft({L('payment')},"m3"); inLeft({L('cancellations')},"m4"); pop({tPlace},"m5");
// every run
out({tChk},"mb",0.3); show({tChk},"i3",0.5); cam({tChk},960,473,1,0.5); cam({tChk}+0.5,900,600,1.08,6);
inUp({tChk}+0.1,"ck",0.5); inLeft({L('marks')},"k1"); inLeft({L('adds')},"k2"); inLeft({L('keeps')},"k3"); inLeft({L('stores')},"k4");
// nothing is estimated
out({tNE},"ck",0.3); scrim({tNE},"scrimR",0,0.3); show({tNE},"i4",0.5); cam({tNE},960,473,1,0.4); cam({tNE}+0.4,980,776,1.7,1.2,"power3.inOut");
slam({L('nothing')},"ne",-4); pop({L('unverified')},"uv");
// four screens
out({tCEO},"ne",0.3); out({tCEO},"uv",0.3); show({tCEO},"i5",0.5); cam({tCEO},960,473,1,0.4); cam({tCEO}+0.4,330,152,2.0,1.1,"power3.inOut");
show({L('four')}-0.2,"scr",0.2); inUp({L('brief')},"sc1",0.4); inUp({L('decisions')},"sc2",0.4); inUp({L('dates')},"sc3",0.4); inUp({L('every',3)},"sc4",0.4);
// urgent
out({tUrg},"scr",0.3); cam({tUrg},960,473,1,0.6); scrim({tUrg},"scrim",0.8,0.5); inUp({tUrg}+0.2,"ur",0.5);
"""
    return body, js

# ================================================================ S09 SAVINGS
def s09():
    sid = "s09"; La = lambda w, n=1: wt("line09a", w, n); off_b = SCENES[8][2][1][1]
    Lb = lambda w, n=1: wt("line09b", w, n, off_b)
    rows = [("r1", "Google One · 30 TB → 2 TB", "$200", "$10", La("storage")),
            ("r2", "Perplexity", "$690", "cancelled", La("Unused")),
            ("r3", "Gemini API", "$60", "cancelled", La("AI") + 0.3),
            ("r4", "ChatGPT extra seat", "$25", "cancelled", La("seats")),
            ("r5", "Maton", "$200", "$50", La("Maton")),
            ("r6", "Vercel", "$80", "$40", La("Vercel")),
            ("r7", "Wati", "$99", "$49", La("Wati"))]
    rowh = "".join(f'<div class="row" id="{sid}-{k}"><span class="nm">{n}</span><span class="was" id="{sid}-{k}w">{a}<i id="{sid}-{k}s"></i></span><span class="now">{b}</span></div>' for k, n, a, b, t in rows)
    body = f"""
<style>
.row{{display:flex;align-items:center;height:62px;border-bottom:2px solid rgba(255,255,244,.12);font-size:28px;font-weight:700;}}
.row .nm{{flex:1;}} .row .was{{width:150px;text-align:right;color:rgba(255,255,244,.6);position:relative;font-family:'IBM Plex Mono',monospace;}}
.row .was i{{position:absolute;left:10px;right:0;top:50%;height:3px;background:#E06A4E;transform-origin:0 50%;}}
.row .now{{width:200px;text-align:right;color:#93DBB3;font-family:'IBM Plex Mono',monospace;}}
.bar{{position:absolute;left:0;height:46px;border-radius:6px;transform-origin:0 50%;}}
</style>
<div class="abs kicker" id="{sid}-kick" style="left:140px;top:110px">Money saved · per month, before → after</div>
<div class="abs" id="{sid}-led" style="left:140px;top:170px;width:800px">{rowh}
  <div class="mono" id="{sid}-more" style="font-size:22px;color:#93DBB3;margin-top:16px">+ 9 more cancellations &amp; downgrades</div></div>
<div class="abs" id="{sid}-af" style="left:140px;top:720px;width:800px">
  <div class="mono" style="font-size:20px;letter-spacing:.18em;color:#E2B04A;margin-bottom:12px">AFROFARMS · RECURRING COSTS / MONTH</div>
  <div style="position:relative;height:110px">
    <div class="bar" id="{sid}-b1" style="top:0;width:780px;background:rgba(224,106,78,.85)"></div><div class="abs mono" id="{sid}-b1t" style="opacity:0;left:20px;top:8px;font-size:26px;font-weight:700">€1,383</div>
    <div class="bar" id="{sid}-b2" style="top:58px;width:238px;background:#93DBB3"></div><div class="abs mono" id="{sid}-b2t" style="opacity:0;left:260px;top:66px;font-size:26px;font-weight:700;color:#93DBB3">€422</div></div></div>
<div class="abs" id="{sid}-hero" style="left:1040px;top:260px;width:760px">
  <div class="mono" style="font-size:22px;letter-spacing:.2em;color:#93DBB3">ALREADY SAVED</div>
  <div class="serif gold" id="{sid}-big" style="font-size:190px;line-height:1">$0</div>
  <div class="serif" style="font-size:52px;margin-top:-6px">per year</div>
  <div class="mono" id="{sid}-mo" style="font-size:24px;color:rgba(255,255,244,.75);margin-top:14px">≈ $2,757 every month</div></div>
<div class="abs" id="{sid}-pot" style="left:1040px;top:640px;width:760px">
  <div class="chip on" style="font-size:30px;padding:16px 26px">→ $35,685 / year once the last pending cancellation lands</div></div>
<div class="abs mono" id="{sid}-fn" style="left:1040px;top:760px;width:760px;font-size:20px;color:rgba(255,255,244,.6)">Total includes Afrofarms’ operating-cost reduction (€1,383 → €422 / month).</div>"""
    js = f"""inUp(0.2,"kick",0.5,14);
["more","af","hero","pot","fn"].forEach(k=>tl.set($(k),{{opacity:0}},0));
""" + "".join(f'inLeft({t},"{k}",0.4); tl.fromTo($("{k}s"),{{scaleX:0}},{{scaleX:1,duration:0.35,ease:"power2.out"}},{t}+0.35);\n' for k, n, a, b, t in rows) + f"""
inUp({La('downgraded')},"more",0.4,10);
inUp({La('Afrofarms')},"af",0.5);
tl.fromTo($("b1"),{{scaleX:0}},{{scaleX:1,duration:0.8,ease:"power3.out"}},{La('thirteen')});
tl.fromTo($("b2"),{{scaleX:0}},{{scaleX:1,duration:0.8,ease:"power3.out"}},{La('four')});
show({La('thirteen')}+0.5,"b1t",0.3); show({La('four')}+0.5,"b2t",0.3);
inUp({La('Altogether')},"hero",0.5);
count({La('thirty-three')}-0.3,"big",0,33081,1.6,v=>"$"+Math.round(v).toLocaleString("en-US"));
tl.fromTo($("big"),{{scale:1}},{{scale:1.04,duration:0.25,yoyo:true,repeat:1,ease:"power2.out"}},{La('saved')});
pop({Lb('thirty-five')},"pot",0.5); inUp({Lb('pending')},"fn",0.5,10);
"""
    return body, js

# ================================================================ S10 INFRASTRUCTURE
def s10():
    sid = "s10"; L = lambda w, n=1: wt("line10", w, n)
    import math
    CX, CY = 560, 520
    dots = "".join(f'<div class="dot" id="{sid}-d{i}" style="left:{CX + 360*math.cos(2*math.pi*i/40) - 9:.1f}px;top:{CY + 360*math.sin(2*math.pi*i/40) - 9:.1f}px"></div>' for i in range(40))
    mcps = [("Maton", -90, "Maton"), ("NuExtract", 30, "NuExtract"), ("Pinecone", 150, "Pinecone")]
    mh = ""
    for i, (n, ang, w) in enumerate(mcps):
        x = CX + 210 * math.cos(math.radians(ang)); y = CY + 210 * math.sin(math.radians(ang))
        mh += f'<div class="node" id="{sid}-m{i}" style="left:{x-110:.0f}px;top:{y-36:.0f}px">{n}<small>MCP</small></div>'
    body = f"""
<style>
.dot{{position:absolute;width:18px;height:18px;border-radius:50%;background:#93DBB3;}}
.node{{position:absolute;width:220px;height:72px;border-radius:14px;background:#173b29;border:2px solid #E2B04A;display:flex;align-items:center;justify-content:center;gap:10px;font-weight:700;font-size:26px;}}
.node small{{font-family:'IBM Plex Mono',monospace;font-size:16px;color:#E2B04A;}}
.flow{{display:flex;align-items:center;gap:12px;margin:0 0 10px;}}
.flow .chip{{font-size:20px;}}
.flow .ar{{color:#E2B04A;font-family:'IBM Plex Mono',monospace;font-size:24px;}}
.blk{{margin-bottom:22px;}}
.blk .ct{{font-family:'IBM Plex Mono',monospace;font-weight:700;font-size:18px;letter-spacing:.2em;color:#E2B04A;margin-bottom:10px;}}
</style>
<div class="abs kicker" id="{sid}-kick" style="left:140px;top:90px">The layer underneath</div>
<div class="abs" id="{sid}-ring">{dots}</div>
<div class="abs mono" id="{sid}-apps" style="left:{CX-150}px;top:{CY+390}px;width:300px;text-align:center;font-size:22px;color:#93DBB3">40+ apps via Maton</div>
{mh}
<div class="abs" id="{sid}-core" style="left:{CX-95}px;top:{CY-95}px;width:190px;height:190px;border-radius:50%;background:#E2B04A;color:#143221;display:flex;align-items:center;justify-content:center;font-family:'EB Garamond',serif;font-size:52px">Claude</div>
<div class="abs" id="{sid}-col" style="left:1060px;top:150px;width:780px">
  <div class="blk" id="{sid}-q"><div class="ct">QUICKBOOKS · ROUTED</div><div class="flow"><span class="chip">QuickBooks</span><span class="ar">→</span><span class="chip on">Maton</span><span class="ar">→</span><span class="chip">Claude</span></div></div>
  <div class="blk" id="{sid}-tw"><div class="ct">MESSAGING</div><div class="flow"><span class="chip">Twilio</span><span class="chip">WhatsApp · send + read</span></div></div>
  <div class="blk" id="{sid}-sms"><div class="ct">EMAIL → SMS, AUTOMATIC</div><div class="flow"><span class="chip">Claude</span><span class="ar">→</span><span class="chip">email</span><span class="ar">→</span><span class="chip">detect</span><span class="ar">→</span><span class="chip on">SMS</span><span class="ar">→</span><span class="chip">manager</span></div></div>
  <div class="blk" id="{sid}-gi"><div class="ct">ONE OPERATING LAYER</div><div class="flow"><span class="chip on">Global instructions — same rules everywhere</span></div><div class="flow" id="{sid}-cl"><span class="chip">Skills &amp; plugins cleaned up → focused context</span></div></div>
  <div class="blk" id="{sid}-ra"><div class="ct">DATA WORK</div><div class="flow"><span class="chip">Research reports</span><span class="ar">→</span><span class="chip">structured sheet</span><span class="ar">→</span><span class="chip on">research artifact</span></div>
    <div class="flow" id="{sid}-db"><span class="chip">Local database</span><span class="ar">→</span><span class="chip">extracted · cleaned · verified</span><span class="ar">→</span><span class="chip on">dashboards</span></div></div>
</div>"""
    js = f"""inUp(0.2,"kick",0.5,14);
["apps","q","tw","sms","gi","cl","ra","db"].forEach(k=>tl.set($(k),{{opacity:0}},0));
pop(0.5,"core",0.6);
pop({L('Maton',1)},"m0"); pop({L('NuExtract')},"m1"); pop({L('Pinecone')},"m2");
tl.fromTo("#{sid}-ring .dot",{{opacity:0,scale:0}},{{opacity:1,scale:1,duration:0.3,ease:"back.out(2)",stagger:0.03}},{L('More')});
inUp({L('forty')}+0.4,"apps",0.4,10);
tl.fromTo($("ring"),{{rotation:0}},{{rotation:24,duration:{DUR[sid]},ease:"none",transformOrigin:"{CX}px {CY}px"}},0);
inUp({L('QuickBooks')},"q",0.45); inUp({L('Twilio')},"tw",0.45); inUp({L('email-to-s')},"sms",0.45);
inUp({L('Global')},"gi",0.45); inUp({L('cleanup')},"cl",0.45); inUp({L('research')},"ra",0.45); inUp({L('extracted')},"db",0.45);
tl.fromTo($("core"),{{scale:1}},{{scale:1.06,duration:1.6,yoyo:true,repeat:{int(DUR[sid]/3.2)},ease:"sine.inOut"}},1.2);
"""
    return body, js

# ================================================================ S11 CLOSE
PRINCIPLES = [("Source-backed updates", "Source"), ("Verify ≠ write", "Verification"), ("Every change auditable", "change"),
              ("No duplicates", "duplicates"), ("Each business isolated", "business"), ("Live dashboards", "Live"),
              ("No guessing, ever", "guessing"), ("Automated maintenance", "automated")]
def s11():
    sid = "s11"; L = lambda w, n=1: wt("line11", w, n)
    cells = "".join(f'<div class="pr" id="{sid}-p{i}"><span class="pn">0{i+1}</span>{n}</div>' for i, (n, w) in enumerate(PRINCIPLES))
    rail = "".join(f'<div class="rs" id="{sid}-rs{i}">{n}</div>' for i, (n, d) in enumerate(STATIONS))
    body = f"""
<style>
.pr{{height:96px;border-top:2px solid rgba(255,255,244,.2);display:flex;align-items:center;gap:22px;font-family:'EB Garamond',serif;font-size:46px;}}
.pr .pn{{font-family:'IBM Plex Mono',monospace;font-size:20px;color:#E2B04A;}}
.rs{{width:250px;height:58px;border:2px solid #E2B04A;border-radius:10px;display:flex;align-items:center;justify-content:center;font-family:'IBM Plex Mono',monospace;font-weight:700;font-size:22px;letter-spacing:.1em;color:#E2B04A;}}
</style>
<div class="abs" id="{sid}-rail" style="left:140px;top:90px;width:1640px;display:flex;justify-content:space-between">{rail}</div>
<div class="abs h2" id="{sid}-head" style="left:140px;top:200px;width:1640px">Across all of it, the same rules hold.</div>
<div class="abs" id="{sid}-grid" style="left:140px;top:330px;width:1640px;display:grid;grid-template-columns:1fr 1fr;column-gap:80px">{cells}</div>
<div class="abs" id="{sid}-end" data-layout-allow-overlap style="left:0;top:0;width:1920px;height:1080px;background:#143221;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:18px">
  <div class="kicker">Automation &amp; dashboard work</div>
  <div class="serif" style="font-size:120px;line-height:1">Sawera Khadium</div>
  <div class="serif gold" style="font-size:48px">AI automation · live dashboards · source-backed data</div>
  <div class="mono" style="font-size:26px;color:#93DBB3;margin-top:20px">linkedin.com/in/sawera-khadium</div></div>"""
    pj = "".join(f"inLeft({L(w) if w != 'change' else L('change')},'p{i}',0.45);\n" for i, (n, w) in enumerate(PRINCIPLES))
    tEnd = round(W["line11"]["dur"] + 0.5 + 0.4, 2)
    js = f"""tl.set($("end"),{{opacity:0}},0);
""" + "".join(f"pop({0.3 + i*0.12},'rs{i}',0.4);\n" for i in range(5)) + f"""
inUp(0.4,"head",0.6);
{pj}
tl.to([$("grid"),$("head"),$("rail")],{{opacity:0,duration:0.6,ease:"power1.in"}},{tEnd} - 0.3);
tl.fromTo($("end"),{{opacity:0}},{{opacity:1,duration:0.9,ease:"power2.inOut"}},{tEnd});
tl.to($("end"),{{opacity:0,duration:0.8,ease:"power1.in"}},{DUR[sid] - 0.9});
"""
    return body, js

BUILDERS = [s01, s02, s03, s04, s05, s06, s07, s08, s09, s10, s11]

# ---------------------------------------------------------------- captions
SUBS = [(["thirty-three", "thousand", "dollars"], "$33,000"), (["thirty-five", "thousand", "seven", "hundred"], "35,700"),
        (["thirteen", "hundred", "and", "eighty", "euros"], "€1,380"), (["four", "hundred", "and", "twenty."], "€420."),
        (["M", "C", "P"], "MCP"), (["A", "I"], "AI"), (["email-to-S", "M", "S"], "email-to-SMS")]
ONE = {"fifteen": "15", "seventy-one": "71", "forty": "40"}
def caption_groups():
    out = []
    for sid, f, vos, d in SCENES:
        for vo, ls in vos:
            ws = [dict(w) for w in W[vo]["words"]]
            # phrase substitutions
            i = 0; toks = []
            while i < len(ws):
                hit = None
                for pat, rep in SUBS:
                    seg = [w["text"].rstrip(".,:;") if j < len(pat) - 1 else w["text"] for j, w in enumerate(ws[i:i + len(pat)])]
                    if len(seg) == len(pat) and [s.rstrip(".,:;").lower() for s in seg] == [p.rstrip(".,:;").lower() for p in pat]:
                        last = ws[i + len(pat) - 1]["text"]; punct = re.sub(r"^.*?([.,:;]*)$", r"\1", last)
                        hit = ({"text": rep.rstrip(".") + punct, "start": ws[i]["start"], "end": ws[i + len(pat) - 1]["end"]}, len(pat)); break
                if hit: toks.append(hit[0]); i += hit[1]
                else:
                    w = ws[i]; core = re.sub(r"[.,:;]+$", "", w["text"])
                    if core.lower() in ONE: w["text"] = ONE[core.lower()] + w["text"][len(core):]
                    toks.append(w); i += 1
            g = []
            for k, w in enumerate(toks):
                g.append(w)
                nxt = toks[k + 1] if k + 1 < len(toks) else None
                brk = len(g) >= 7 or (w["text"][-1] in ".,:;?!" and len(g) >= 3) or (w["text"][-1] in ".?!") or nxt is None or (nxt["start"] - w["end"] > 0.45)
                if brk:
                    base = START[sid] + ls
                    out.append({"t": round(base + g[0]["start"], 3), "e": round(base + g[-1]["end"], 3), "text": " ".join(x["text"] for x in g)})
                    g = []
    for i in range(len(out)):
        nxt = out[i + 1]["t"] if i + 1 < len(out) else 1e9
        out[i]["d"] = round(min(nxt, out[i]["e"] + 0.5) - out[i]["t"], 3)
    return out

def cap_html(text):
    s = html.escape(text, quote=False)
    return re.sub(r"(\$?€?\d[\d,]*\+?)", r'<span class="cn">\1</span>', s)

# ---------------------------------------------------------------- root
RAIL_CUES = {
    "s03": [(0, 3)],
    "s04": [(0, 3), (wt("line04", "Updater") - 0.3, 2), (wt("line04", "Verifier") - 0.3, 1)],
    "s05": [(0, 3), (wt("line05", "Every", 1), 0), (wt("line05", "emails") - 0.2, 1), (wt("line05", "reply") - 0.2, 4), (wt("line05", "Every", 2) - 0.2, 2)],
    "s06": [(0, 1), (wt("line06", "It", 1) - 0.2, 2), (wt("line06", "When") - 0.2, 4), (wt("line06", "owner's") - 0.4, 3)],
    "s07": [(0, 0), (wt("line07", "so"), 3)],
    "s08": [(0, 2), (wt("line08", "Every") - 0.2, 0), (wt("line08", "updates"), 2), (wt("line08", "rule") - 0.4, 1), (wt("line08", "CEO") - 0.3, 3), (wt("line08", "separate") - 0.5, 4)],
}
def main():
    for (sid, f, vos, d), b in zip(SCENES, BUILDERS):
        body, js = b()
        open(os.path.join(FRAMES, f + ".html"), "w", encoding="utf-8").write(tmpl(sid, body, js))
    import sys; sys.path.insert(0, os.path.dirname(os.path.abspath(__file__))); import layers; open(os.path.join(ROOT, "index.html"), "w", encoding="utf-8").write(layers.build_root(sys.modules[__name__]))
    print("total", TOTAL, "scenes", {s: (START[s], DUR[s]) for s in START})
    print("captions", len(caption_groups()))

if __name__ == "__main__":
    main()
