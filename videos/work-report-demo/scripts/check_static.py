"""For each shot, measure motion inside its crisp keep-rects (scroll = privacy risk)."""
import json, subprocess, numpy as np, sys
C = json.load(open("scripts/footage_shots.json")); R = C["regions"]
W, H = 960, 473
def frames(t0, t1, fps=4):
    p = subprocess.run(["ffmpeg", "-v", "error", "-ss", str(t0), "-to", str(t1), "-i", C["src"], "-vf", f"fps={fps},scale={W}:{H},format=gray", "-f", "rawvideo", "-"], capture_output=True)
    a = np.frombuffer(p.stdout, np.uint8); return a.reshape(-1, H, W).astype(np.int16)
for scene, shots in C["scenes"].items():
    for i, s in enumerate(shots):
        fr = frames(s["in"], s["out"])
        names = s.get("keep") or s.get("rects")
        rep = []
        for n in names:
            x0, y0, x1, y1 = [v // 2 for v in R[n]]
            reg = fr[:, y0:y1, x0:x1]
            d = np.abs(reg - reg[0]).mean(axis=(1, 2))
            rep.append(f"{n}={d.max():.1f}")
        print(scene, i, f"{s['in']}-{s['out']}", " ".join(rep))
