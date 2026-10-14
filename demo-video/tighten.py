"""Shorten silent gaps (>3s) in a rendered video to 2s, outside protected ranges.
usage: python tighten.py IN.mp4 OUT.mp4 protect_start-protect_end ..."""
import re, subprocess, sys
IN, OUT = sys.argv[1], sys.argv[2]
protect = [tuple(map(float, p.split("-"))) for p in sys.argv[3:]]
err = subprocess.run(["ffmpeg", "-i", IN, "-vn", "-af", "silencedetect=n=-45dB:d=3", "-f", "null", "-"],
                     capture_output=True, text=True, encoding="utf-8", errors="replace").stderr
dur = float(subprocess.check_output(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", IN]))
st = [float(x) for x in re.findall(r"silence_start: ([\d.]+)", err)]
en = [float(x) for x in re.findall(r"silence_end: ([\d.]+)", err)]
drops = []
for a, b in zip(st, en):
    a2, b2 = a + 1.0, b - 1.0
    if a < 5 or b > dur - 3 or any(not (b2 < p0 or a2 > p1) for p0, p1 in protect):
        continue
    drops.append((a2, b2))
keep, cur = [], 0.0
for a, b in drops:
    keep.append((cur, a)); cur = b
keep.append((cur, dur))
print("dropping", round(sum(b - a for a, b in drops), 1), "s in", len(drops), "gaps ->", round(sum(b - a for a, b in keep), 1), "s")
fc, lab = [], ""
for i, (a, b) in enumerate(keep):
    fc.append(f"[0:v]trim={a:.3f}:{b:.3f},setpts=PTS-STARTPTS[v{i}]")
    fc.append(f"[0:a]atrim={a:.3f}:{b:.3f},asetpts=PTS-STARTPTS,afade=t=in:d=0.03,afade=t=out:st={b-a-0.03:.3f}:d=0.03[a{i}]")
    lab += f"[v{i}][a{i}]"
fc.append(lab + f"concat=n={len(keep)}:v=1:a=1[v][a]")
subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", IN, "-filter_complex", ";".join(fc), "-map", "[v]", "-map", "[a]",
                "-c:v", "libx264", "-preset", "slow", "-crf", "17", "-pix_fmt", "yuv420p", "-r", "30",
                "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", OUT], check=True)
print("wrote", OUT)
