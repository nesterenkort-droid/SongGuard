"""Detection orchestration: scan → global candidates → scored findings.

This ties the scanners, the scorer, and the DB together:

* `build_context` loads a tenant's own-labels, whitelist and pirate watchlist.
* `upsert_candidate` stores a scanner's RawCandidate as a single global row.
* `ingest_candidates` scores each candidate against the artist's tracks (respecting
  the whitelist gate) and writes/refreshes findings — never clobbering a human
  decision.
* `run_scan_for_artist` is the live entrypoint (Tier 0 DSP diff + optional search).
* `transition` / `add_whitelist_from_finding` drive the dashboard actions.

Findings are per-track, candidates are global (PLAN.md §6): a candidate scored against
several of our tracks yields several findings, but only one candidate row.
"""

import os
import tempfile
from dataclasses import dataclass
from datetime import UTC, date, datetime

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import (
    PE_APPLE_LABEL,
    PE_DISTRIBUTOR,
    PE_SPOTIFY_LABEL,
    PE_YT_CHANNEL,
    PLATFORM_ITUNES,
    PLATFORM_SPOTIFY,
    RESOLVED_STATUSES,
    STATUS_CONFIRMED,
    STATUS_DETECTED,
    STATUS_DISMISSED,
    STATUS_PENDING_REVIEW,
    STATUS_REMIX_REVIEW,
    STATUS_TOLERATED,
    WL_CHANNEL,
    WL_ISRC,
    WL_OWN_LABEL,
    WL_PLATFORM_ID,
    WL_SCOPE_ARTIST,
    Artist,
    Finding,
    FindingEvent,
    PirateEntity,
    PlatformCandidate,
    ScanJob,
    Track,
    WhitelistEntry,
)
from app.scanners import itunes_scan, spotify_scan, youtube_scan
from app.scanners.base import RawCandidate
from app.services import audit, images, notify, takedown
from app.services.ai_judge import evaluate_candidate
from app.services.audio_downloader import download_preview_audio, download_youtube_audio
from app.services.normalize import detect_variant, normalize_title
from app.services.panako import ORIGINALS_DIR, query_candidate
from app.services.scoring import (
    BAND_LOW,
    CandidateFacts,
    DetectionContext,
    ScoreResult,
    TrackFacts,
    normalize_label,
    score_candidate,
    whitelist_gate,
)

# Actions accepted by `transition`, mapped to the target status.
ACTION_STATUS = {
    "confirm": STATUS_CONFIRMED,
    "dismiss": STATUS_DISMISSED,
    "tolerate": STATUS_TOLERATED,
    "reopen": STATUS_PENDING_REVIEW,
}


# --- Context ------------------------------------------------------------------

async def build_context(session: AsyncSession, artist: Artist) -> DetectionContext:
    """Load own-labels, whitelist and pirate watchlist relevant to this artist."""
    ctx = DetectionContext()

    wl_rows = await session.scalars(
        select(WhitelistEntry).where(
            (WhitelistEntry.artist_id == artist.id) | (WhitelistEntry.artist_id.is_(None))
        )
    )
    for w in wl_rows:
        norm = w.normalized_value or normalize_label(w.value)
        if w.entry_type == WL_OWN_LABEL:
            ctx.own_labels.add(norm)
        elif w.entry_type == WL_ISRC:
            ctx.whitelist_isrcs.add(w.value.lower())
        elif w.entry_type == WL_CHANNEL:
            ctx.whitelist_channels.add(norm)
        elif w.entry_type == WL_PLATFORM_ID:
            ctx.whitelist_platform_ids.add(norm)

    pe_rows = await session.scalars(select(PirateEntity))
    for p in pe_rows:
        if p.entity_type == PE_YT_CHANNEL:
            ctx.pirate_channels.add(p.normalized_value)
        else:
            ctx.pirate_labels.add(p.normalized_value)
    return ctx


def _artist_names(artist: Artist, track: Track) -> list[str]:
    names = {artist.name}
    if track.credit:
        # Split "A, B & C" into individual names.
        for part in track.credit.replace("&", ",").split(","):
            part = part.strip()
            if part:
                names.add(part)
    return list(names)


def _track_facts(artist: Artist, track: Track) -> TrackFacts:
    return TrackFacts(
        id=track.id,
        title=track.title,
        normalized_title=track.normalized_title,
        artist_names=_artist_names(artist, track),
        isrc=track.isrc,
        duration_ms=track.duration_ms,
        release_date=track.release_date,
        cover_phash=track.cover_phash,
        cover_dhash=track.cover_dhash,
    )


def _candidate_facts(cand: PlatformCandidate) -> CandidateFacts:
    is_variant, variant_label = detect_variant(cand.title)
    published = cand.published_at
    if isinstance(published, datetime):
        published = published.date()
    return CandidateFacts(
        platform=cand.platform,
        native_id=cand.native_id,
        title=cand.title,
        normalized_title=cand.normalized_title,
        uploader=cand.uploader,
        duration_ms=cand.duration_ms,
        isrc=cand.isrc,
        parsed_provider=cand.parsed_provider,
        parsed_plabel=cand.parsed_plabel,
        published_at=published,
        cover_phash=cand.cover_phash,
        cover_dhash=cand.cover_dhash,
        is_variant=is_variant,
        variant_label=variant_label,
        licensed_content=cand.licensed_content,
    )


# --- Candidate upsert ---------------------------------------------------------

def _as_dt(value) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=UTC)
    return None


async def upsert_candidate(session: AsyncSession, raw: RawCandidate) -> PlatformCandidate:
    """Insert or refresh the single global row for (platform, native_id)."""
    cand = await session.scalar(
        select(PlatformCandidate).where(
            PlatformCandidate.platform == raw.platform,
            PlatformCandidate.native_id == raw.native_id,
        )
    )
    if cand is None:
        cand = PlatformCandidate(platform=raw.platform, native_id=raw.native_id)
        session.add(cand)

    cand.title = raw.title
    cand.normalized_title = normalize_title(raw.title)
    cand.url = raw.url or cand.url
    cand.uploader = raw.uploader or cand.uploader
    cand.description_raw = raw.description_raw or cand.description_raw
    cand.parsed_provider = raw.parsed_provider or cand.parsed_provider
    cand.parsed_plabel = raw.parsed_plabel or cand.parsed_plabel
    cand.isrc = raw.isrc or cand.isrc
    cand.published_at = _as_dt(raw.published_at) or cand.published_at
    cand.duration_ms = raw.duration_ms or cand.duration_ms
    cand.thumb_url = raw.thumb_url or cand.thumb_url
    if raw.licensed_content is not None:
        cand.licensed_content = raw.licensed_content
    if raw.raw_json:
        cand.raw_json = raw.raw_json
    cand.last_seen = datetime.now(UTC)
    await session.flush()
    return cand


async def _attach_candidate_cover(
    client: httpx.AsyncClient, cand: PlatformCandidate, cover_url: str
) -> None:
    """Best-effort: hash the candidate cover so the pHash signal can fire."""
    try:
        content = await images.fetch(client, cover_url)
        cand.cover_phash, cand.cover_dhash = images.hash_bytes(content)
    except Exception:  # noqa: BLE001
        pass


# --- Scoring + persistence ----------------------------------------------------

@dataclass
class ScanSummary:
    artist_id: int
    artist_name: str
    scanned: int = 0
    new_candidates: int = 0
    findings_created: int = 0
    findings_updated: int = 0
    high: int = 0
    mid: int = 0

    def as_dict(self) -> dict:
        return self.__dict__


def _best_results(
    cand_facts: CandidateFacts, tracks: list[tuple[Artist, Track]], ctx: DetectionContext
) -> list[tuple[Track, ScoreResult]]:
    """Score a candidate against every track; keep non-low, non-gated matches."""
    out: list[tuple[Track, ScoreResult]] = []
    for artist, track in tracks:
        tf = _track_facts(artist, track)
        if whitelist_gate(cand_facts, tf, ctx):
            continue
        result = score_candidate(cand_facts, tf, ctx)
        if result.band != BAND_LOW:
            out.append((track, result))
    # Strongest match first.
    out.sort(key=lambda r: r[1].score, reverse=True)
    return out


async def _persist_finding(
    session: AsyncSession,
    artist: Artist,
    cand: PlatformCandidate,
    track: Track,
    result: ScoreResult,
    summary: ScanSummary,
    audio_match: dict | None = None,
    llm: dict | None = None,
) -> None:
    finding = await session.scalar(
        select(Finding).where(
            Finding.candidate_id == cand.id, Finding.track_id == track.id
        )
    )
    is_new = finding is None
    if finding is None:
        finding = Finding(candidate_id=cand.id, track_id=track.id, status=STATUS_DETECTED)
        session.add(finding)
        summary.findings_created += 1

        # Кросс-платформенный триггер: при нахождении пиратки на Spotify/Apple автоматически
        # ставим задачу на YouTube-сканирование для этого трека (приоритет 50)
        if cand.platform in ["spotify", "itunes"] and result.band == "high":
            existing_job = await session.scalar(
                select(ScanJob).where(
                    ScanJob.track_id == track.id,
                    ScanJob.platform == "youtube",
                )
            )
            if not existing_job:
                session.add(
                    ScanJob(
                        track_id=track.id,
                        platform="youtube",
                        priority=50,
                        status="pending",
                    )
                )
            else:
                existing_job.priority = max(existing_job.priority, 50)
                if existing_job.status in ["completed", "failed"]:
                    existing_job.status = "pending"
                    existing_job.outcome = None
    else:
        summary.findings_updated += 1
    # Refresh the score/signals, but never override a human decision.
    finding.score = result.score
    finding.band = result.band
    finding.signals = result.signals_json()
    finding.thresholds_version = result.thresholds_version
    finding.audio_match = audio_match
    finding.llm = llm

    if finding.status not in RESOLVED_STATUSES:
        if llm:
            verdict = llm.get("verdict")
            if verdict == "remix":
                finding.status = STATUS_REMIX_REVIEW
            elif verdict == "pirate":
                finding.status = STATUS_PENDING_REVIEW
            elif verdict == "safe":
                finding.status = STATUS_DISMISSED
            else:
                finding.status = STATUS_PENDING_REVIEW
        else:
            if result.band == "high":
                finding.status = STATUS_PENDING_REVIEW
            else:
                finding.status = STATUS_DETECTED
    await session.flush()

    if is_new:
        await notify.enqueue_finding_notifications(session, artist, finding)


async def ingest_candidates(
    session: AsyncSession,
    artist: Artist,
    raws: list[RawCandidate],
    *,
    ctx: DetectionContext | None = None,
    download_covers: bool = True,
) -> ScanSummary:
    """Upsert raw candidates and (re)score them against the artist's tracks.

    Pure of the network except optional cover downloads, so tests drive it directly
    with captured RawCandidates.
    """
    if ctx is None:
        ctx = await build_context(session, artist)
    summary = ScanSummary(artist_id=artist.id, artist_name=artist.name)

    # The artist's tracks (candidate is scored against these).
    tracks = list(await session.scalars(select(Track).where(Track.primary_artist_id == artist.id)))
    track_pairs = [(artist, t) for t in tracks]

    client = httpx.AsyncClient() if download_covers else None
    try:
        for raw in raws:
            summary.scanned += 1
            existed = await session.scalar(
                select(PlatformCandidate.id).where(
                    PlatformCandidate.platform == raw.platform,
                    PlatformCandidate.native_id == raw.native_id,
                )
            )
            cand = await upsert_candidate(session, raw)
            if existed is None:
                summary.new_candidates += 1

            cand_facts = _candidate_facts(cand)
            # Pre-filter: only fetch a cover for candidates that already relate to a
            # track by title, to keep network bounded.
            if client is not None and raw.cover_url and not cand.cover_phash:
                relates = any(
                    score_candidate(cand_facts, _track_facts(a, t), ctx).band != BAND_LOW
                    for a, t in track_pairs
                )
                if relates:
                    await _attach_candidate_cover(client, cand, raw.cover_url)
                    cand_facts = _candidate_facts(cand)

            for track, result in _best_results(cand_facts, track_pairs, ctx):
                # 1. Check if we should/can run audio matching (Panako)
                audio_match_dict = None
                original_exists = os.path.exists(
                    os.path.join(ORIGINALS_DIR, f"{track.id}_1.00.wav")
                )

                if original_exists and result.band in ["mid", "high"]:
                    temp_wav = os.path.join(
                        tempfile.gettempdir(), f"query_{track.id}_{cand.id}.wav"
                    )
                    download_success = False

                    if cand.platform == "youtube" and cand.url:
                        download_success = await download_youtube_audio(cand.url, temp_wav)
                    elif cand.platform == "itunes" and cand.raw_json:
                        preview_url = cand.raw_json.get("preview_url")
                        if preview_url:
                            download_success = await download_preview_audio(preview_url, temp_wav)

                    if download_success and os.path.exists(temp_wav):
                        match_res = await query_candidate(temp_wav)
                        try:
                            os.remove(temp_wav)
                        except Exception:  # noqa: BLE001
                            pass

                        if match_res.matched and match_res.track_id == track.id:
                            audio_match_dict = {
                                "matched": True,
                                "true_stretch": match_res.true_stretch,
                                "score": match_res.score,
                            }
                        else:
                            audio_match_dict = {
                                "matched": False,
                            }

                # If audio check ran, re-score with audio match info
                if audio_match_dict is not None:
                    tf = _track_facts(artist, track)
                    result = score_candidate(cand_facts, tf, ctx, audio_match=audio_match_dict)

                # 2. Run LLM Judge if score is in the mid range (40-69)
                llm_dict = None
                if result.band == "mid":
                    verdict = await evaluate_candidate(
                        track_title=track.title,
                        track_artist=artist.name,
                        candidate_title=cand.title,
                        candidate_uploader=cand.uploader or "Unknown",
                        candidate_description=cand.description_raw or "",
                        candidate_platform=cand.platform,
                        duration_diff_sec=(
                            abs(cand.duration_ms - track.duration_ms) / 1000.0
                            if cand.duration_ms is not None and track.duration_ms is not None
                            else None
                        ),
                        score_before_ai=result.score,
                        audio_matched=bool(audio_match_dict and audio_match_dict.get("matched")),
                        audio_true_stretch=(
                            audio_match_dict.get("true_stretch") if audio_match_dict else None
                        ),
                    )
                    llm_dict = {
                        "verdict": verdict.verdict,
                        "confidence": verdict.confidence,
                        "reasoning_ru": verdict.reasoning_ru,
                        "cost_usd": verdict.cost_usd,
                    }

                if result.band == "high":
                    summary.high += 1
                elif result.band == "mid":
                    summary.mid += 1

                await _persist_finding(
                    session, artist, cand, track, result, summary,
                    audio_match=audio_match_dict, llm=llm_dict
                )
    finally:
        if client is not None:
            await client.aclose()

    return summary


async def get_finding_context(
    session: AsyncSession, finding_id: int
) -> tuple[Finding, PlatformCandidate, Track, Artist] | None:
    """Load a finding with its candidate/track/artist — the join both the dashboard
    and the bot card renderer need."""
    row = (
        await session.execute(
            select(Finding, PlatformCandidate, Track, Artist)
            .join(PlatformCandidate, Finding.candidate_id == PlatformCandidate.id)
            .join(Track, Finding.track_id == Track.id)
            .join(Artist, Track.primary_artist_id == Artist.id)
            .where(Finding.id == finding_id)
        )
    ).first()
    return tuple(row) if row else None


# --- Live scan ----------------------------------------------------------------

async def run_scan_for_artist(
    session: AsyncSession,
    artist: Artist,
    *,
    actor_user_id: int | None = None,
    do_search: bool = False,
) -> ScanSummary:
    """Live Tier 0 DSP diff (Spotify + Apple) for one artist, plus optional search."""
    tracks = list(await session.scalars(select(Track).where(Track.primary_artist_id == artist.id)))
    known_spotify = {t.spotify_track_id for t in tracks if t.spotify_track_id}
    known_apple = {str(t.apple_track_id) for t in tracks if t.apple_track_id}

    raws: list[RawCandidate] = []
    if artist.spotify_artist_id:
        raws.extend(await spotify_scan.scan_artist_page(artist.spotify_artist_id, known_spotify))
    if artist.apple_artist_id:
        apple_raws = await itunes_scan.scan_artist_page(artist.apple_artist_id, known_apple)
        await _enrich_apple_labels(apple_raws)
        raws.extend(apple_raws)

    if do_search:
        for t in tracks:
            q = f"{t.title} {artist.name}"
            if artist.spotify_artist_id:
                raws.extend(await spotify_scan.search_tracks(q))
            raws.extend(await itunes_scan.search_tracks(q))
            if settings.youtube_api_key:
                raws.extend(await youtube_scan.search_tracks(q))

    ctx = await build_context(session, artist)
    summary = await ingest_candidates(session, artist, raws, ctx=ctx)

    now = datetime.now(UTC)
    for t in tracks:
        if artist.spotify_artist_id:
            t.last_scanned_spotify = now
        if artist.apple_artist_id:
            t.last_scanned_apple = now
        if settings.youtube_api_key:
            t.last_scanned_youtube = now

    await audit.log(
        session,
        actor_user_id=actor_user_id,
        action="scan.artist",
        entity_type="artist",
        entity_id=artist.id,
        summary=(
            f"Скан «{artist.name}»: кандидатов {summary.scanned} "
            f"(новых {summary.new_candidates}), находок +{summary.findings_created}, "
            f"high={summary.high}, mid={summary.mid}"
        ),
        data=summary.as_dict(),
    )
    await session.commit()
    return summary


async def _enrich_apple_labels(raws: list[RawCandidate]) -> None:
    """Scrape the Apple ℗ label for candidates that carry an album page URL."""
    from app.services import apple_label

    async with httpx.AsyncClient() as client:
        for raw in raws:
            url = (raw.raw_json or {}).get("collection_view_url")
            if url:
                label = await apple_label.fetch_label(client, url)
                if label:
                    raw.parsed_plabel = label


# --- Lifecycle actions --------------------------------------------------------

async def transition(
    session: AsyncSession,
    finding: Finding,
    action: str,
    *,
    actor_user_id: int | None = None,
    note: str | None = None,
) -> None:
    """Move a finding to a new status, writing a FindingEvent (full audit)."""
    if action not in ACTION_STATUS:
        raise ValueError(f"Неизвестное действие: {action}")
    new_status = ACTION_STATUS[action]
    old_status = finding.status
    finding.status = new_status
    session.add(
        FindingEvent(
            finding_id=finding.id,
            actor_user_id=actor_user_id,
            action=action,
            from_status=old_status,
            to_status=new_status,
            note=note,
        )
    )
    if new_status == STATUS_CONFIRMED:
        await _register_pirate_entity(session, finding, actor_user_id)
        await takedown.capture_evidence(session, finding)


async def _register_pirate_entity(
    session: AsyncSession, finding: Finding, actor_user_id: int | None
) -> None:
    """On confirm, remember the pirate's label/channel for the watchlist + recidivism."""
    cand = await session.get(PlatformCandidate, finding.candidate_id)
    if cand is None:
        return
    label = cand.parsed_plabel or cand.parsed_provider
    if not label:
        return
    norm = normalize_label(label)
    if not norm:
        return
    if cand.platform == PLATFORM_SPOTIFY:
        etype = PE_DISTRIBUTOR if "records dk" in norm else PE_SPOTIFY_LABEL
    elif cand.platform == PLATFORM_ITUNES:
        etype = PE_APPLE_LABEL
    else:
        etype = PE_YT_CHANNEL
    entity = await session.scalar(
        select(PirateEntity).where(
            PirateEntity.entity_type == etype, PirateEntity.normalized_value == norm
        )
    )
    if entity is None:
        entity = PirateEntity(
            entity_type=etype, value=label, normalized_value=norm,
            hit_count=1, created_by_user_id=actor_user_id,
        )
        session.add(entity)
    else:
        entity.hit_count += 1
        entity.last_seen = datetime.now(UTC)


async def add_whitelist_from_finding(
    session: AsyncSession,
    finding: Finding,
    entry_type: str,
    *,
    actor_user_id: int | None = None,
) -> WhitelistEntry | None:
    """Whitelist something from a finding in one click (channel / label / ISRC / id)."""
    cand = await session.get(PlatformCandidate, finding.candidate_id)
    track = await session.get(Track, finding.track_id)
    if cand is None or track is None:
        return None

    value = _whitelist_value(cand, entry_type)
    if not value:
        return None
    entry = WhitelistEntry(
        scope=WL_SCOPE_ARTIST,
        artist_id=track.primary_artist_id,
        entry_type=entry_type,
        value=value,
        normalized_value=normalize_label(value),
        note=f"из находки #{finding.id}",
        created_by_user_id=actor_user_id,
    )
    session.add(entry)
    # Whitelisting implies this candidate is not piracy → dismiss the finding.
    await transition(
        session, finding, "dismiss", actor_user_id=actor_user_id,
        note=f"внесено в белый список: {entry_type}={value}",
    )
    return entry


def _whitelist_value(cand: PlatformCandidate, entry_type: str) -> str | None:
    if entry_type == WL_ISRC:
        return cand.isrc
    if entry_type == WL_CHANNEL:
        return cand.uploader
    if entry_type == WL_OWN_LABEL:
        return cand.parsed_provider or cand.parsed_plabel
    if entry_type == WL_PLATFORM_ID:
        return cand.native_id
    return None
