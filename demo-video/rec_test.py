import time, wave, record
r = record.Recorder("takes/rec_test.mkv"); r.start()
time.sleep(1.0)
w = wave.open("audio/n07_jarvis.wav"); p = r.p
o = p.open(format=p.get_format_from_width(w.getsampwidth()), channels=w.getnchannels(), rate=w.getframerate(), output=True)
o.write(w.readframes(w.getnframes())); o.close()
time.sleep(1.0); r.stop()
