"""Turn a raw take into the final video.

usage: python post.py N [--keep-waits]
  - aligns audio to video using the flash + beep sync marker
  - trims to the storyboard, cuts Jarvis's "checking Hermes" dead air
  - encodes takes/hermes-demo_take_N.mp4
"""
import json, re, subprocess, sys

N = sys.argv[1]
PICK = sys.argv[sys.argv.index("--pickup") + 1] if "--pickup" in sys.argv else None
KEEP_WAITS = "--keep-waits" in sys.argv
OUT = f"takes/hermes-demo_take_{N}" + (f"+{PICK}" if PICK else "") + ".mp4"


def run(args):
    return subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace")


class Take:
    def __init__(self, n):
        self.raw, self.events = f"takes/take_{n}.mkv", json.load(open(f"takes/take_{n}.json"))
        out = run(["ffmpeg", "-t", "25", "-i", self.raw, "-vf", "scale=192:108,signalstats,metadata=print:key=lavfi.signalstats.YAVG",
                   "-an", "-f", "null", "-"]).stderr
        self.flash = next((float(m.group(1)) for m in re.finditer(r"pts_time:([\d.]+)[^\n]*\n[^\n]*YAVG=([\d.]+)", out)
                           if float(m.group(2)) > 200), None)
        out = run(["ffmpeg", "-t", "25", "-i", self.raw, "-vn", "-af", "bandpass=f=1000:w=200,silencedetect=n=-30dB:d=0.1",
                   "-f", "null", "-"]).stderr
        ends = [float(x) for x in re.findall(r"silence_end: ([\d.]+)", out)]
        if self.flash is None or not ends:
            sys.exit(f"{self.raw}: sync marker not found")
        self.av = ends[0] - self.flash                 # >0: audio lags video
        self.sync_t = self.ev("sync") - 0.25
        print(f"{self.raw}: flash {self.flash:.3f}s, beep {ends[0]:.3f}s -> audio offset {self.av:+.3f}s")

    def ev(self, name, **match):
        for e in self.events:
            if e["ev"] == name and all(e.get(k) == v for k, v in match.items()):
                return e["t"]
        return None

    def v(self, t):                                    # event clock -> video clock
        return self.flash + (t - self.sync_t)


DUR = {k.split("/")[-1][:-4]: v for k, v in json.load(open("audio/durations.json")).items()}
main = Take(N)
start = main.flash + 0.6
if PICK:
    pick = Take(PICK)
    JOIN = sys.argv[sys.argv.index("--join") + 1] if "--join" in sys.argv else None
    splice_main = (main.ev(JOIN) + 0.8) if JOIN else (main.ev("splice") or (main.ev("say", key="n14_tasks") + DUR["n14_tasks"] + 0.4))
    main_end = main.v(splice_main)
else:
    main_end = main.v(main.ev("end")) - 0.4

cuts = []
if not KEEP_WAITS:
    for q in ("q1", "q2"):
        a, b = main.ev("jarvis-react", q=q), main.ev("jarvis-speaking", q=q)
        react = next((e.get("state") for e in main.events if e["ev"] == "jarvis-react" and e.get("q") == q), None)
        if a is not None and b is not None and react == "thinking" and (b - a) > 4.5:
            cuts.append((main.v(a) + 2.5, main.v(b) - 1.0))
segs, cur = [], start
for a, b in sorted(cuts):
    segs.append((main, cur, a)); cur = b
segs.append((main, cur, main_end))
if PICK:
    pcur, pend = pick.v(pick.ev("splice") - 0.3), pick.v(pick.ev("end")) - 0.4
    sent, done = pick.ev("agent-sent"), pick.ev("agent-done")
    if sent is not None and done is not None and not KEEP_WAITS:
        a, b = pick.v(sent + DUR["n09b_agent_wait"] + 1.2), pick.v(done) - 1.5
        if b - a > 2:
            segs.append((pick, pcur, a)); pcur = b
    segs.append((pick, pcur, pend))
print("segments:", [(t.raw[-10:], round(a, 2), round(b, 2)) for t, a, b in segs])
print("final length ~", round(sum(b - a for _, a, b in segs), 1), "s")

# 4. build the filter graph (each take's audio shifted by its own offset)
takes = [main] + ([pick] if PICK else [])
fc, labels = [], []
for i, (t, a, b) in enumerate(segs):
    k = takes.index(t)
    fc.append(f"[{k}:v]trim={a:.3f}:{b:.3f},setpts=PTS-STARTPTS[v{i}]")
    fc.append(f"[{k}:a]atrim={a + t.av:.3f}:{b + t.av:.3f},asetpts=PTS-STARTPTS,afade=t=in:d=0.04,afade=t=out:st={b - a - 0.04:.3f}:d=0.04[a{i}]")
    labels.append(f"[v{i}][a{i}]")
total = sum(b - a for _, a, b in segs)
fc.append("".join(labels) + f"concat=n={len(segs)}:v=1:a=1[vc][ac]")
fc.append(f"[vc]fade=t=in:d=0.6,fade=t=out:st={total - 1.2:.3f}:d=1.2[vo]")
fc.append(f"[ac]afade=t=out:st={total - 1.2:.3f}:d=1.2,loudnorm=I=-16:TP=-1.5[ao]")
inputs = sum((["-i", t.raw] for t in takes), [])
r = run(["ffmpeg", "-y", "-loglevel", "error", *inputs, "-filter_complex", ";".join(fc), "-map", "[vo]", "-map", "[ao]",
         "-c:v", "libx264", "-preset", "slow", "-crf", "18", "-pix_fmt", "yuv420p", "-r", "30",
         "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-movflags", "+faststart", OUT])
if r.returncode:
    sys.exit(r.stderr[-2000:])
print("wrote", OUT)
