"""Скрипт сквозной E2E верификации вехи M5: Аудио-фингерпринтинг и ИИ-судья.

Запуск внутри контейнера:
    docker compose run --rm --no-deps -v D:/NG:/src -w /src web python -m scripts.verify_m5
"""

import asyncio
import os
import shutil
import tempfile
from datetime import date
from unittest.mock import AsyncMock, patch

from sqlalchemy import delete, select

from app.db import SessionLocal
from app.models import Artist, Finding, PlatformCandidate, ScanJob, Track
from app.redis_client import redis_client
from app.services import ai_judge, detection, panako
from tests.test_panako_audio import _generate_beep_audio


def ok(cond: bool) -> str:
    return "✅" if cond else "❌"


async def main():
    print("🚀 Старт E2E верификации вехи M5...")
    async with SessionLocal() as session:
        # 1. Очистка старых данных
        await session.execute(delete(ScanJob))
        await session.execute(delete(Finding))
        await session.execute(delete(PlatformCandidate))
        await session.execute(delete(Track))
        await session.execute(delete(Artist))
        await session.commit()

        await panako.clear_database()
        if os.path.exists(panako.ORIGINALS_DIR):
            shutil.rmtree(panako.ORIGINALS_DIR)
        os.makedirs(panako.ORIGINALS_DIR, exist_ok=True)

        key = ai_judge._get_spend_key()
        await redis_client.delete(key)
        print("✅ База данных, Redis и Panako очищены.")

        # 2. Создаем тестового артиста и трек
        artist = Artist(name="TWXNY")
        session.add(artist)
        await session.flush()

        track = Track(
            primary_artist_id=artist.id,
            title="HEAVENLY JUMPSTYLE",
            normalized_title="heavenly jumpstyle",
            credit="TWXNY",
            isrc="QZHN52501234",
            duration_ms=15000,
        )
        session.add(track)
        await session.commit()
        print(f"✅ Создан артист {artist.name} и трек {track.title} (ID={track.id}).")

        # 3. Синтезируем оригинал и добавляем в Panako
        with tempfile.TemporaryDirectory() as tmpdir:
            orig_wav = os.path.join(tmpdir, "original.wav")
            await _generate_beep_audio(orig_wav, duration_sec=15)
            
            # Копируем в originals_dir для ингеста
            dest_orig = os.path.join(panako.ORIGINALS_DIR, f"{track.id}_1.00.wav")
            shutil.copy(orig_wav, dest_orig)
            
            print("⚙️ Индексируем эталоны в Panako...")
            assert await panako.store_reference(track.id, orig_wav) is True
            print("✅ Эталоны успешно сохранены в Panako.")

            # 4. Симулируем кандидата на YouTube (замедленного на 20% -> 0.8x скорость)
            query_slow_wav = os.path.join(tmpdir, "query_slow.wav")
            await _generate_beep_audio(query_slow_wav, duration_sec=15, speed=0.8)

            # Создаем RawCandidate для YouTube
            # Заменяем скачивание YouTube-аудио на подсовывание нашего локального файла
            print("\n⚙️ Запускаем E2E детекцию для замедленной пиратки YouTube...")
            raw_youtube = detection.RawCandidate(
                platform="youtube",
                native_id="NzL0wDrGtYM",
                title="HEAVENLY JUMPSTYLE (Slowed)",
                url="https://www.youtube.com/watch?v=NzL0wDrGtYM",
                uploader="TWXNY - Topic",
                parsed_provider="DistroKid",
                parsed_plabel="℗ 2026 13207436 Records DK",
                isrc="QZHN52501299",  # Другой ISRC
                published_at=date(2026, 7, 13),
                duration_ms=18750,  # 15000 / 0.8
            )

            # Мокаем скачивание аудио, чтобы оно возвращало наш синтезированный файл
            async def mock_download_yt(url, dest):
                shutil.copy(query_slow_wav, dest)
                return True

            with patch("app.services.detection.download_youtube_audio", mock_download_yt):
                await detection.ingest_candidates(
                    session, artist, [raw_youtube], download_covers=False
                )

            # 5. Проверяем результаты детекции в БД
            await session.commit()
            finding = await session.scalar(
                select(Finding).where(Finding.track_id == track.id)
            )

            print(f"📊 Статус находки: {finding.status}")
            print(f"📊 Скор находки: {finding.score} / 100")
            print(f"📊 Сигналы: {finding.signals}")
            print(f"📊 Аудио-матч: {finding.audio_match}")

            # Проверки:
            # Ожидаемый базовый скор метаданных:
            # title_exact (40) + uploader matching artist (20) + duration preset match (30) + DistroKid label (25) = 115
            # + аудио-матч (+40) = 155 (капится до 100 в интерфейсе или скор идет выше).
            # В любом случае band == high, статус pending_review.
            
            is_matched = finding.audio_match.get("matched") is True
            is_correct_stretch = abs(finding.audio_match.get("true_stretch") - 0.8) < 0.05
            is_high_score = finding.score >= 70
            is_status_ok = finding.status == "pending_review"

            print(f"   Аудио совпало по Panako: {finding.audio_match.get('matched')} {ok(is_matched)}")
            print(f"   Коэффициент замедления ~0.8: {finding.audio_match.get('true_stretch')} {ok(is_correct_stretch)}")
            print(f"   Скор >= 70 (high band): {finding.score} {ok(is_high_score)}")
            print(f"   Статус = pending_review: {finding.status} {ok(is_status_ok)}")

            # 6. Симулируем mid-band кандидата для проверки ИИ-судьи
            # Скор будет: title_fuzzy (30) = 30 -> попадает в mid-band (40-69) если добавим другие метаданные,
            # например, uploader (20) = 50. Без аудио матча.
            print("\n⚙️ Запускаем E2E детекцию для mid-band кандидата и ИИ-судьи...")
            raw_mid = detection.RawCandidate(
                platform="itunes",
                native_id="12345678",
                title="HEAVENLY JUMPSTYLE REMIX",
                url="http://apple.com/12345678",
                uploader="TWXNY",  # +20 artist match
                parsed_provider="Different Label",  # +20 foreign label
                published_at=date(2026, 7, 14),
                duration_ms=16000,
            )

            # Мокаем ИИ-судью, возвращающего вердикт remix
            mock_llm_response = ai_judge.JudgeVerdict(
                verdict="remix",
                confidence=85,
                reasoning_ru="Это неофициальный ремикс, так как в названии присутствует слово REMIX.",
                cost_usd=0.0015
            )

            async def mock_download_preview(url, dest):
                # Ничего не возвращаем, аудио-проверка не сработает
                return False

            with patch("app.services.detection.download_preview_audio", mock_download_preview):
                with patch(
                    "app.services.detection.evaluate_candidate",
                    AsyncMock(return_value=mock_llm_response),
                ) as mock_ai:
                    with patch.object(ai_judge.settings, "anthropic_api_key", "test-key"):
                        await detection.ingest_candidates(
                            session, artist, [raw_mid], download_covers=False
                        )
                        # Проверяем, что ИИ-судья был вызван
                        assert mock_ai.call_count == 1
                        print("🤖 ИИ-судья был успешно вызван для кандидата.")

            await session.commit()
            finding_mid = (await session.execute(
                select(Finding)
                .join(PlatformCandidate, Finding.candidate_id == PlatformCandidate.id)
                .where(PlatformCandidate.native_id == "12345678")
            )).scalar_one()

            print(f"📊 Статус mid-находки: {finding_mid.status}")
            print(f"📊 ИИ-Вердикт: {finding_mid.llm}")

            is_remix_status = finding_mid.status == "remix_review"
            is_verdict_ok = finding_mid.llm.get("verdict") == "remix"
            print(f"   Статус = remix_review (для ремиксов): {finding_mid.status} {ok(is_remix_status)}")
            print(f"   Вердикт ИИ записан в базу: {finding_mid.llm.get('verdict')} {ok(is_verdict_ok)}")

            success = is_matched and is_correct_stretch and is_high_score and is_status_ok and is_remix_status and is_verdict_ok
            if success:
                print("\n🎉 ВЕРИФИКАЦИЯ ВЕХИ M5 УСПЕШНО ПРОЙДЕНА!")
            else:
                print("\n❌ ВЕРИФИКАЦИЯ ВЕХИ M5 ПРОВАЛЕНА!")


if __name__ == "__main__":
    asyncio.run(main())
