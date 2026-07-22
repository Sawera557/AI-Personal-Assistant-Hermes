from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_homepage_uses_reference_orbit_layers():
    template = (ROOT / "templates" / "index.html").read_text(encoding="utf-8")
    css = (ROOT / "static" / "style.css").read_text(encoding="utf-8")

    assert 'class="home-reference-rings"' in template
    assert 'class="home-reference-brand"' in template
    assert 'class="home-reference-sweep"' in template
    assert ".home-view .home-chip" in css
    assert "border-radius: 8px" in css
    assert "background: #05080f" in css
    assert "radial-gradient(circle at 50% 50%, var(--hud-corehot)" in css


if __name__ == "__main__":
    test_homepage_uses_reference_orbit_layers()
    print("PASS test_homepage_uses_reference_orbit_layers")
