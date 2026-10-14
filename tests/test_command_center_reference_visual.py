from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_command_center_uses_reference_hud_sections():
    template = (ROOT / "templates" / "index.html").read_text(encoding="utf-8")
    css = (ROOT / "static" / "style.css").read_text(encoding="utf-8")
    js = (ROOT / "static" / "command.js").read_text(encoding="utf-8")

    assert 'class="command-recommendation"' in template
    assert 'id="command-hero-cards"' in template
    assert 'id="command-systems-summary"' in template
    assert 'id="command-pipeline-grid"' in template
    assert 'class="command-note command-note-integrity"' in template

    assert ".command-hero-grid" in css
    assert ".command-hero-card" in css
    assert ".command-recommendation" in css
    assert ".command-pipeline-grid" in css
    assert ".command-note-integrity" in css

    assert "commandHeroCardsEl" in js
    assert "renderHeroCards" in js
    assert "renderPipeline" in js
    assert "Needs you now" in js
    assert "Prepared for approval" in js


if __name__ == "__main__":
    test_command_center_uses_reference_hud_sections()
    print("PASS test_command_center_uses_reference_hud_sections")
