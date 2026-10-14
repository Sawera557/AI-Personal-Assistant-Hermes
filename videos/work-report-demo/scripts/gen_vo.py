"""Generate per-line VO with Gemini Kore from SCRIPT.md. usage: python scripts/gen_vo.py [line numbers...]"""
import re, sys, os, json, importlib.util
HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("tts", os.path.join(HERE, "..", "..", "..", "demo-video", "tts.py"))
tts = importlib.util.module_from_spec(spec); spec.loader.exec_module(tts)
txt = open(os.path.join(HERE, "..", "SCRIPT.md"), encoding="utf-8").read()
lines = re.findall(r"## Line (\d+) — .*?\*\*Delivery:\*\* (.*?)\n\n    (.+?)\n", txt, re.S)
want = set(sys.argv[1:])
style_base = "Read in a warm, confident, unhurried voice, like a builder proudly walking a peer through real work"
out = {}
for n, deliv, text in lines:
    if want and n not in want: continue
    path = os.path.join(HERE, "..", "assets", "vo", f"line{int(n):02d}.wav")
    d = tts.tts("Kore", path, text, f"{style_base}. {deliv}")
    out[n] = round(d, 2); print(n, out[n], flush=True)
json.dump(out, open(os.path.join(HERE, "..", "assets", "vo", "durations.json"), "w"), indent=1)
