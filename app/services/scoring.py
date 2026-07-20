"""Explainable detection scoring (pure, no I/O — easy to unit-test).

Given facts about a candidate, one of our tracks, and per-tenant context (own
labels, whitelist, pirate watchlist), `score_candidate` returns a list of signals —
each with its raw value, human-readable RU label, and weighted contribution — plus a
total score and a band. Every weight and the thresholds version are recorded so a
finding can be debugged and later retuned (PLAN.md §6, §7 Ярус 2/3).

Weights are the PLAN.md §7 starting values. They live here as versioned constants;
moving them into an admin-editable DB table is a later milestone. Whenever they
change, bump THRESHOLDS_VERSION so old findings stay interpretable.
"""

import re
from dataclasses import dataclass, field
from datetime import date
from difflib import SequenceMatcher

from app.services import images
from app.services.normalize import normalize_title

THRESHOLDS_VERSION = "m2.1"

# --- Signal weights (PLAN.md §7 Ярус 3) ---
W_TITLE_EXACT = 40
W_TITLE_FUZZY = 30
W_SUFFIX = 15
W_ARTIST = 20
W_DURATION_RATIO = 30
W_FOREIGN_ISRC = 15
W_FOREIGN_LABEL = 20
W_PIRATE_LABEL = 25  # watchlist match or `\d+ Records DK` autolabel
W_COVER_STRONG = 25  # pHash hamming <= 8
W_COVER_WEAK = 15  # pHash hamming <= 14
W_DATE_DELTA = 5
W_UNLICENSED = 8  # weak: YouTube video not tracked by any partner + no distributor info

# --- Bands ---
BAND_HIGH = "high"
BAND_MID = "mid"
BAND_LOW = "low"
THRESHOLD_HIGH = 70
THRESHOLD_MID = 40

# Fuzzy title match cutoff (normalized titles are already alnum-folded).
FUZZY_CUTOFF = 0.82

# Speed presets used by pirates (slowed/sped). The candidate/original *duration*
# ratio clusters around these; ±2% tolerance turns metadata alone into near-proof.
STRETCH_PRESETS = (0.80, 0.90, 1.00, 1.10, 1.25, 1.33, 1.50)
STRETCH_TOLERANCE = 0.02

# DistroKid's auto-generated ℗ line, e.g. "13207436 Records DK".
_RECORDS_DK_RE = re.compile(r"^\d+\s+records\s+dk$")


@dataclass
class CandidateFacts:
    platform: str
    native_id: str
    title: str
    normalized_title: str
    uploader: str | None = None
    duration_ms: int | None = None
    isrc: str | None = None
    parsed_provider: str | None = None
    parsed_plabel: str | None = None
    published_at: date | None = None
    cover_phash: str | None = None
    cover_dhash: str | None = None
    is_variant: bool = False
    variant_label: str | None = None
    # YouTube only: contentDetails.licensedContent. NOT a Content ID claim status
    # (that's not exposed by the public API) — just "is this tracked by YouTube's
    # partner system at all". None for non-YouTube or when unknown.
    licensed_content: bool | None = None


@dataclass
class TrackFacts:
    id: int
    title: str
    normalized_title: str
    artist_names: list[str] = field(default_factory=list)
    isrc: str | None = None
    duration_ms: int | None = None
    release_date: date | None = None
    cover_phash: str | None = None
    cover_dhash: str | None = None


@dataclass
class DetectionContext:
    """Per-tenant knowledge, all values pre-normalized with `normalize_label`."""

    own_labels: set[str] = field(default_factory=set)
    whitelist_isrcs: set[str] = field(default_factory=set)
    whitelist_channels: set[str] = field(default_factory=set)
    whitelist_platform_ids: set[str] = field(default_factory=set)
    pirate_labels: set[str] = field(default_factory=set)
    pirate_channels: set[str] = field(default_factory=set)


@dataclass
class Signal:
    key: str
    label: str  # human-readable RU
    raw: object  # the observed value that produced this signal
    contribution: int
    weight: int

    def as_dict(self) -> dict:
        return {
            "key": self.key,
            "label": self.label,
            "raw": self.raw,
            "contribution": self.contribution,
            "weight": self.weight,
        }


@dataclass
class ScoreResult:
    score: int
    band: str
    signals: list[Signal]
    thresholds_version: str = THRESHOLDS_VERSION

    def signals_json(self) -> list[dict]:
        return [s.as_dict() for s in self.signals]


def normalize_label(value: str | None) -> str:
    """Fold a label/provider string for fuzzy comparison.

    Drops the ℗/© marks, "provided to youtube by" boilerplate and a leading year, then
    lowercases and collapses whitespace. Keeps digits (DistroKid ids are meaningful).
    """
    if not value:
        return ""
    s = value.lower()
    s = s.replace("℗", " ").replace("©", " ")
    s = re.sub(r"provided to youtube by", " ", s)
    s = re.sub(r"under exclusive licen[cs]e.*$", " ", s)
    s = re.sub(r"^\s*\d{4}\s+", " ", s)  # leading "2026 " copyright year
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def title_similarity(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    return SequenceMatcher(None, a, b).ratio()


def humanize_stretch(stretch: float | None) -> str:
    """" (замедлено на 25%)" / " (ускорено на 20%)" / "" — plain-language version
    of a Panako speed-ratio, same convention as the duration-ratio signal below
    (ratio > 1 = candidate is slower/longer than our original)."""
    if stretch is None:
        return ""
    pct = round((stretch - 1) * 100)
    if pct > 0:
        return f" (замедлено на {pct}%)"
    if pct < 0:
        return f" (ускорено на {-pct}%)"
    return " (тот же темп)"


def duration_stretch(cand_ms: int | None, orig_ms: int | None) -> tuple[float | None, float | None]:
    """Return (matched_preset, ratio) if candidate/original duration hits a speed
    preset within tolerance, else (None, ratio-or-None)."""
    if not cand_ms or not orig_ms:
        return None, None
    ratio = cand_ms / orig_ms
    for preset in STRETCH_PRESETS:
        if abs(ratio - preset) <= STRETCH_TOLERANCE:
            return preset, ratio
    return None, ratio


def _artist_matches(uploader: str | None, artist_names: list[str]) -> str | None:
    """Return the matched artist name if the uploader/channel credits one of ours."""
    up = normalize_title(uploader or "")
    if not up:
        return None
    up_tokens = set(up.split())
    for name in artist_names:
        n = normalize_title(name)
        if not n:
            continue
        n_tokens = set(n.split())
        if n and (n in up or up in n or n_tokens <= up_tokens):
            return name
    return None


def whitelist_gate(cand: CandidateFacts, track: TrackFacts, ctx: DetectionContext) -> str | None:
    """If the candidate is provably ours/allowed, return a human reason; else None.

    ISRC is the strongest gate: our own delivery carries our ISRC (PLAN.md §7).
    """
    if cand.isrc:
        ci = cand.isrc.upper()
        if track.isrc and ci == track.isrc.upper():
            return f"ISRC совпадает с нашим ({track.isrc}) — это наша доставка"
        if ci.lower() in ctx.whitelist_isrcs:
            return f"ISRC {cand.isrc} в белом списке"
    if cand.uploader and normalize_label(cand.uploader) in ctx.whitelist_channels:
        return f"Канал «{cand.uploader}» в белом списке"
    pid = normalize_label(cand.native_id)
    if pid and pid in ctx.whitelist_platform_ids:
        return "Идентификатор релиза в белом списке"
    return None


def _label_signal(cand: CandidateFacts, ctx: DetectionContext) -> Signal | None:
    """Provenance signal from provider/℗ label. Pirate watchlist / DistroKid-autolabel
    outrank a merely-foreign label."""
    provider = normalize_label(cand.parsed_provider)
    plabel = normalize_label(cand.parsed_plabel)
    candidates = [c for c in (provider, plabel) if c]
    if not candidates:
        return None

    # First, if the label is one of our own labels, we do not flag it.
    if ctx.own_labels and any(c in ctx.own_labels for c in candidates):
        return None

    # Strongest: known pirate entity or the DistroKid `\d+ Records DK` autolabel.
    for c in candidates:
        if c in ctx.pirate_labels or _RECORDS_DK_RE.match(c) or "distrokid" in c:
            shown = cand.parsed_plabel or cand.parsed_provider
            return Signal(
                "pirate_label",
                f"Лейбл/дистрибьютор из списка пиратов: «{shown}»",
                shown,
                W_PIRATE_LABEL,
                W_PIRATE_LABEL,
            )

    # Otherwise: foreign label only meaningful if the tenant declared its own labels.
    if ctx.own_labels:
        shown = cand.parsed_provider or cand.parsed_plabel
        return Signal(
            "foreign_label",
            f"Лейбл/дистрибьютор не из наших: «{shown}»",
            shown,
            W_FOREIGN_LABEL,
            W_FOREIGN_LABEL,
        )
    return None


def score_candidate(
    cand: CandidateFacts, track: TrackFacts, ctx: DetectionContext, audio_match: dict | None = None
) -> ScoreResult:
    """Score a candidate against one of our tracks. Assumes the whitelist gate has
    already been checked by the caller (a gated candidate should not be scored)."""
    signals: list[Signal] = []

    def add(key: str, label: str, raw: object, weight: int) -> None:
        signals.append(Signal(key, label, raw, weight, weight))

    # --- Title ---
    sim = title_similarity(cand.normalized_title, track.normalized_title)
    if cand.normalized_title and cand.normalized_title == track.normalized_title:
        add("title_exact", "Точное совпадение названия", cand.title, W_TITLE_EXACT)
    elif sim >= FUZZY_CUTOFF:
        add("title_fuzzy", f"Похожее название ({int(sim * 100)}%)", cand.title, W_TITLE_FUZZY)

    # --- Pirate suffix (slowed/sped/nightcore/...) ---
    if cand.is_variant:
        add("suffix", f"Пиратский суффикс: {cand.variant_label}", cand.variant_label, W_SUFFIX)

    # --- Artist match ---
    matched_artist = _artist_matches(cand.uploader, track.artist_names)
    if matched_artist:
        add("artist", f"Совпадает артист: {matched_artist}", cand.uploader, W_ARTIST)

    # --- Duration ratio on a stretch preset ---
    preset, ratio = duration_stretch(cand.duration_ms, track.duration_ms)
    if preset is not None:
        pct = int(round((ratio - 1) * 100))
        if pct > 0:
            human = f"замедлено на {pct}% (длительность ×{ratio:.2f})"
        elif pct < 0:
            human = f"ускорено на {-pct}% (длительность ×{ratio:.2f})"
        else:
            human = "та же длительность (вероятная копия)"
        add("duration_ratio", human, round(ratio, 3), W_DURATION_RATIO)

    # --- ISRC: same metadata, different ISRC = strong pirate signal ---
    if cand.isrc and track.isrc and cand.isrc.upper() != track.isrc.upper() and sim >= FUZZY_CUTOFF:
        add("foreign_isrc", f"Другой ISRC при том же треке: {cand.isrc}", cand.isrc, W_FOREIGN_ISRC)

    # --- Provider / ℗ label ---
    label_sig = _label_signal(cand, ctx)
    if label_sig:
        signals.append(label_sig)

    # --- Weak signal: YouTube video outside any partner tracking, no distributor
    # info either — looks like an informal reupload, not an official delivery.
    # `licensed_content=True` is NOT treated as a legitimacy signal in the other
    # direction: pirate uploads routed through DistroKid-style distributors are
    # also licensed_content=True (the distributor's own Content ID claim), so it
    # doesn't discriminate — only the *absence* of any tracking is informative.
    title_matched = cand.normalized_title == track.normalized_title or sim >= FUZZY_CUTOFF
    if (
        cand.platform == "youtube"
        and cand.licensed_content is False
        and title_matched
        and not cand.parsed_provider
        and not cand.parsed_plabel
    ):
        add(
            "unlicensed",
            "Видео вне партнёрской системы YouTube и без указания дистрибьютора",
            False,
            W_UNLICENSED,
        )

    # --- Cover pHash ---
    if cand.cover_phash and track.cover_phash:
        try:
            dist = images.hamming_hex(cand.cover_phash, track.cover_phash)
        except ValueError:
            dist = None
        if dist is not None and dist <= 8:
            add("cover", f"Обложка почти идентична (расхождение {dist} бит)", dist, W_COVER_STRONG)
        elif dist is not None and dist <= 14:
            add("cover", f"Обложка похожа (расхождение {dist} бит)", dist, W_COVER_WEAK)

    # --- Date delta (weighted signal, not a filter) ---
    if cand.published_at and track.release_date and cand.published_at > track.release_date:
        delta_days = (cand.published_at - track.release_date).days
        add("date_delta", f"Выпущено на {delta_days} дн. позже оригинала", delta_days, W_DATE_DELTA)

    # --- Audio Match (Panako) ---
    if audio_match is not None:
        if audio_match.get("matched"):
            stretch = audio_match.get("true_stretch")
            human = f"Звук совпадает по отпечатку{humanize_stretch(stretch)}"
            add("audio_match", human, stretch, 40)
        else:
            add("audio_no_match", "Звук не совпадает с оригиналом", None, -50)

    score = sum(s.contribution for s in signals)
    if score >= THRESHOLD_HIGH:
        band = BAND_HIGH
    elif score >= THRESHOLD_MID:
        band = BAND_MID
    else:
        band = BAND_LOW
    return ScoreResult(score=score, band=band, signals=signals)
