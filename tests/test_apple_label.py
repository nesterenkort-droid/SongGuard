"""Apple Music ℗-label scraping (pure parse, no network)."""

from app.services.apple_label import parse_label_from_html


def test_parse_from_json_copyright():
    html = '...{"name":"HEAVENLY JUMPSTYLE (Slowed)","copyright":"℗ 2026 13207436 Records DK"}...'
    assert parse_label_from_html(html) == "℗ 2026 13207436 Records DK"


def test_parse_from_bare_pline():
    html = "<div><p>Some text</p><p>℗ 2025 0to8</p></div>"
    label = parse_label_from_html(html)
    assert label is not None
    assert "0to8" in label


def test_parse_none_when_absent():
    assert parse_label_from_html("<html><body>no label here</body></html>") is None
