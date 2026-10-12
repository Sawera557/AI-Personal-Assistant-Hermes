"""Batch several lines per TTS request, then split on the N-1 longest pauses."""
import json, subprocess, re, sys, os, wave
import tts as T
MODEL = sys.argv[1]; T.MODEL = MODEL
cfg = json.load(open("narration.json", encoding="utf-8"))
SHIFT = {"n03_columns": 1, "n13_agents": 1}
def ffdur(p): return float(subprocess.check_output(["ffprobe","-v","error","-show_entries","format=duration","-of","csv=p=0",p]))
def batch(voice, items, tag):
    raw = f"audio/_batch_{tag}.wav"
    if not os.path.exists(raw):
        T.tts(voice, raw, "\n\n".join(v for _, v in items))
    out = subprocess.run(["ffmpeg","-i",raw,"-af","silencedetect=noise=-45dB:d=0.25","-f","null","-"],capture_output=True,text=True).stderr
    starts=[float(x) for x in re.findall(r"silence_start: ([\d.]+)",out)]
    ends=[float(x) for x in re.findall(r"silence_end: ([\d.]+)",out)]
    sil=[(e-s,s,e) for s,e in zip(starts,ends) if s>0.3]
    total=ffdur(raw)
    words=[len(v.split()) for _,v in items]; tw=sum(words)
    cuts=[]; cum=0
    for i in range(len(items)-1):
        cum+=words[i]; target=total*cum/tw
        cand=[c for c in sil if c not in cuts and (not cuts or c[1]>cuts[-1][2])]
        best=max(cand, key=lambda c: c[0]-1.2*abs((c[1]+c[2])/2-target)/max(1,total/60))
        cuts.append(best)
    for i,(k,_) in enumerate(items[:-1]):
        if k in SHIFT:
            later=[c for c in sil if c[1]>cuts[i][2]]
            cuts[i]=later[SHIFT[k]-1]
    bounds=[0]+[(s+e)/2 for _,s,e in cuts]+[total]
    words=[len(v.split()) for _,v in items]; tw=sum(words)
    res={}
    for i,(k,_) in enumerate(items):
        a,b=bounds[i],bounds[i+1]
        out=f"audio/{k}.wav"
        subprocess.run(["ffmpeg","-y","-loglevel","error","-i",raw,"-ss",str(a),"-to",str(b),"-af",
          "silenceremove=start_periods=1:start_threshold=-50dB,areverse,silenceremove=start_periods=1:start_threshold=-50dB,areverse,loudnorm=I=-16:TP=-1.5","-ar","48000",out],check=True)
        d=ffdur(out); res[out]=round(d,2)
        print(f"{k:16s} {d:6.2f}s  words={words[i]:3d}  s/word={d/words[i]:.2f}  pauses-used={[round(c[0],2) for c in cuts]}" if i==0 else f"{k:16s} {d:6.2f}s  words={words[i]:3d}  s/word={d/words[i]:.2f}")
    return res
lines=list(cfg["lines"].items())
res={}
groups=[lines[0:7],lines[7:14],lines[14:]]
for gi,g in enumerate(groups):
    res.update(batch(cfg["voice"], g, f"n{gi}"))
for k,v in cfg["questions"].items():
    res.update(batch("Puck", [(k,v)], k))
json.dump(res, open("audio/durations.json","w"), indent=1)
print("total narration", round(sum(v for k,v in res.items() if "/n" in k),1))
