"""Планировщик сканирования каталога и старения треков.

Реализует pull-модель сканирования:
1. Заполнение очереди ScanJob задачами для треков, требующих проверки.
2. Обработку очереди по приоритетам с учетом дневных квот и circuit breaker.
3. Логику старения («decay») горячих треков: автоматический перевод в обычную
   ротацию после 10 подряд чистых сканов или 60 дней без новых находок.
4. Concurrency-блокировки в Redis для предотвращения параллельного сканирования одного трека.
5. Расчет прогресса первичного импорта каталога нового пользователя и ETA.

Все комментарии и лог-сообщения написаны на русском языке.
"""

import logging
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db import SessionLocal
from app.models import Artist, Finding, ScanJob, Track
from app.redis_client import redis_client
from app.scanners import itunes_scan, spotify_scan, youtube_scan
from app.services import budgeter, detection

logger = logging.getLogger("trackguard.scheduler")


class BudgetExhaustedError(Exception):
    pass


class PlatformAPIError(Exception):
    pass



async def is_track_hot(session: AsyncSession, track: Track, platform: str) -> bool:
    """Определяет, является ли трек 'горячим' (требующим ежедневного сканирования)."""
    if track.is_hot_pinned:
        return True

    if track.release_date and (datetime.now(UTC).date() - track.release_date).days < 60:
        return True


    # Ищем последнюю находку (активную или подтвержденную) для этого трека
    latest_finding = await session.scalar(
        select(Finding)
        .where(Finding.track_id == track.id)
        .order_by(desc(Finding.created_at))
        .limit(1)
    )
    if not latest_finding:
        return False

    # Если последняя находка старше hot_track_decay_days, то трек остыл (decay)
    decay_limit = datetime.now(UTC) - timedelta(days=settings.hot_track_decay_days)
    if latest_finding.created_at < decay_limit:
        return False

    # Считываем количество успешных чистых сканирований из Redis
    redis_decay_key = f"hot_decay:{track.id}:{platform}"
    val = await redis_client.get(redis_decay_key)
    clean_scans = int(val) if val else 0
    # Если чистых сканирований больше или равно hot_track_max_clean_scans, трек остыл
    if clean_scans >= settings.hot_track_max_clean_scans:
        return False

    return True


async def populate_scan_jobs(session: AsyncSession) -> None:
    """Анализирует все треки в БД и добавляет/обновляет задачи ScanJob в очереди.

    Периодичность:
    - Горячие треки: каждые 24 часа.
    - Обычные треки (в ротации): каждые 7 дней.
    """
    now = datetime.now(UTC)
    hot_cutoff = now - timedelta(hours=24)
    rotation_cutoff = now - timedelta(days=7)

    # Выбираем все треки для анализа
    tracks = list(await session.scalars(select(Track)))

    for track in tracks:
        for platform in ["youtube", "spotify", "itunes"]:
            is_hot = await is_track_hot(session, track, platform)
            priority = 20 if is_hot else 10
            cutoff = hot_cutoff if is_hot else rotation_cutoff

            # Проверяем, нужно ли сканировать
            last_scanned = getattr(
                track, f"last_scanned_{'apple' if platform == 'itunes' else platform}"
            )
            
            # Если никогда не сканировалось или вышло время действия прошлого скана
            if not last_scanned or last_scanned < cutoff:
                # Ищем существующую задачу
                job = await session.scalar(
                    select(ScanJob).where(
                        ScanJob.track_id == track.id,
                        ScanJob.platform == platform,
                    )
                )
                if not job:
                    # Создаем новую задачу
                    job = ScanJob(
                        track_id=track.id,
                        platform=platform,
                        priority=priority,
                        status="pending",
                    )
                    session.add(job)
                elif job.status in ["completed", "failed"]:
                    if job.status == "completed":
                        job.retry_count = 0
                    # Переводим старую задачу обратно в pending
                    job.status = "pending"
                    job.priority = priority
                    job.outcome = None
                    # Даем SQLAlchemy понять, что объект изменился
                    session.add(job)


async def execute_single_job(session: AsyncSession, job: ScanJob) -> None:
    """Выполняет сканирование для одной задачи ScanJob."""
    track = await session.get(Track, job.track_id)
    if not track:
        job.status = "failed"
        job.outcome = "Трек не найден"
        return

    artist = await session.get(Artist, track.primary_artist_id)
    if not artist:
        job.status = "failed"
        job.outcome = "Артист не найден"
        return

    platform = job.platform
    lock_key = f"lock:scan:{track.id}:{platform}"

    # Ставим Redis блокировку на 30 минут, чтобы избежать параллельных запусков
    acquired = await redis_client.set(lock_key, "1", nx=True, ex=1800)
    if not acquired:
        logger.info("Сканирование %s для трека %s уже заблокировано", platform, track.id)
        return

    job.status = "running"
    await session.commit()

    logger.info("Запуск задачи %s для трека %s (приоритет %s)", platform, track.title, job.priority)
    raws = []
    try:
        # Проверяем предохранитель
        if await budgeter.is_circuit_breaker_active(platform):
            raise BudgetExhaustedError(f"Предохранитель активен для платформы {platform}")

        # Выполняем сканирование в зависимости от платформы
        q = f"{track.title} {artist.name}"
        if platform == "youtube":
            if not settings.youtube_search_enabled:
                logger.info("YouTube-поиск отключён (YOUTUBE_SEARCH_ENABLED=false) — пропуск")
                raws = []
            else:
                # 1. Проверяем и списываем дневную квоту поиска YouTube
                # Лимитируем только реальный поиск (Priority 10, 20, 100)
                if not await budgeter.consume_youtube_search_quota(session):
                    raise BudgetExhaustedError("Превышена дневная квота поиска YouTube")

                # 2. Делаем поиск трека
                raws.extend(await youtube_scan.search_tracks(q, limit=5))

                # 3. Дополнительно проверяем Topic-канал (не чаще раза в 12 часов для артиста)
                if artist.yt_topic_channel_id:
                    topic_lock_key = f"youtube:topic_scanned:{artist.id}"
                    if not await redis_client.exists(topic_lock_key):
                        is_uc = artist.yt_topic_channel_id.startswith("UC")
                        playlist_id = (
                            "UU" + artist.yt_topic_channel_id[2:]
                            if is_uc
                            else artist.yt_topic_channel_id
                        )
                        topic_raws = await youtube_scan.scan_playlist_items(
                            playlist_id, known_video_ids=set(), limit=15
                        )
                        if topic_raws:
                            raws.extend(topic_raws)
                            await redis_client.set(topic_lock_key, "1", ex=43200)  # 12 часов

        elif platform == "spotify":
            if not settings.spotify_enabled:
                logger.info("Spotify отключён (SPOTIFY_ENABLED=false) — пропуск скана")
                raws = []
            else:
                # Соблюдаем token-bucket частоту запросов
                # Spotify: 30 запросов в минуту (емкость 5, восполнение 0.5 токенов в секунду)
                if not await budgeter.acquire_token(platform, capacity=5, refill_rate=0.5):
                    raise BudgetExhaustedError("Превышен лимит частоты запросов Spotify (token bucket)")

                raws.extend(await spotify_scan.search_tracks(q, limit=5))

                # Сканируем страницу артиста (не чаще раза в 6 часов)
                if artist.spotify_artist_id:
                    spot_lock_key = f"spotify:artist_scanned:{artist.id}"
                    if not await redis_client.exists(spot_lock_key):
                        spot_res = await session.scalars(
                            select(Track.spotify_track_id)
                            .where(
                                Track.primary_artist_id == artist.id,
                                Track.spotify_track_id.is_not(None),
                            )
                        )
                        known_ids = set(spot_res)
                        raws.extend(
                            await spotify_scan.scan_artist_page(
                                artist.spotify_artist_id, known_ids
                            )
                        )
                        await redis_client.set(spot_lock_key, "1", ex=21600)  # 6 часов

        elif platform == "itunes":
            # iTunes: 20 запросов в минуту (емкость 3, восполнение 0.33 токенов в секунду)
            if not await budgeter.acquire_token(platform, capacity=3, refill_rate=0.33):
                raise BudgetExhaustedError("Превышен лимит частоты запросов iTunes (token bucket)")

            raws.extend(await itunes_scan.search_tracks(q, limit=5))

            # Сканируем страницу артиста Apple (не чаще раза в 6 часов)
            if artist.apple_artist_id:
                apple_lock_key = f"apple:artist_scanned:{artist.id}"
                if not await redis_client.exists(apple_lock_key):
                    apple_res = await session.scalars(
                        select(Track.apple_track_id)
                        .where(
                            Track.primary_artist_id == artist.id,
                            Track.apple_track_id.is_not(None),
                        )
                    )
                    known_ids = {str(val) for val in apple_res}
                    apple_raws = await itunes_scan.scan_artist_page(
                        artist.apple_artist_id, known_ids
                    )
                    await detection._enrich_apple_labels(apple_raws)
                    raws.extend(apple_raws)
                    await redis_client.set(apple_lock_key, "1", ex=21600)  # 6 часов

        # Инжектируем собранных кандидатов и рассчитываем совпадения
        summary = await detection.ingest_candidates(session, artist, raws)

        # Обновляем счетчик чистых сканирований в Redis для старения (decay)
        redis_decay_key = f"hot_decay:{track.id}:{platform}"
        if summary.findings_created > 0:
            await redis_client.set(redis_decay_key, "0")
        else:
            await redis_client.incr(redis_decay_key)

        # Обновляем дату последнего сканирования на треке
        now = datetime.now(UTC)
        if platform == "youtube":
            track.last_scanned_youtube = now
        elif platform == "spotify":
            track.last_scanned_spotify = now
        elif platform == "itunes":
            track.last_scanned_apple = now

        job.status = "completed"
        job.last_scanned_at = now
        outcome_msg = (
            f"Успешно. Найдено кандидатов: {summary.scanned}, "
            f"новых находок: {summary.findings_created}"
        )
        job.outcome = outcome_msg
        logger.info("Задача %s для трека %s успешно завершена", platform, track.title)

    except Exception as e:
        logger.exception(
            "Ошибка при выполнении задачи сканирования %s для трека %s",
            platform,
            track.title,
        )
        job.retry_count = getattr(job, "retry_count", 0) + 1
        if job.retry_count >= 3:
            job.status = "abandoned"
        else:
            job.status = "failed"
        job.outcome = str(e)[:500]
        
        # Если это ошибка превышения квот или лимитов - триггерим предохранитель
        if isinstance(e, (httpx.HTTPStatusError, PlatformAPIError)):
            await budgeter.trip_circuit_breaker(platform)

    finally:
        if acquired:
            await redis_client.delete(lock_key)
        await session.commit()


async def process_pending_jobs(session: AsyncSession, limit: int = 5) -> None:
    """Выбирает и запускает выполнение `limit` задач из очереди pending ScanJob.

    Сортирует по приоритету (сначала ручные, затем кросс-триггеры, затем горячие, затем ротация).
    """
    jobs = list(
        await session.scalars(
            select(ScanJob)
            .where(ScanJob.status == "pending")
            .order_by(desc(ScanJob.priority), ScanJob.created_at)
            .limit(limit)
        )
    )
    for job in jobs:
        await execute_single_job(session, job)


async def scheduler_tick(ctx: dict) -> None:
    """Периодическая задача планировщика arq.

    Вызывается каждые 15 минут, наполняет очередь и обрабатывает часть задач.
    """
    logger.info("Тик планировщика: наполнение очереди и запуск задач")
    async with SessionLocal() as session:
        # Наполняем очередь задач
        await populate_scan_jobs(session)
        await session.commit()

        # Обрабатываем накопившиеся задачи (запускаем до 5 задач за тик)
        await process_pending_jobs(session, limit=5)


async def get_import_progress(session: AsyncSession, artist_id: int) -> dict:
    """Рассчитывает процент выполнения первичного импорта и ETA для артиста."""
    # Первичный импорт считается завершенным по YouTube сканированию всех треков
    total = await session.scalar(
        select(func.count(Track.id)).where(Track.primary_artist_id == artist_id)
    )
    if not total:
        return {"progress": 100.0, "scanned": 0, "total": 0, "eta_seconds": 0}

    scanned = await session.scalar(
        select(func.count(Track.id)).where(
            Track.primary_artist_id == artist_id,
            Track.last_scanned_youtube.is_not(None),
        )
    )

    progress = round((scanned / total) * 100.0, 1)
    remaining = total - scanned
    # На один трек с учетом пауз и квот закладываем в среднем 5 секунд
    eta_seconds = remaining * 5

    return {
        "progress": progress,
        "scanned": scanned,
        "total": total,
        "eta_seconds": eta_seconds,
    }
