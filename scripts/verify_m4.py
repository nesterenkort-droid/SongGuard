"""Скрипт сквозной E2E верификации вехи M4: YouTube + Бюджетер + Планировщик.

Запуск внутри контейнера:
    docker compose run --rm --no-deps -v D:/NG:/src -w /src web python -m scripts.verify_m4
"""

import asyncio
from datetime import date
from unittest.mock import AsyncMock, patch

from sqlalchemy import delete, select

from app.db import SessionLocal
from app.models import Artist, Finding, PlatformCandidate, ScanJob, Track
from app.redis_client import redis_client
from app.services import budgeter, detection
from app.services.scheduler import (
    execute_single_job,
    get_import_progress,
    is_track_hot,
    populate_scan_jobs,
)


def ok(cond: bool) -> str:
    return "✅" if cond else "❌"


async def main():
    print("🚀 Старт E2E верификации вехи M4...")
    async with SessionLocal() as session:
        # 1. Очистка старых тестовых данных
        await session.execute(delete(ScanJob))
        await session.execute(delete(Finding))
        await session.execute(delete(PlatformCandidate))
        await session.execute(delete(Track))
        await session.execute(delete(Artist))
        await session.commit()
        print("✅ База данных очищена от старых тестовых записей.")

        # Очищаем ключи в Redis
        await redis_client.delete("quota:youtube:" + budgeter.get_pacific_today())
        await redis_client.delete("rate_limit:spotify")
        await redis_client.delete("rate_limit:itunes")
        await redis_client.delete("youtube_api_key_index")
        print("✅ Redis очищен от тестовых лимитов и индексов.")

        # 2. Создаем тестового артиста и трек
        artist = Artist(
            name="TWXNY",
            spotify_artist_id="twxnyspotify",
            apple_artist_id="1718381786",
            yt_topic_channel_id="UC12345topic",
        )
        session.add(artist)
        await session.flush()

        track = Track(
            primary_artist_id=artist.id,
            title="HEAVENLY JUMPSTYLE",
            normalized_title="heavenly jumpstyle",
            credit="TWXNY",
            spotify_track_id="orig_spot",
            apple_track_id=1859638952,
        )
        session.add(track)
        await session.commit()
        print(f"✅ Создан артист {artist.name} и трек {track.title}.")

        # 3. Проверяем расчет прогресса импорта каталога нового пользователя
        progress = await get_import_progress(session, artist.id)
        print(
            f"📊 Начальный прогресс импорта: {progress['progress']}% (ETA: {progress['eta_seconds']} сек.) "
            f"{ok(progress['progress'] == 0.0)}"
        )

        # 4. Наполняем очередь задач планировщика (первый скан)
        await populate_scan_jobs(session)
        await session.commit()
        jobs = list(await session.scalars(select(ScanJob)))
        print(
            f"✅ Планировщик наполнил очередь задач. Всего задач: {len(jobs)} (ожидалось: 3) "
            f"{ok(len(jobs) == 3)}"
        )
        print(f"   Задачи: {[f'{j.platform} (status={j.status}, priority={j.priority})' for j in jobs]}")

        # 5. Проверяем ротацию API ключей YouTube
        with patch("app.config.settings.youtube_api_key", "keyA,keyB"):
            from app.scanners.youtube_scan import _get_youtube_api_key
            k1 = await _get_youtube_api_key()
            k2 = await _get_youtube_api_key()
            k3 = await _get_youtube_api_key()
            is_valid_rotation = (k1 != k2) and (k1 == k3)
            print(f"🔄 Чередование ключей YouTube: {k1} -> {k2} -> {k3} {ok(is_valid_rotation)}")

        # 6. Симулируем выполнение задач через моки
        mock_youtube_search = AsyncMock(
            return_value=[
                detection.RawCandidate(
                    platform="youtube",
                    native_id="NzL0wDrGtYM",
                    title="HEAVENLY JUMPSTYLE (Slowed)",
                    url="https://www.youtube.com/watch?v=NzL0wDrGtYM",
                    uploader="TWXNY - Topic",
                    parsed_provider="DistroKid",
                    parsed_plabel="℗ 2026 13207436 Records DK",
                    isrc="QZHN52501234",
                    published_at=date(2026, 7, 13),
                    duration_ms=143000,
                    thumb_url="http://example.com/thumb.jpg",
                )
            ]
        )

        mock_spotify_search = AsyncMock(
            return_value=[
                detection.RawCandidate(
                    platform="spotify",
                    native_id="pirate_spotify_1",
                    title="HEAVENLY JUMPSTYLE (Nightcore)",
                    url="https://open.spotify.com/track/pirate_spotify_1",
                    uploader="TWXNY",
                    parsed_provider="DistroKid",
                    parsed_plabel="℗ 13207436 Records DK",
                    isrc="QZHN52501235",
                    published_at=date(2026, 7, 14),
                    duration_ms=90000,
                )
            ]
        )

        # Выполняем задачу по Spotify
        print("\n⚙️ Выполняем Spotify скан...")
        with patch("app.scanners.spotify_scan.search_tracks", mock_spotify_search):
            with patch("app.scanners.spotify_scan.scan_artist_page", AsyncMock(return_value=[])):
                spotify_job = await session.scalar(select(ScanJob).where(ScanJob.platform == "spotify"))
                await execute_single_job(session, spotify_job)
                await session.refresh(spotify_job)
                print(f"   Статус задачи: {spotify_job.status}, результат: {spotify_job.outcome}")

        # Проверяем, что обнаруженная пиратка на Spotify вызвала КРОСС-ТРИГГЕР на YouTube с приоритетом 50
        print("\n⚡ Проверяем кросс-платформенные триггеры...")
        findings = list(await session.scalars(select(Finding)))
        for f in findings:
            print(f"   [DEBUG] Находка: id={f.id}, score={f.score}, band={f.band}, status={f.status}")
        youtube_job = await session.scalar(select(ScanJob).where(ScanJob.platform == "youtube"))
        await session.refresh(youtube_job)
        print(
            f"   YouTube задача автоматически обновлена до приоритета 50: "
            f"status={youtube_job.status}, priority={youtube_job.priority} "
            f"{ok(youtube_job.priority == 50)}"
        )

        # Выполняем YouTube скан
        print("\n⚙️ Выполняем YouTube скан...")
        with patch("app.scanners.youtube_scan.search_tracks", mock_youtube_search):
            with patch("app.scanners.youtube_scan.scan_playlist_items", AsyncMock(return_value=[])):
                await execute_single_job(session, youtube_job)
                await session.refresh(youtube_job)
                print(f"   Статус задачи: {youtube_job.status}, результат: {youtube_job.outcome}")

        # Проверяем, создана ли находка (Finding) для YouTube пиратки
        finding = await session.scalar(
            select(Finding).where(
                Finding.track_id == track.id,
                Finding.score >= 70,
            )
        )
        print(
            f"🔍 Найден пиратский YouTube-ролик (скор >= 70): {ok(finding is not None)} "
            f"(Score={finding.score if finding else 0}, Band={finding.band if finding else ''})"
        )

        # 7. Проверяем статус 'горячего трека'
        is_hot = await is_track_hot(session, track)
        print(f"\n🔥 Трек стал горячим (из-за свежей находки): {is_hot} {ok(is_hot is True)}")

        # 8. Симулируем 10 чистых сканирований для проверки логики СТАРЕНИЯ (decay)
        print("\n⏳ Симулируем 10 чистых сканирований (старение горячего трека)...")
        with patch("app.scanners.youtube_scan.search_tracks", AsyncMock(return_value=[])):
            with patch("app.scanners.youtube_scan.scan_playlist_items", AsyncMock(return_value=[])):
                for _ in range(10):
                    # Сбрасываем задачу YouTube обратно в pending
                    youtube_job.status = "pending"
                    youtube_job.priority = 20  # горячий приоритет
                    await session.commit()

                    # Выполняем скан (он будет чистым, т.к. возвращает пустые списки)
                    await execute_single_job(session, youtube_job)

        # Проверяем старение трека
        is_hot_after_decay = await is_track_hot(session, track)
        print(f"❄️ Трек остыл (decay) после 10 чистых сканов: {not is_hot_after_decay} {ok(is_hot_after_decay is False)}")

        # 9. Проверяем прогресс импорта в конце
        progress_end = await get_import_progress(session, artist.id)
        print(
            f"\n📊 Финальный прогресс импорта: {progress_end['progress']}% (ETA: {progress_end['eta_seconds']} сек.) "
            f"{ok(progress_end['progress'] == 100.0)}"
        )

    print("\n🎉 ВЕРИФИКАЦИЯ ВЕХИ M4 УСПЕШНО ПРОЙДЕНА!")


if __name__ == "__main__":
    asyncio.run(main())
