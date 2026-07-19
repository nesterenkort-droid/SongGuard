"""Тесты для планировщика сканирования, старения треков и прогресса импорта."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import delete, select

from app.config import settings
from app.models import Artist, Finding, PlatformCandidate, ScanJob, Track
from app.redis_client import redis_client
from app.services.scheduler import (
    get_import_progress,
    is_track_hot,
    populate_scan_jobs,
    process_pending_jobs,
)


@pytest.fixture
async def setup_data(db_session):
    # Очищаем таблицы перед тестами
    await db_session.execute(delete(ScanJob))
    await db_session.execute(delete(Finding))
    await db_session.execute(delete(PlatformCandidate))
    await db_session.execute(delete(Track))
    await db_session.execute(delete(Artist))
    await db_session.flush()

    artist = Artist(
        name="TWXNY",
        spotify_artist_id="twxnyspotify",
        apple_artist_id="1718381786",
        yt_topic_channel_id="UC12345topic",
    )
    db_session.add(artist)
    await db_session.flush()

    track = Track(
        primary_artist_id=artist.id,
        title="HEAVENLY JUMPSTYLE",
        normalized_title="heavenly jumpstyle",
        credit="TWXNY",
        spotify_track_id="orig_spot",
        apple_track_id=1859638952,
    )
    db_session.add(track)
    await db_session.flush()

    return artist, track


@pytest.mark.asyncio
async def test_is_track_hot_decay_logic(db_session, setup_data):
    artist, track = setup_data

    # 1. По умолчанию новый трек не горячий
    assert await is_track_hot(db_session, track) is False

    # 2. Если трек закреплен (is_hot_pinned), то он горячий
    track.is_hot_pinned = True
    await db_session.flush()
    assert await is_track_hot(db_session, track) is True

    # Снимаем закрепление
    track.is_hot_pinned = False
    await db_session.flush()

    # 3. Добавляем недавнюю находку
    cand = PlatformCandidate(
        platform="spotify",
        native_id="pirate_1",
        title="HEAVENLY JUMPSTYLE",
        normalized_title="heavenly jumpstyle",
        url="http://example.com",
    )
    db_session.add(cand)
    await db_session.flush()

    finding = Finding(
        track_id=track.id,
        candidate_id=cand.id,
        score=80,
        band="high",
        status="detected",
    )
    db_session.add(finding)
    await db_session.flush()
    assert await is_track_hot(db_session, track) is True

    # 4. Проверяем старение по времени (находка создана 61 день назад при лимите 60 дней)
    with patch.object(settings, "hot_track_decay_days", 60):
        # Меняем дату находки «в прошлое»
        finding.created_at = datetime.now(UTC) - timedelta(days=61)
        await db_session.flush()
        assert await is_track_hot(db_session, track) is False

        # Возвращаем дату находки в настоящее время
        finding.created_at = datetime.now(UTC)
        await db_session.flush()
        assert await is_track_hot(db_session, track) is True

    # 5. Проверяем старение по числу чистых сканов (лимит 2 чистых скана)
    redis_decay_key = f"track:clean_scans:{track.id}"
    await redis_client.delete(redis_decay_key)

    with patch.object(settings, "hot_track_max_clean_scans", 2):
        # 1 чистый скан
        await redis_client.incr(redis_decay_key)
        # 1 скан < 2, трек все еще горячий
        assert await is_track_hot(db_session, track) is True

        # 2 чистых скана
        await redis_client.incr(redis_decay_key)
        # 2 чистых скана >= 2, трек остывает!
        assert await is_track_hot(db_session, track) is False

    await redis_client.delete(redis_decay_key)


@pytest.mark.asyncio
async def test_populate_scan_jobs(db_session, setup_data):
    artist, track = setup_data

    # Запускаем генерацию задач
    await populate_scan_jobs(db_session)
    await db_session.flush()

    # Должно создаться 3 задачи (по одной на каждую платформу)
    jobs = list(await db_session.scalars(select(ScanJob)))
    assert len(jobs) == 3
    assert all(j.status == "pending" for j in jobs)
    assert all(j.priority == 10 for j in jobs)  # ротация (холодный трек)

    # Имитируем, что задачи завершены
    now = datetime.now(UTC)
    for j in jobs:
        j.status = "completed"
        j.last_scanned_at = now
    track.last_scanned_youtube = now
    track.last_scanned_spotify = now
    track.last_scanned_apple = now
    await db_session.flush()

    # Снова запускаем наполнение - новые задачи создаваться не должны, т.к. лимит времени не вышел
    await populate_scan_jobs(db_session)
    await db_session.flush()
    jobs2 = list(await db_session.scalars(select(ScanJob)))
    assert all(j.status == "completed" for j in jobs2)

    # Имитируем выход времени ротации (сдвигаем скан назад на 8 дней)
    for j in jobs2:
        j.last_scanned_at = datetime.now(UTC) - timedelta(days=8)
    track.last_scanned_youtube = datetime.now(UTC) - timedelta(days=8)
    track.last_scanned_spotify = datetime.now(UTC) - timedelta(days=8)
    track.last_scanned_apple = datetime.now(UTC) - timedelta(days=8)
    await db_session.flush()

    # Теперь задачи должны обновиться обратно в pending
    await populate_scan_jobs(db_session)
    await db_session.flush()
    jobs3 = list(await db_session.scalars(select(ScanJob)))
    assert all(j.status == "pending" for j in jobs3)


@pytest.mark.asyncio
async def test_execute_scan_jobs_with_mocks(db_session, setup_data):
    artist, track = setup_data

    # Создаем pending задачу
    job = ScanJob(track_id=track.id, platform="youtube", status="pending", priority=10)
    db_session.add(job)
    await db_session.flush()

    # Мокаем вызовы к внешним API
    mock_search = AsyncMock(return_value=[])
    mock_playlist = AsyncMock(return_value=[])
    mock_quota = AsyncMock(return_value=True)

    with patch("app.scanners.youtube_scan.search_tracks", mock_search):
        with patch("app.scanners.youtube_scan.scan_playlist_items", mock_playlist):
            with patch("app.services.budgeter.consume_youtube_search_quota", mock_quota):
                await process_pending_jobs(db_session, limit=1)
                
                # Задача должна успешно завершиться
                await db_session.refresh(job)
                assert job.status == "completed"
                assert "Успешно" in job.outcome
                mock_search.assert_called_once()
                mock_quota.assert_called_once()
                
                # Должен быть обновлен трек
                await db_session.refresh(track)
                assert track.last_scanned_youtube is not None


@pytest.mark.asyncio
async def test_import_progress(db_session, setup_data):
    artist, track = setup_data

    # Еще не сканирован
    p = await get_import_progress(db_session, artist.id)
    assert p["progress"] == 0.0
    assert p["total"] == 1
    assert p["scanned"] == 0
    assert p["eta_seconds"] == 5

    # Имитируем успешный скан
    track.last_scanned_youtube = datetime.now(UTC)
    await db_session.flush()

    p2 = await get_import_progress(db_session, artist.id)
    assert p2["progress"] == 100.0
    assert p2["total"] == 1
    assert p2["scanned"] == 1
    assert p2["eta_seconds"] == 0
