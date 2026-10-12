"""Batch narration lines into few Gemini TTS requests (plain text, no style -> nothing extra read aloud).
usage: python scripts/gen_vo_batched.py MODEL"""
import re, sys, os, json, importlib.util
HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.join(HERE, "..")
spec = importlib.util.spec_from_file_location("tts", os.path.join(ROOT, "..", "..", "demo-video", "tts.py"))
T = importlib.util.module_from_spec(spec); spec.loader.exec_module(T)
T.MODEL = sys.argv[1]
txt = open(os.path.join(ROOT, "SCRIPT.md"), encoding="utf-8").read()
lines = {int(n): t for n, t in re.findall(r"## Line (\d+) — .*?\n\n    (.+?)\n", txt, re.S)}
groups = [[1, 2, 3], [4, 5, 6], [7, 8, 9], [10, 11]]
os.makedirs(os.path.join(ROOT, "assets", "vo"), exist_ok=True)
meta = {}
for gi, g in enumerate(groups):
    out = os.path.join(ROOT, "assets", "vo", f"_batch_g{gi}.wav")
    if os.path.exists(out): print("skip", out); continue
    d = T.tts("Kore", out, "\n\n".join(lines[n] for n in g))
    meta[f"g{gi}"] = {"lines": g, "dur": round(d, 2)}; print(gi, round(d, 2), flush=True)
json.dump({"model": T.MODEL, "groups": groups, "lines": lines}, open(os.path.join(ROOT, "assets", "vo", "batches.json"), "w"), indent=1)
