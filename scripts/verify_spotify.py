"""LIVE Spotify import verification (real API + covers + ISRC).

Run:  docker compose run --rm -v D:/NG:/src -w /src web python -m scripts.verify_spotify
Finds TWXNY on Spotify, imports the catalog live (titles/ISRC/dates/durations +
downloads & perceptually-hashes covers), and reports.
"""

import asyncio

import httpx
from sqlalchemy import func, select

from app.db import SessionLocal, engine
from app.importers import spotify
from app.models import Artist, Track, User
from app.services import catalog


def ok(cond: bool) -> str:
    return "✅" if cond else "❌"


async def find_twxny_id() -> str | None:
    async with httpx.AsyncClient() as c:
        token = await spotify._get_token(c)
        r = await spotify._get(c, token, "/search", {"q": "TWXNY", "type": "artist", "limit": 5})
        items = r.get("artists", {}).get("items", [])
        print("  Кандидаты в Spotify:")
        for a in items:
            print(f"    - {a['name']} | id={a['id']} | подписчиков={a.get('followers', {}).get('total')}")
        for a in items:
            if a["name"].lower() == "twxny":
                return a["id"]
        return items[0]["id"] if items else None


async def main() -> None:
    print("=== Поиск TWXNY в Spotify ===")
    sid = await find_twxny_id()
    print(f"{ok(bool(sid))} Spotify artist id: {sid}")
    if not sid:
        return

    print("\n=== Живой импорт каталога из Spotify (с обложками и ISRC) ===")
    async with SessionLocal() as s:
        admin = await s.scalar(select(User).where(User.tg_user_id == 42))
        result = await catalog.import_artist_catalog(
            s, actor_user=admin, ref=f"spotify:artist:{sid}"
        )
    print(f"{ok(result['total'] > 0)} импорт: {result}")

    async with SessionLocal() as s:
        artist = await s.scalar(select(Artist).where(Artist.spotify_artist_id == sid))
        total = await s.scalar(
            select(func.count(Track.id)).where(Track.primary_artist_id == artist.id)
        )
        with_isrc = await s.scalar(
            select(func.count(Track.id)).where(
                Track.primary_artist_id == artist.id, Track.isrc.isnot(None)
            )
        )
        with_cover = await s.scalar(
            select(func.count(Track.id)).where(
                Track.primary_artist_id == artist.id, Track.cover_phash.isnot(None)
            )
        )
        variants = await s.scalar(
            select(func.count(Track.id)).where(
                Track.primary_artist_id == artist.id, Track.is_variant.is_(True)
            )
        )
        print(f"\n=== Каталог «{artist.name}» (Spotify) ===")
        print(f"{ok(total > 0)} треков: {total}")
        print(f"{ok(with_isrc > 0)} с ISRC: {with_isrc}")
        print(f"{ok(with_cover > 0)} с обложкой+хешем (скачано вживую): {with_cover}")
        print(f"   вариантов (slowed/nightcore/...): {variants}")

        rows = list(await s.scalars(
            select(Track).where(Track.primary_artist_id == artist.id)
            .order_by(Track.release_date.desc().nullslast()).limit(10)
        ))
        print("\n  Примеры треков:")
        for t in rows:
            tag = f" [{t.variant_label}]" if t.is_variant else ""
            cov = t.cover_phash[:8] + "…" if t.cover_phash else "нет"
            print(f"    • «{t.title[:38]}»{tag} isrc={t.isrc} обложка={cov} {t.duration_ms}мс")

    await engine.dispose()
    print("\n✅ Spotify live import complete.")


if __name__ == "__main__":
    asyncio.run(main())
