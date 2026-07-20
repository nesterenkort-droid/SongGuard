"""End-to-end M6 verification: takedown packets + recheck lifecycle.

Run inside the web container:
    docker compose run --rm --no-deps -v C:/SongGuard:/src -w /src web python -m scripts.verify_m6

Proves the M6 acceptance path (PLAN.md §13): находка → подтверждение → пакет с
архивом → (ручная отправка) → отслеживание → removed, on the golden case.
"""

import asyncio
from datetime import date
from unittest.mock import AsyncMock

from sqlalchemy import delete, select

from app.db import SessionLocal, engine
from app.models import (
    ROUTE_DISTRIBUTOR,
    Artist,
    ArtistMember,
    EvidenceArchive,
    Finding,
    PlatformCandidate,
    TakedownPacket,
    Track,
    User,
    WhitelistEntry,
)
from app.services import detection, recheck, takedown
from app.services.normalize import normalize_title

DEMO_MARKER = "m6_demo_twxny"


def ok(cond: bool) -> str:
    return "✅" if cond else "❌"


async def _reset_demo(session) -> tuple[Artist, User]:
    user = await session.scalar(select(User).where(User.tg_user_id == 600042))
    if user is None:
        user = User(tg_user_id=600042, display_name="M6 Verify Owner")
        session.add(user)
        await session.flush()
    user.legal_name = "0to8 LLC"
    user.legal_address = "1 Label Way, City"
    user.legal_email = "legal@0to8.example"

    # Candidates are global (not scoped to an artist) — clean up the demo one by id.
    await session.execute(
        delete(PlatformCandidate).where(PlatformCandidate.native_id == "m6_pirate_track_1")
    )

    artist = await session.scalar(select(Artist).where(Artist.spotify_artist_id == DEMO_MARKER))
    if artist is not None:
        await session.execute(delete(TakedownPacket).where(
            TakedownPacket.finding_id.in_(
                select(Finding.id).join(Track).where(Track.primary_artist_id == artist.id)
            )
        ))
        await session.execute(delete(EvidenceArchive).where(
            EvidenceArchive.finding_id.in_(
                select(Finding.id).join(Track).where(Track.primary_artist_id == artist.id)
            )
        ))
        await session.execute(delete(WhitelistEntry).where(WhitelistEntry.artist_id == artist.id))
        await session.execute(delete(Track).where(Track.primary_artist_id == artist.id))
        await session.execute(delete(ArtistMember).where(ArtistMember.artist_id == artist.id))
    else:
        artist = Artist(name="TWXNY (M6 demo)", spotify_artist_id=DEMO_MARKER)
        session.add(artist)
        await session.flush()
    session.add(ArtistMember(artist_id=artist.id, user_id=user.id, role="owner"))

    track = Track(
        primary_artist_id=artist.id, title="HEAVENLY JUMPSTYLE",
        normalized_title=normalize_title("HEAVENLY JUMPSTYLE"),
        isrc="QZHN52501234", release_date=date(2025, 11, 28),
        duration_ms=114462, spotify_track_id="m6_orig_spotify_id", source="spotify",
    )
    session.add(track)
    await session.commit()
    return artist, user


async def main() -> None:
    async with SessionLocal() as session:
        artist, user = await _reset_demo(session)
        track = await session.scalar(select(Track).where(Track.primary_artist_id == artist.id))

        cand = PlatformCandidate(
            platform="spotify", native_id="m6_pirate_track_1",
            title="HEAVENLY JUMPSTYLE (Slowed)", normalized_title=normalize_title("HEAVENLY JUMPSTYLE"),
            uploader="TWXNY", parsed_provider="13207436 Records DK",
            parsed_plabel="℗ 2026 13207436 Records DK",
            url="https://open.spotify.com/track/m6_pirate_track_1",
        )
        session.add(cand)
        await session.flush()
        finding = Finding(candidate_id=cand.id, track_id=track.id, score=150, band="high")
        session.add(finding)
        await session.commit()

    print("\n=== 1) Подтверждение → evidence-архив автоматически ===")
    async with SessionLocal() as session:
        finding = await session.scalar(
            select(Finding).join(PlatformCandidate).where(
                PlatformCandidate.native_id == "m6_pirate_track_1"
            )
        )
        await detection.transition(session, finding, "confirm", actor_user_id=None)
        await session.commit()
        evidence = await session.scalar(
            select(EvidenceArchive).where(EvidenceArchive.finding_id == finding.id)
        )
        print(f"{ok(evidence is not None)} evidence_archive создан: id={evidence.id if evidence else None}")
        print(f"{ok(evidence.candidate_snapshot['parsed_plabel'] == cand.parsed_plabel if evidence else False)} "
              f"снимок лейбла сохранён: {evidence.candidate_snapshot.get('parsed_plabel') if evidence else '—'}")
        finding_id = finding.id

    print("\n=== 2) Генерация пакета (дистрибьютор — приоритетный маршрут) ===")
    async with SessionLocal() as session:
        finding = await session.get(Finding, finding_id)
        cand = await session.get(PlatformCandidate, finding.candidate_id)
        user = await session.scalar(select(User).where(User.tg_user_id == 600042))
        routes = takedown.available_routes(cand)
        print(f"{ok(routes[0] == ROUTE_DISTRIBUTOR)} маршруты (дистрибьютор первый): {routes}")
        packet = await takedown.generate_packet(session, finding, ROUTE_DISTRIBUTOR, actor_user=user)
        await session.commit()
        print(f"{ok('13207436 Records DK' in packet.body_en)} текст жалобы содержит лейбл пирата")
        print(f"{ok(user.legal_name in packet.body_en)} текст жалобы содержит юр. имя заявителя")
        print(f"{ok(finding.status == 'packet_ready')} статус находки: {finding.status}")
        packet_id = packet.id

    print("\n=== 3) Блокировка без юридических данных ===")
    async with SessionLocal() as session:
        bare_user = User(tg_user_id=600043, display_name="No Legal Fields")
        session.add(bare_user)
        await session.flush()
        finding = await session.get(Finding, finding_id)
        blocked = False
        try:
            await takedown.generate_packet(session, finding, ROUTE_DISTRIBUTOR, actor_user=bare_user)
        except takedown.LegalFieldsMissing:
            blocked = True
        print(f"{ok(blocked)} генерация без legal-полей заблокирована")
        await session.rollback()

    print("\n=== 4) Ручная отправка ===")
    async with SessionLocal() as session:
        packet = await session.get(TakedownPacket, packet_id)
        user = await session.scalar(select(User).where(User.tg_user_id == 600042))
        await takedown.mark_sent(session, packet, actor_user_id=user.id)
        await session.commit()
        finding = await session.get(Finding, finding_id)
        print(f"{ok(packet.status == 'sent')} пакет отмечен отправленным")
        print(f"{ok(packet.follow_up_at is not None)} follow_up_at запланирован: {packet.follow_up_at}")
        print(f"{ok(finding.status == 'sent')} статус находки: {finding.status}")

    print("\n=== 5) Recheck: пиратка удалена → removed + уведомление ===")
    async with SessionLocal() as session:
        import app.services.liveness as liveness_module
        original_check = liveness_module.check_alive
        liveness_module.check_alive = AsyncMock(return_value=False)
        try:
            outcomes = await recheck.run_liveness_recheck(session)
        finally:
            liveness_module.check_alive = original_check
        finding = await session.get(Finding, finding_id)
        packet = await session.get(TakedownPacket, packet_id)
        # outcomes is a global tally across the (shared dev) DB, not scoped to this
        # demo — the per-finding/packet checks below are the real assertions.
        print(f"ℹ️  recheck (вся БД, включая прочие demo-находки): {outcomes}")
        print(f"{ok(finding.status == 'removed')} статус НАШЕЙ находки: {finding.status}")
        print(f"{ok(packet.outcome == 'removed')} исход пакета: {packet.outcome}")

        from app.models import NotificationOutbox
        row = await session.scalar(
            select(NotificationOutbox).where(
                NotificationOutbox.dedupe_key.like(f"removed:{finding_id}:%")
            )
        )
        print(f"{ok(row is not None)} уведомление о победе поставлено в очередь")

    await engine.dispose()
    print("\n✅ M6 verification complete.")


if __name__ == "__main__":
    asyncio.run(main())
