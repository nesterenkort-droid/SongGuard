"""Scoring unit tests (pure, no DB, no network)."""

from datetime import date

from app.services.scoring import (
    CandidateFacts,
    DetectionContext,
    TrackFacts,
    duration_stretch,
    normalize_label,
    score_candidate,
    whitelist_gate,
)

ORIGINAL = TrackFacts(
    id=1,
    title="HEAVENLY JUMPSTYLE",
    normalized_title="heavenly jumpstyle",
    artist_names=["TWXNY", "Sxilwix", "Innxcence"],
    isrc="QZHN52501234",
    duration_ms=114462,
    release_date=date(2025, 11, 28),
)


def _pirate(**over) -> CandidateFacts:
    base = dict(
        platform="spotify",
        native_id="pirate123",
        title="HEAVENLY JUMPSTYLE (Slowed)",
        normalized_title="heavenly jumpstyle",
        uploader="TWXNY",
        duration_ms=143078,  # 1.25x → slowed
        isrc="DEXX12600001",  # different label's ISRC
        parsed_provider="DistroKid",
        parsed_plabel="13207436 Records DK",
        published_at=date(2026, 7, 13),
        is_variant=True,
        variant_label="Slowed",
    )
    base.update(over)
    return CandidateFacts(**base)


def _ctx(**over) -> DetectionContext:
    base = dict(own_labels={normalize_label("0to8")})
    base.update(over)
    return DetectionContext(**base)


def test_golden_pirate_scores_high():
    result = score_candidate(_pirate(), ORIGINAL, _ctx())
    keys = {s.key for s in result.signals}
    assert result.band == "high"
    assert result.score >= 70
    # The signals that make this case a slam dunk:
    assert "title_exact" in keys
    assert "suffix" in keys
    assert "artist" in keys
    assert "duration_ratio" in keys
    assert "pirate_label" in keys  # `\d+ Records DK` autolabel
    assert "foreign_isrc" in keys


def test_isrc_match_is_whitelisted():
    # Same ISRC as ours = our own delivery → gated, never scored.
    cand = _pirate(isrc=ORIGINAL.isrc)
    reason = whitelist_gate(cand, ORIGINAL, _ctx())
    assert reason is not None
    assert "ISRC" in reason


def test_foreign_label_needs_declared_own_labels():
    cand = _pirate(parsed_provider="Some Other Label", parsed_plabel=None)
    # With no declared own-labels we can't call a label "foreign".
    with_none = score_candidate(cand, ORIGINAL, _ctx(own_labels=set()))
    assert "foreign_label" not in {s.key for s in with_none.signals}
    # With own-labels declared, an outside label is a signal.
    with_own = score_candidate(cand, ORIGINAL, _ctx())
    assert "foreign_label" in {s.key for s in with_own.signals}


def test_own_label_not_flagged():
    cand = _pirate(parsed_provider="0to8", parsed_plabel="℗ 2025 0to8 under exclusive license")
    result = score_candidate(cand, ORIGINAL, _ctx())
    keys = {s.key for s in result.signals}
    assert "foreign_label" not in keys
    assert "pirate_label" not in keys


def test_duration_stretch_presets():
    preset, ratio = duration_stretch(143078, 114462)
    assert preset == 1.25
    assert round(ratio, 2) == 1.25
    # Unrelated duration → no preset.
    preset2, _ = duration_stretch(200000, 114462)
    assert preset2 is None


def test_fuzzy_title_below_exact():
    cand = _pirate(normalized_title="heavenly jumpstyl", is_variant=False, variant_label=None)
    result = score_candidate(cand, ORIGINAL, _ctx())
    keys = {s.key for s in result.signals}
    assert "title_fuzzy" in keys
    assert "title_exact" not in keys


def test_unrelated_track_scores_low():
    cand = _pirate(
        title="COMPLETELY DIFFERENT SONG",
        normalized_title="completely different song",
        uploader="Some Stranger",
        duration_ms=200000,
        parsed_provider=None,
        parsed_plabel=None,
        isrc=None,
        is_variant=False,
        variant_label=None,
    )
    result = score_candidate(cand, ORIGINAL, _ctx())
    assert result.band == "low"
    assert result.score < 40
