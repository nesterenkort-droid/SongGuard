"""End-to-end M2 verification.

Run inside the web container:
    docker compose run --rm --no-deps -v C:/SongGuard:/src -w /src web python -m scripts.verify_m2

Proves the detection core on the golden case (PLAN.md §14):
  * an original track ("HEAVENLY JUMPSTYLE", ℗ 0to8) is seeded with its own-label
  * the pirate ("… (Slowed)", ℗ 13207436 Records DK, DistroKid) is ingested as a
    global candidate and scored into a HIGH-band finding with an explainable breakdown
  * whitelisting the pirate channel dismisses it and a rescan does not re-flag
  * (best-effort) a LIVE iTunes Tier-0 artist-page diff is attempted and reported

The live step degrades gracefully if the container has no outbound internet.
"""

import asyncio
from datetime import date

from sqlalchemy import delete, func, select

from app.db import SessionLocal, engine
from app.models import (
    WL_CHANNEL,
    WL_OWN_LABEL,
    Artist,
    Finding,
    PlatformCandidate,
    Track,
    User,
    WhitelistEntry,
)
from app.scanners import itunes_scan
from app.scanners.base import RawCandidate
from app.services import detection
from app.services.normalize import normalize_title
from app.services.scoring import normalize_label

DEMO_SPOTIFY_ID = "m2_demo_twxny"


def ok(cond: bool) -> str:
    return "✅" if cond else "❌"


def _pirate_raw() -> RawCandidate:
    return RawCandidate(
        platform="spotify",
        native_id="m2_pirate_track_1",
        title="HEAVENLY JUMPSTYLE (Slowed)",
        url="https://open.spotify.com/track/m2_pirate_track_1",
        uploader="TWXNY",
        parsed_provider="13207436 Records DK",
        parsed_plabel="℗ 2026 13207436 Records DK",
        isrc="DEXX12600001",
        published_at=date(2026, 7, 13),
        duration_ms=143078,  # 1.25x slowed
    )


async def _reset_demo(session) -> Artist:
    """Idempotent: rebuild the demo artist + original track + own-label from scratch."""
    artist = await session.scalar(
        select(Artist).where(Artist.spotify_artist_id == DEMO_SPOTIFY_ID)
    )
    if artist is not None:
        # Wipe prior findings/candidates for a clean, repeatable run.
        await session.execute(delete(PlatformCandidate).where(PlatformCandidate.native_id == "m2_pirate_track_1"))
        await session.execute(delete(WhitelistEntry).where(WhitelistEntry.artist_id == artist.id))
        await session.execute(delete(Track).where(Track.primary_artist_id == artist.id))
    else:
        artist = Artist(name="TWXNY (M2 demo)", spotify_artist_id=DEMO_SPOTIFY_ID)
        session.add(artist)
        await session.flush()

    track = Track(
        primary_artist_id=artist.id,
        title="HEAVENLY JUMPSTYLE",
        normalized_title=normalize_title("HEAVENLY JUMPSTYLE"),
        credit="TWXNY, Sxilwix & Innxcence",
        release_date=date(2025, 11, 28),
        isrc="QZHN52501234",
        duration_ms=114462,
        spotify_track_id="m2_orig_spotify_id",
        source="spotify",
    )
    session.add(track)
    session.add(
        WhitelistEntry(
            scope="artist", artist_id=artist.id, entry_type=WL_OWN_LABEL,
            value="0to8", normalized_value=normalize_label("0to8"),
        )
    )
    await session.commit()
    return artist


async def main() -> None:
    async with SessionLocal() as session:
        admin = await session.scalar(select(User).where(User.tg_user_id == 42))
        if admin is None:
            admin = User(tg_user_id=42, display_name="Verify Admin", is_admin=True)
            session.add(admin)
            await session.commit()
    print(f"{ok(True)} demo admin ready (tg=42)")

    print("\n=== Детекция золотой пиратки (кассета, офлайн) ===")
    async with SessionLocal() as session:
        artist = await _reset_demo(session)
        summary = await detection.ingest_candidates(
            session, artist, [_pirate_raw()], download_covers=False
        )
        await session.commit()
    print(f"{ok(summary.new_candidates == 1)} кандидат создан (глобальный): +{summary.new_candidates}")
    print(f"{ok(summary.findings_created == 1)} находка создана: +{summary.findings_created}")
    print(f"{ok(summary.high == 1)} в диапазоне HIGH: {summary.high}")

    async with SessionLocal() as session:
        finding = await session.scalar(
            select(Finding).join(Track, Finding.track_id == Track.id)
            .where(Track.primary_artist_id == artist.id)
            .order_by(Finding.score.desc())
        )
        print(f"\n  Находка: score={finding.score} band={finding.band} status={finding.status} "
              f"(пороги {finding.thresholds_version})")
        print("  Разбор сигналов (человеческим языком):")
        for s in finding.signals:
            print(f"    • {s['label']}  (+{s['contribution']})")
        keys = {s["key"] for s in finding.signals}
        need = {"title_exact", "suffix", "duration_ratio", "pirate_label"}
        print(f"\n  {ok(need <= keys)} ключевые сигналы присутствуют: {sorted(need & keys)}")
        print(f"  {ok(finding.band == 'high' and finding.score >= 70)} итог: HIGH и score≥70")
        finding_id = finding.id

    print("\n=== dismiss → whitelist канала → rescan не флажит повторно ===")
    async with SessionLocal() as session:
        finding = await session.get(Finding, finding_id)
        await detection.add_whitelist_from_finding(session, finding, WL_CHANNEL, actor_user_id=None)
        await session.commit()
        print(f"{ok(finding.status == 'dismissed')} находка отклонена и канал в белом списке")

    async with SessionLocal() as session:
        artist = await session.scalar(select(Artist).where(Artist.spotify_artist_id == DEMO_SPOTIFY_ID))
        summary2 = await detection.ingest_candidates(
            session, artist, [_pirate_raw()], download_covers=False
        )
        await session.commit()
        total = await session.scalar(
            select(func.count(Finding.id)).join(Track, Finding.track_id == Track.id)
            .where(Track.primary_artist_id == artist.id)
        )
        print(f"{ok(summary2.findings_created == 0)} rescan: новых находок 0")
        print(f"{ok(total == 1)} всего находок по треку осталось: {total} (та же, отклонённая)")

    print("\n=== Живой скан iTunes (ярус 0, best-effort) ===")
    try:
        cands = await itunes_scan.scan_artist_page("1718381786", set(), limit=50)
        print(f"{ok(True)} iTunes ответил: релизов-кандидатов у TWXNY (без учёта каталога): {len(cands)}")
        for c in cands[:5]:
            print(f"    • «{c.title[:44]}» {c.duration_ms}мс {c.published_at}")
    except Exception as exc:  # noqa: BLE001
        print(f"⚠️  живой iTunes недоступен (нет исходящего интернета?): {exc}")

    await engine.dispose()
    print("\n✅ M2 verification complete.")


if __name__ == "__main__":
    asyncio.run(main())
