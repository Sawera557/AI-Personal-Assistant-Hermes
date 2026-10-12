import json, base64, subprocess, urllib.request, sys, os
import tts as T
cfg=json.load(open("narration.json",encoding="utf-8"))
keys=(sys.argv[1:] if len(sys.argv)>1 else list(cfg["lines"]))
parts=[{"text":"Transcribe each clip verbatim. Reply as JSON object mapping clip id to transcript, nothing else."}]
for k in keys:
    mp3=subprocess.check_output(["ffmpeg","-loglevel","error","-i",f"audio/{k}.wav","-ac","1","-ar","16000","-b:a","32k","-f","mp3","-"])
    parts += [{"text":f"Clip {k}:"},{"inlineData":{"mimeType":"audio/mp3","data":base64.b64encode(mp3).decode()}}]
body={"contents":[{"parts":parts}],"generationConfig":{"responseMimeType":"application/json"}}
req=urllib.request.Request("https://generativelanguage.googleapis.com/v1beta/models/gemini-3.5-flash-lite:generateContent",data=json.dumps(body).encode(),headers={"Content-Type":"application/json","x-goog-api-key":T.KEY})
d=json.load(urllib.request.urlopen(req,timeout=300))
tr=json.loads(d["candidates"][0]["content"]["parts"][0]["text"])
allt={**cfg["lines"],**cfg.get("lines2",{}),**cfg["questions"]}
norm=lambda s:[w.strip(".,:;!?'\"").lower() for w in s.split()]
for k in keys:
    got=norm(tr.get(k,"")); exp=norm(allt[k])
    ok = got[:2]==exp[:2] and got[-2:]==exp[-2:]
    print(("OK  " if ok else "BAD ")+k, "|", " ".join(got[:5]), "...", " ".join(got[-4:]))
