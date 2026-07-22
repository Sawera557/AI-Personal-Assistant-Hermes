from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_voice_overlay_uses_reference_jarvis_stage():
    template = (ROOT / "templates" / "index.html").read_text(encoding="utf-8")
    css = (ROOT / "static" / "style.css").read_text(encoding="utf-8")
    js = (ROOT / "static" / "voice.js").read_text(encoding="utf-8")

    assert "J.A.R.V.I.S." in template
    assert 'class="call-orb-radar"' in template
    assert 'id="call-transcript"' in template
    assert 'id="call-readout-stt"' in template
    assert 'id="call-readout-uplink"' in template
    assert ".call-orb-radar" in css
    assert ".call-orb-core" in css
    assert ".call-transcript" in css
    assert "--call-color" in css

    orb_block = css.rsplit(".call-orb-wrap .call-orb {", 1)[1].split("}", 1)[0]
    spokes_block = css.rsplit(".call-orb-spokes {", 1)[1].split("}", 1)[0]

    assert "@keyframes hud-spokes-spin" in css
    assert "animation: hud-spokes-spin" in spokes_block
    assert "transform: translate(-50%, -50%) rotate(360deg)" in css
    assert "transform: none;" in orb_block
    assert "animation: none !important;" in orb_block
    assert "callTranscript" in js


if __name__ == "__main__":
    test_voice_overlay_uses_reference_jarvis_stage()
    print("PASS test_voice_overlay_uses_reference_jarvis_stage")
