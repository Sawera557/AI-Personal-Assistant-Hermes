"""Root + persistent layers (background, rail, captions) as sub-compositions. Imported by build.py."""
import os


def sub(cid, dur, css, body, js):
    return f"""<!doctype html>
<html lang="en"><head><meta charset="UTF-8" /></head>
<body>
<template>
<style>
#{cid}-root{{position:absolute;inset:0;overflow:hidden;}}
{css}
</style>
<div id="{cid}-root" data-composition-id="{cid}" data-width="1920" data-height="1080" data-duration="{dur}">
{body}
</div>
<script>
(function(){{
const tl = gsap.timeline({{ paused: true }});
{js}
window.__timelines["{cid}"] = tl;
}})();
</script>
</template>
</body></html>
"""


def build_layers(B):
    TOTAL, START, XFADE, STATIONS = B.TOTAL, B.START, B.XFADE, B.STATIONS
    lines = "".join(f'<div class="hl" style="top:{y}px"></div>' for y in range(40, 1240, 60))
    bg = sub("bgc", TOTAL, """
#bgc-root{background:#143221;}
#bgc-glow{position:absolute;left:1100px;top:-300px;width:1200px;height:1200px;border-radius:50%;background:radial-gradient(circle,rgba(226,176,74,.16) 0%,rgba(226,176,74,0) 65%);}
#bgc-lines{position:absolute;left:0;top:0;width:1920px;height:1300px;}
.hl{position:absolute;left:0;width:1920px;height:1px;background:rgba(147,219,179,.10);}""",
        f'<div id="bgc-glow" data-layout-allow-overflow></div><div id="bgc-lines" data-layout-allow-overflow>{lines}</div>',
        f'tl.fromTo("#bgc-lines",{{y:0}},{{y:-120,duration:{TOTAL},ease:"none"}},0);\n'
        f'tl.fromTo("#bgc-glow",{{scale:1,opacity:.55}},{{scale:1.15,opacity:.8,duration:6,yoyo:true,repeat:{int(TOTAL / 6)},ease:"sine.inOut"}},0);')

    r0, r1 = START["s03"], START["s09"] + XFADE
    stations = "".join(f'<div class="rst" id="rail-st{i}">{n}</div>' for i, (n, d) in enumerate(STATIONS))
    rjs = [f'tl.fromTo("#rail-in",{{opacity:0,y:-16}},{{opacity:1,y:0,duration:0.6,ease:"power3.out"}},0);',
           f'tl.to("#rail-in",{{opacity:0,y:-16,duration:0.5,ease:"power2.in"}},{round(START["s09"] - r0, 3)});']
    for sid, cues in B.RAIL_CUES.items():
        for lt, idx in cues:
            T = round(START[sid] + lt - r0, 3)
            rjs.append(f'tl.to("#rail-puck",{{x:{idx * 182},duration:0.5,ease:"expo.inOut"}},{T});')
            for j in range(5):
                col = "#143221" if j == idx else "rgba(255,255,244,.7)"
                rjs.append(f'tl.to("#rail-st{j}",{{color:"{col}",duration:0.3}},{round(T + 0.15, 3)});')
    W5 = 5 * 170 + 4 * 12
    pos = "".join(f"#rail-st{i}{{left:{i * 182}px;}}" for i in range(5))
    rail = sub("rail", round(r1 - r0, 3), f"""
#rail-in{{position:absolute;left:{(1920 - W5) // 2}px;top:26px;width:{W5}px;height:48px;}}
#rail-puck{{position:absolute;left:0;top:0;width:170px;height:48px;border-radius:10px;background:#E2B04A;}}
.rst{{position:absolute;top:0;width:170px;height:48px;border:2px solid rgba(255,255,244,.25);border-radius:10px;display:flex;align-items:center;justify-content:center;font-family:'IBM Plex Mono',monospace;font-weight:700;font-size:18px;letter-spacing:.1em;color:rgba(255,255,244,.7);}}
{pos}""", f'<div id="rail-in"><div id="rail-puck"></div>{stations}</div>', "\n".join(rjs))

    caps = B.caption_groups()
    CAPCSS = """
.cap{position:absolute;left:0;top:920px;width:1920px;height:110px;}
.cap .cp{position:absolute;left:50%;top:50%;opacity:0;white-space:normal;width:max-content;}
.cp{display:inline-block;max-width:1600px;text-align:center;font-family:'Montserrat',sans-serif;font-weight:700;font-size:38px;line-height:1.25;color:#FFFFF4;background:rgba(12,32,21,.82);padding:10px 28px;border-radius:12px;}
.cn{color:#E2B04A;}"""
    NL = chr(10)
    NCH = 5; per = -(-len(caps) // NCH); chunks = []
    for ci in range(NCH):
        part = caps[ci * per:(ci + 1) * per]
        if not part: continue
        c0 = round(part[0]["t"] - 0.05, 3); c1 = round(part[-1]["t"] + part[-1]["d"] + 0.05, 3)
        cid = f"cap{ci + 1}"
        caph = f'<div id="{cid}-box" class="cap">' + "".join(f'<span class="cp" id="{cid}-{i}">{B.cap_html(c["text"])}</span>' for i, c in enumerate(part)) + "</div>"
        cjs = NL.join(f'tl.fromTo("#{cid}-{i}",{{opacity:0,xPercent:-50,yPercent:-50,y:8}},{{opacity:1,xPercent:-50,yPercent:-50,y:0,duration:0.12,ease:"power2.out"}},{round(c["t"] - c0, 3)}); tl.to("#{cid}-{i}",{{opacity:0,duration:0.06}},{round(c["t"] - c0 + c["d"] - 0.06, 3)});' for i, c in enumerate(part))
        chunks.append((cid, c0, round(c1 - c0, 3), sub(cid, round(c1 - c0, 3), CAPCSS, caph, cjs)))
    B.CAP_CHUNKS = [(cid, c0, d) for cid, c0, d, _ in chunks]
    for cid, c0, d, html_ in chunks:
        open(os.path.join(B.ROOT, "compositions", f"captions-{cid}.html"), "w", encoding="utf-8").write(html_)
    L = os.path.join(B.ROOT, "compositions")
    for n, c in (("background", bg), ("rail", rail)):
        open(os.path.join(L, n + ".html"), "w", encoding="utf-8").write(c)
    return r0, r1


def build_root(B):
    TOTAL, START, XFADE, SCENES, DUR, W = B.TOTAL, B.START, B.XFADE, B.SCENES, B.DUR, B.W
    order = [s for s, *_ in SCENES]
    r0, r1 = build_layers(B)

    def host(hid, cid, src, start, dur, track):
        return (f'<div id="{hid}" data-composition-id="{cid}" data-composition-src="{src}" data-start="{start}" '
                f'data-duration="{dur}" data-track-index="{track}" data-width="1920" data-height="1080"></div>')
    hosts = [host("h-bgc", "bgc", "compositions/background.html", 0, TOTAL, 0)]
    for i, (sid, f, vos, d) in enumerate(SCENES):
        hd = round(DUR[sid] + (XFADE if i < len(SCENES) - 1 else 0), 3)
        hosts.append(host(f"h-{sid}", sid, f"compositions/frames/{f}.html", START[sid], hd, 1 + (i % 2)))
    hosts.append(host("h-rail", "rail", "compositions/rail.html", r0, round(r1 - r0, 3), 3))
    for cid, c0, d in B.CAP_CHUNKS:
        hosts.append(host(f"h-{cid}", cid, f"compositions/captions-{cid}.html", c0, d, 4))
    audio = []
    for sid, f, vos, d in SCENES:
        for vo, ls in vos:
            audio.append(f'<audio id="a-{vo}" src="assets/vo/{vo}.wav" data-start="{round(START[sid] + ls, 3)}" '
                         f'data-duration="{W[vo]["dur"]}" data-track-index="6" data-volume="1"></audio>')
    js = []
    kinds = {"s02": "push", "s03": "fade", "s04": "push", "s05": "push", "s06": "push", "s07": "push",
             "s08": "push", "s09": "up", "s10": "zoom", "s11": "fade"}
    X = XFADE
    for i in range(1, len(order)):
        a, b = order[i - 1], order[i]; T = START[b]; k = kinds[b]
        if k == "push":
            js.append(f'tl.fromTo("#h-{a}",{{xPercent:0}},{{xPercent:-100,duration:{X},ease:"power3.inOut",immediateRender:false}},{T});')
            js.append(f'tl.fromTo("#h-{b}",{{xPercent:100}},{{xPercent:0,duration:{X},ease:"power3.inOut"}},{T});')
        elif k == "up":
            js.append(f'tl.fromTo("#h-{a}",{{yPercent:0}},{{yPercent:-100,duration:{X},ease:"power3.inOut",immediateRender:false}},{T});')
            js.append(f'tl.fromTo("#h-{b}",{{yPercent:100}},{{yPercent:0,duration:{X},ease:"power3.inOut"}},{T});')
        elif k == "zoom":
            js.append(f'tl.fromTo("#h-{a}",{{scale:1,opacity:1}},{{scale:1.25,opacity:0,duration:{X},ease:"power2.in",immediateRender:false}},{T});')
            js.append(f'tl.fromTo("#h-{b}",{{scale:0.85,opacity:0}},{{scale:1,opacity:1,duration:{X},ease:"power2.out"}},{T});')
        else:
            js.append(f'tl.fromTo("#h-{a}",{{opacity:1}},{{opacity:0,duration:{X},ease:"power1.inOut",immediateRender:false}},{T});')
            js.append(f'tl.fromTo("#h-{b}",{{opacity:0}},{{opacity:1,duration:{X},ease:"power1.inOut"}},{T});')
    nl = "\n"
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=1920, height=1080" />
<script src="https://cdn.jsdelivr.net/npm/gsap@3.14.2/dist/gsap.min.js"></script>
<style>
*{{margin:0;padding:0;box-sizing:border-box;}}
html,body{{margin:0;width:1920px;height:1080px;overflow:hidden;background:#143221;}}
#root{{position:relative;width:100%;height:100%;overflow:hidden;background:#143221;}}
[data-composition-id="main"] > div[data-composition-src]{{position:absolute;inset:0;}}
</style>
</head>
<body>
<div id="root" data-composition-id="main" data-start="0" data-duration="{TOTAL}" data-width="1920" data-height="1080">
{nl.join(hosts)}
{nl.join(audio)}
</div>
<script>
const tl = gsap.timeline({{ paused: true }});
{nl.join(js)}
window.__timelines["main"] = tl;
</script>
</body>
</html>
"""
