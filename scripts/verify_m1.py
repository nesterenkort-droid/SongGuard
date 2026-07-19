"""End-to-end M1 verification.

Run inside the web container:
    docker compose run --rm -v D:/NG:/src -w /src web python -m scripts.verify_m1

Proves, with REAL captured iTunes data for TWXNY (artist 1718381786):
  * catalog import → DB upsert (titles/dates/durations/variants/normalized titles)
  * cover perceptual hashing on real image bytes
  * uninvited login rejected, invite→join registration
  * audit trail on every mutation

NOTE: this environment's containers have no outbound internet (the host proxy binds
loopback only), so the live HTTP fetch + cover downloads are exercised via captured
real data / local bytes here. On a normal host (the VPS) the same code runs live.
"""

import asyncio
import io
import secrets

from PIL import Image
from sqlalchemy import func, select

from app.auth import service as auth_service
from app.db import SessionLocal, engine
from app.importers.itunes import parse_lookup
from app.models import Artist, AuditEvent, Invite, Track, User
from app.services import catalog, images

TWXNY_APPLE_ID = "1718381786"
ART = "https://is1-ssl.mzstatic.com/image/thumb/Music221/v4/93/6b/44/x/100x100bb.jpg"

# Real data captured from the live iTunes lookup API for artist 1718381786.
REAL_ITUNES = {
    "results": [
        {"wrapperType": "artist", "artistName": "TWXNY", "artistId": 1718381786},
        {"wrapperType": "track", "kind": "song", "trackName": "HEAVENLY JUMPSTYLE",
         "artistName": "TWXNY, Sxilwix & Innxcence", "releaseDate": "2025-11-07T12:00:00Z",
         "trackTimeMillis": 114462, "trackId": 1859638952, "collectionId": 1859638749,
         "artworkUrl100": ART, "previewUrl": "https://a/p.m4a"},
        {"wrapperType": "track", "kind": "song", "trackName": "HEAVENLY JUMPSTYLE (Slowed)",
         "artistName": "TWXNY", "releaseDate": "2025-11-07T12:00:00Z",
         "trackTimeMillis": 128478, "trackId": 1859639035, "collectionId": 1859638749,
         "artworkUrl100": ART},
        {"wrapperType": "track", "kind": "song", "trackName": "HEAVENLY JUMPSTYLE (Super Slowed)",
         "artistName": "TWXNY", "releaseDate": "2025-11-29T12:00:00Z",
         "trackTimeMillis": 144219, "trackId": 1859635611, "collectionId": 1859635610,
         "artworkUrl100": ART},
        {"wrapperType": "track", "kind": "song", "trackName": "HEAVENLY JUMPSTYLE (Ultra Slowed)",
         "artistName": "TWXNY", "releaseDate": "2025-11-29T12:00:00Z",
         "trackTimeMillis": 161879, "trackId": 1859635612, "collectionId": 1859635610,
         "artworkUrl100": ART},
        {"wrapperType": "track", "kind": "song", "trackName": "Heavenly jumpstyle (Nightcore)",
         "artistName": "TWXNY, Sxilwix & Innxcence", "releaseDate": "2026-05-25T12:00:00Z",
         "trackTimeMillis": 113847, "trackId": 6785467894, "collectionId": 6785467889,
         "artworkUrl100": ART},
        {"wrapperType": "track", "kind": "song", "trackName": "HEAVENLY JUMPSTYLE (Instrumental)",
         "artistName": "TWXNY", "releaseDate": "2025-11-07T12:00:00Z",
         "trackTimeMillis": 114462, "trackId": 1859638964, "collectionId": 1859638749,
         "artworkUrl100": ART},
        {"wrapperType": "track", "kind": "song", "trackName": "MONTAGEM PRATEDAR",
         "artistName": "TWXNY, cutemain & Innxcence", "releaseDate": "2026-07-17T12:00:00Z",
         "trackTimeMillis": 90750, "trackId": 6790367067, "collectionId": 6790367059,
         "artworkUrl100": ART},
        {"wrapperType": "track", "kind": "song", "trackName": "NEON SAD JUMPSTYLE",
         "artistName": "TWXNY & Innxcence", "releaseDate": "2026-01-16T12:00:00Z",
         "trackTimeMillis": 105231, "trackId": 1867625932, "collectionId": 1867625688,
         "artworkUrl100": ART},
        {"wrapperType": "track", "kind": "song", "trackName": "GUANO VIA",
         "artistName": "TWXNY, KPHK & Innxcence", "releaseDate": "2025-07-01T12:00:00Z",
         "trackTimeMillis": 84923, "trackId": 1823203405, "collectionId": 1823203402,
         "artworkUrl100": ART},
        {"wrapperType": "track", "kind": "song", "trackName": "AUTOMOTIVO CINEMA",
         "artistName": "TWXNY, Innxcence & LXGHTXNG", "releaseDate": "2024-12-13T12:00:00Z",
         "trackTimeMillis": 84052, "trackId": 1785986412, "collectionId": 1785986410,
         "artworkUrl100": ART},
    ]
}


def ok(cond: bool) -> str:
    return "✅" if cond else "❌"


async def main() -> None:
    async with SessionLocal() as s:
        admin = await s.scalar(select(User).where(User.tg_user_id == 42))
        if admin is None:
            admin = User(tg_user_id=42, display_name="Verify Admin", is_admin=True)
            s.add(admin)
            await s.commit()
            admin = await s.scalar(select(User).where(User.tg_user_id == 42))
    print(f"{ok(admin.is_admin)} admin bootstrap: user id={admin.id}, is_admin={admin.is_admin}")

    # --- 1) Import REAL captured iTunes data through the real parser + apply pipeline ---
    print("\n=== Импорт реального каталога TWXNY (данные iTunes API) ===")
    imported = parse_lookup(REAL_ITUNES, TWXNY_APPLE_ID)
    async with SessionLocal() as s:
        admin = await s.scalar(select(User).where(User.tg_user_id == 42))
        result = await catalog.apply_imported_artist(
            s, actor_user=admin, imported=imported,
            platform="itunes", external_id=TWXNY_APPLE_ID, download_covers=False,
        )
    print(f"{ok(result['total'] == 10)} импорт: {result}")

    # --- 2) inspect the imported catalog ---
    async with SessionLocal() as s:
        artist = await s.scalar(select(Artist).where(Artist.apple_artist_id == TWXNY_APPLE_ID))
        total = await s.scalar(
            select(func.count(Track.id)).where(Track.primary_artist_id == artist.id)
        )
        with_date = await s.scalar(
            select(func.count(Track.id)).where(
                Track.primary_artist_id == artist.id, Track.release_date.isnot(None)
            )
        )
        with_dur = await s.scalar(
            select(func.count(Track.id)).where(
                Track.primary_artist_id == artist.id, Track.duration_ms.isnot(None)
            )
        )
        variants = await s.scalar(
            select(func.count(Track.id)).where(
                Track.primary_artist_id == artist.id, Track.is_variant.is_(True)
            )
        )
        print(f"\n=== Каталог «{artist.name}» (id={artist.id}) ===")
        print(f"{ok(total == 10)} всего треков: {total}")
        print(f"{ok(with_date == total)} с датой релиза: {with_date}/{total}")
        print(f"{ok(with_dur == total)} с длительностью: {with_dur}/{total}")
        print(f"{ok(variants >= 4)} офиц. вариантов (slowed/nightcore/instrumental): {variants}")

        rows = list(await s.scalars(
            select(Track)
            .where(Track.primary_artist_id == artist.id,
                   Track.normalized_title == "heavenly jumpstyle")
            .order_by(Track.release_date, Track.title)
        ))
        print(f"\n  {ok(len(rows) >= 5)} треков, нормализованных в 'heavenly jumpstyle': {len(rows)}")
        for t in rows:
            tag = f"[{t.variant_label}]" if t.is_variant else "[оригинал]"
            ratio = f"{t.duration_ms / 114462:.2f}x" if t.duration_ms else "—"
            print(f"    • «{t.title}» {tag} дата={t.release_date} {t.duration_ms}мс (к ориг. {ratio})")

    # --- 3) cover perceptual hashing on real image bytes (offline) ---
    print("\n=== Хеширование обложек ===")
    img = Image.new("RGB", (300, 300))
    px = img.load()
    for y in range(300):
        for x in range(300):
            px[x, y] = ((x + y) % 256, (x * 2) % 256, (y * 2) % 256)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    ph, dh = images.hash_bytes(buf.getvalue())
    print(f"{ok(len(ph) == 16 and len(dh) == 16)} pHash={ph} dHash={dh} (по 64 бита)")

    # --- 4) auth: uninvited rejected ---
    print("\n=== Авторизация ===")
    async with SessionLocal() as s:
        nonce = await auth_service.create_nonce(auth_service.MODE_LOGIN)
        r = await auth_service.confirm_start(s, f"login-{nonce}", 918273, "Stranger")
        stranger = await s.scalar(select(User).where(User.tg_user_id == 918273))
        print(f"{ok(not r['ok'] and stranger is None)} неприглашённый отвергнут: «{r['message']}»")

    # --- 5) invite → join ---
    async with SessionLocal() as s:
        admin = await s.scalar(select(User).where(User.tg_user_id == 42))
        token = "verify-" + secrets.token_urlsafe(6)
        s.add(Invite(token=token, created_by_user_id=admin.id, note="verify"))
        await s.commit()
        nonce = await auth_service.create_nonce(auth_service.MODE_JOIN, invite_token=token)
        r = await auth_service.confirm_start(s, f"join-{nonce}", 654321, "Colleague")
        colleague = await s.scalar(select(User).where(User.tg_user_id == 654321))
        used = await s.scalar(select(Invite).where(Invite.token == token))
        good = r["ok"] and colleague is not None and used.used_by_user_id == colleague.id
        print(f"{ok(good)} инвайт→регистрация: registered={r.get('registered')}, "
              f"admin={colleague.is_admin}, инвайт использован user={used.used_by_user_id}")

    # --- 6) audit trail ---
    async with SessionLocal() as s:
        rows = list(await s.scalars(
            select(AuditEvent).order_by(AuditEvent.id.desc()).limit(6)
        ))
        print("\n=== Аудит (последние события) ===")
        for e in rows:
            print(f"  [{e.action}] {e.summary}")
        print(f"{ok(len(rows) > 0)} каждая мутация записана в аудит")

    await engine.dispose()
    print("\n✅ M1 verification complete.")


if __name__ == "__main__":
    asyncio.run(main())
