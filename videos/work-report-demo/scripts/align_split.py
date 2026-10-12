"""Align script words to Groq word timings per batch; split per-line WAVs; write captions words.
Outputs assets/vo/lineNN.wav (+ line09a/line09b) and assets/vo/words.json {line: [{text,start,end}] local to that wav}."""
import json, re, subprocess, difflib, os
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__))); VO = os.path.join(ROOT, "assets", "vo")
B = json.load(open(os.path.join(VO, "batches.json")))
lines = {int(k): v for k, v in B["lines"].items()}
NUM = {"15": "fifteen", "71": "seventy-one", "40": "forty"}
def norm(w):
    w = w.lower().replace("’", "'"); return re.sub(r"[^a-z0-9']", "", w)
def wsplit(s): return [t for t in s.split() if norm(t)]
out_words = {}; PAD_A, PAD_B = 0.12, 0.35
def cut(src, a, b, dst):
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", src, "-ss", f"{a:.3f}", "-to", f"{b:.3f}",
                    "-af", "loudnorm=I=-16:TP=-1.5:LRA=7", "-ar", "48000", "-ac", "1", dst], check=True)
for gi, g in enumerate(B["groups"]):
    src = os.path.join(VO, f"_batch_g{gi}.wav")
    gw = json.load(open(os.path.join(VO, f"_batch_g{gi}.groq.json")))["words"]
    # expand whisper tokens: split "$33,000"-style into nothing special; just normalise
    wt = [norm(NUM.get(w["word"].strip(), w["word"])) for w in gw]
    script = []  # (line, token)
    for n in g:
        for t in wsplit(lines[n]): script.append((n, t))
    st = [norm(t) for _, t in script]
    sm = difflib.SequenceMatcher(a=st, b=wt, autojunk=False)
    times = [None] * len(st)
    for a, b, size in sm.get_matching_blocks():
        for k in range(size): times[a + k] = (gw[b + k]["start"], gw[b + k]["end"])
    # interpolate gaps
    i = 0
    while i < len(st):
        if times[i] is None:
            j = i
            while j < len(st) and times[j] is None: j += 1
            t0 = times[i - 1][1] if i > 0 else 0.0
            t1 = times[j][0] if j < len(st) else gw[-1]["end"]
            span = max(t1 - t0, 0.05); L = sum(len(st[k]) + 2 for k in range(i, j)); acc = t0
            for k in range(i, j):
                d = span * (len(st[k]) + 2) / L; times[k] = (acc, acc + d); acc += d
            i = j
        else: i += 1
    matched = sum(1 for a, b, s in sm.get_matching_blocks() for _ in range(s))
    print(f"g{gi}: script {len(st)} tokens, matched {matched}")
    for n in g:
        idx = [k for k, (ln, _) in enumerate(script) if ln == n]
        a = max(0, times[idx[0]][0] - PAD_A); b = times[idx[-1]][1] + PAD_B
        segs = [(f"line{n:02d}", idx, a, b)]
        if n == 9:  # split before "And about thirty-five..." for the held frame
            k_and = [k for k in idx if script[k][1] == "And" and script[k + 1][1] == "about" and script[k + 2][1].startswith("thirty-five")][0]
            mid_a = times[k_and - 1][1] + 0.25; mid_b = times[k_and][0] - 0.08
            segs = [("line09a", [k for k in idx if k < k_and], a, mid_a), ("line09b", [k for k in idx if k >= k_and], mid_b, b)]
        for name, ids, sa, sb in segs:
            cut(src, sa, sb, os.path.join(VO, f"{name}.wav"))
            out_words[name] = {"dur": round(sb - sa, 3), "words": [{"text": script[k][1], "start": round(times[k][0] - sa, 3), "end": round(times[k][1] - sa, 3)} for k in ids]}
            print(f"  {name}: {sb - sa:6.2f}s  {len(ids)} words")
for k in out_words:  # real duration of the written file
    out_words[k]["dur"] = round(float(subprocess.check_output(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", os.path.join(VO, k + ".wav")])), 3)
json.dump(out_words, open(os.path.join(VO, "words.json"), "w"), indent=1)
print("total", round(sum(v["dur"] for v in out_words.values()), 2))
