"""Finding card rendering (pure, no aiogram/network)."""

from datetime import UTC, datetime

from app.bot.cards import (
    build_finding_buttons,
    build_finding_text,
    callback_data,
    parse_callback_data,
)
from app.models import Finding, PlatformCandidate, Track


def _finding() -> tuple[Finding, PlatformCandidate, Track]:
    cand = PlatformCandidate(
        id=1, platform="spotify", native_id="p1",
        title="HEAVENLY JUMPSTYLE (Slowed)", normalized_title="heavenly jumpstyle",
        uploader="TWXNY", parsed_plabel="℗ 2026 13207436 Records DK",
        published_at=datetime(2026, 7, 13, tzinfo=UTC),
    )
    track = Track(id=2, title="HEAVENLY JUMPSTYLE", normalized_title="heavenly jumpstyle")
    finding = Finding(
        id=42, candidate_id=1, track_id=2, score=150, band="high", status="detected",
        signals=[{"key": "title_exact", "label": "Точное совпадение названия", "contribution": 40}],
    )
    return finding, cand, track


def test_card_text_has_key_facts():
    finding, cand, track = _finding()
    text = build_finding_text(finding, cand, track, "TWXNY")
    assert "HEAVENLY JUMPSTYLE (Slowed)" in text
    assert "Spotify" in text
    assert "TWXNY" in text
    assert "13207436 Records DK" in text
    assert "150" in text
    assert "Точное совпадение названия" in text


def test_card_escapes_html():
    finding, cand, track = _finding()
    cand.title = "A <script>bad</script> & B"
    text = build_finding_text(finding, cand, track, "TWXNY")
    assert "<script>" not in text
    assert "&lt;script&gt;" in text


def test_buttons_have_confirm_dismiss_tolerate_and_site_link():
    finding, _cand, _track = _finding()
    rows = build_finding_buttons(finding)
    flat_texts = {b.text for row in rows for b in row}
    assert {"✓ Пиратка", "✗ Ложное", "🕊 Разрешить", "🌐 На сайте"} <= flat_texts
    # Confirm/dismiss/tolerate carry callback_data; the site link is a URL button.
    site_button = next(b for row in rows for b in row if b.text == "🌐 На сайте")
    assert site_button.url is not None
    assert site_button.callback_data is None


def test_callback_data_roundtrip():
    data = callback_data("confirm", 42)
    assert parse_callback_data(data) == ("confirm", 42)
    assert parse_callback_data("garbage") is None
    assert parse_callback_data("f:confirm:notanumber") is None
