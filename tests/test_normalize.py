"""Title normalization + variant detection."""

from app.services.normalize import detect_variant, normalize_title


def test_variant_suffix_stripped_matches_original():
    assert normalize_title("HEAVENLY JUMPSTYLE") == normalize_title(
        "HEAVENLY JUMPSTYLE (Slowed)"
    )
    assert normalize_title("HEAVENLY JUMPSTYLE") == normalize_title(
        "HEAVENLY JUMPSTYLE (Ultra Slowed)"
    )


def test_detect_variant():
    is_v, label = detect_variant("HEAVENLY JUMPSTYLE (Ultra Slowed)")
    assert is_v is True
    assert label == "Ultra Slowed"

    is_v2, label2 = detect_variant("HEAVENLY JUMPSTYLE")
    assert is_v2 is False
    assert label2 is None


def test_homoglyph_folding():
    # Cyrillic 'у' (U+0443) should fold to Latin 'y'.
    assert normalize_title("jumpstyle") == normalize_title("jumpstуle")


def test_normalized_is_lowercase_alnum():
    assert normalize_title("  Héllo —  World!! ") == "hello world"
