import json, subprocess, concurrent.futures as cf
from tts import tts
cfg = json.load(open("narration.json", encoding="utf-8"))
jobs = [(cfg["voice"], f"audio/{k}.wav", v) for k, v in cfg["lines"].items()]
jobs += [("Puck", f"audio/{k}.wav", v) for k, v in cfg["questions"].items()]
def run(j):
    v, out, text = j
    raw = out.replace(".wav", "_raw.wav")
    tts(v, raw, text)
    # trim leading/trailing silence, normalise loudness
    subprocess.run(["ffmpeg","-y","-loglevel","error","-i",raw,"-af",
        "silenceremove=start_periods=1:start_threshold=-50dB,areverse,silenceremove=start_periods=1:start_threshold=-50dB,areverse,loudnorm=I=-16:TP=-1.5",
        "-ar","48000",out], check=True)
    d = float(subprocess.check_output(["ffprobe","-v","error","-show_entries","format=duration","-of","csv=p=0",out]))
    return out, round(d,2)
with cf.ThreadPoolExecutor(6) as ex:
    res = dict(ex.map(run, jobs))
json.dump(res, open("audio/durations.json","w"), indent=1)
for k,v in res.items(): print(k, v)
print("total narration", round(sum(v for k,v in res.items() if "/n" in k),1))
