"""Gemini TTS -> WAV. usage: python tts.py VOICE OUT.wav "text" [style]"""
import base64, json, os, sys, wave, time, urllib.request
KEY = next(l.split("=",1)[1].strip().strip('"') for l in open(os.path.join(os.path.dirname(__file__),"..",".env"),encoding="utf-8") if l.startswith("GEMINI_API_KEY="))
MODEL = os.environ.get("TTS_MODEL","gemini-3.8-flash-tts")
def tts(voice, out, text, style=""):
    prompt = (style + ": " if style else "") + text
    body = {"contents":[{"parts":[{"text":prompt}]}],
            "generationConfig":{"responseModalities":["AUDIO"],
              "speechConfig":{"voiceConfig":{"prebuiltVoiceConfig":{"voiceName":voice}}}}}
    req = urllib.request.Request(f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent",
        data=json.dumps(body).encode(), headers={"Content-Type":"application/json","x-goog-api-key":KEY})
    for i in range(4):
        try:
            d = json.load(urllib.request.urlopen(req, timeout=180)); break
        except Exception as e:
            print("retry", i, e, file=sys.stderr); time.sleep(5*(i+1))
    else: raise SystemExit("TTS failed")
    pcm = base64.b64decode(d["candidates"][0]["content"]["parts"][0]["inlineData"]["data"])
    with wave.open(out,"wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(24000); w.writeframes(pcm)
    return len(pcm)/48000
if __name__ == "__main__":
    print(round(tts(sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4] if len(sys.argv)>4 else ""),2), "s")
