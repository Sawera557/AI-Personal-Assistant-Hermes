import json, subprocess, numpy as np
C = json.load(open("scripts/footage_shots.json")); W, H = 480, 236; fps = 5
p = subprocess.run(["ffmpeg", "-v", "error", "-i", C["src"], "-vf", f"fps={fps},scale={W}:{H},format=gray", "-f", "rawvideo", "-"], capture_output=True)
fr = np.frombuffer(p.stdout, np.uint8).reshape(-1, H, W).astype(np.int16)
# fraction of pixels changed > 12 between consecutive frames (cursor touches few pixels)
ch = [(np.abs(fr[i] - fr[i - 1]) > 12).mean() for i in range(1, len(fr))]
np.save("scripts/motion.npy", np.array(ch))
runs = []; s = None
for i, c in enumerate(ch):
    t = (i + 1) / fps
    if c < 0.004:
        if s is None: s = t
    else:
        if s is not None and t - s >= 1.2: runs.append((round(s, 1), round(t, 1)))
        s = None
if s is not None: runs.append((round(s, 1), round(len(ch) / fps, 1)))
for a, b in runs: print(f"{int(a//60)}:{a%60:04.1f}-{int(b//60)}:{b%60:04.1f}  ({a}-{b}, {b-a:.1f}s)")
