"""Title normalization and variant detection.

`normalized_title` is the matching key used by M2 signals: it folds unicode (NFKC),
maps common Cyrillic/Latin homoglyphs, strips variant markers like "(Slowed)", and
reduces to lowercase alphanumerics. `detect_variant` separately flags whether a title
is a Slowed/Sped/Nightcore/etc. version so the artist's *own* variants can be excluded
from piracy flags later.
"""

import re
import unicodedata

# Compact confusables map (Cyrillic / other lookalikes → Latin). Not exhaustive, but
# covers the homoglyph tricks pirates typically use.
_CONFUSABLES = {
    "а": "a", "е": "e", "о": "o", "р": "p", "с": "c", "у": "y", "х": "x", "к": "k",
    "м": "m", "т": "t", "в": "b", "н": "h", "і": "i", "ј": "j", "ѕ": "s", "ԁ": "d",
    "ɡ": "g", "ן": "l", "ο": "o", "ε": "e", "α": "a", "ρ": "p", "τ": "t", "ν": "v",
}

# Ordered so more specific markers win (Ultra/Super Slowed before plain Slowed).
_VARIANT_PATTERNS: list[tuple[str, str]] = [
    (r"ultra\s*slowed", "Ultra Slowed"),
    (r"super\s*slowed", "Super Slowed"),
    (r"slowed(\s*\+?\s*reverb)?", "Slowed"),
    (r"sped\s*up", "Sped Up"),
    (r"night\s*core", "Nightcore"),
    (r"\b8d\b", "8D"),
    (r"bass\s*boost(ed)?", "Bass Boosted"),
    (r"reverb", "Reverb"),
    (r"instrumental", "Instrumental"),
    (r"tik\s*tok", "TikTok"),
    (r"nightcore", "Nightcore"),
    (r"remix", "Remix"),
    (r"\bedit\b", "Edit"),
    (r"version", "Version"),
]

# Bracketed groups: ( ... ) or [ ... ]
_BRACKET_RE = re.compile(r"[\(\[][^\)\]]*[\)\]]")


def detect_variant(title: str) -> tuple[bool, str | None]:
    low = title.lower()
    for pattern, label in _VARIANT_PATTERNS:
        if re.search(pattern, low):
            return True, label
    return False, None


def _strip_variant_markers(title: str) -> str:
    """Remove bracketed groups that contain a variant keyword, plus trailing dashes."""
    def _drop(match: re.Match) -> str:
        inner = match.group(0).lower()
        for pattern, _ in _VARIANT_PATTERNS:
            if re.search(pattern, inner):
                return " "
        return match.group(0)

    stripped = _BRACKET_RE.sub(_drop, title)
    # Also drop a trailing " - <variant>" tail.
    stripped = re.sub(
        r"\s*[-–—]\s*(" + "|".join(p for p, _ in _VARIANT_PATTERNS) + r").*$",
        "",
        stripped,
        flags=re.IGNORECASE,
    )
    return stripped


def normalize_title(title: str) -> str:
    core = _strip_variant_markers(title)
    # NFKD splits accents into combining marks; drop them so "é" → "e".
    decomposed = unicodedata.normalize("NFKD", core)
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    folded = stripped.lower()
    folded = "".join(_CONFUSABLES.get(ch, ch) for ch in folded)
    folded = re.sub(r"[^a-z0-9]+", " ", folded)
    return re.sub(r"\s+", " ", folded).strip()
