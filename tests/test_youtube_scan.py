"""Тесты для сканера YouTube и связанных функций обработки изображений."""

import io
from unittest.mock import patch

import pytest
from PIL import Image

from app.scanners.youtube_scan import (
    parse_iso8601_duration,
    parse_youtube_description,
    scan_playlist_items,
    search_tracks,
)
from app.services.images import dhash, hash_bytes_cropped, phash


def test_parse_iso8601_duration():
    assert parse_iso8601_duration("PT3M25S") == (3 * 60 + 25) * 1000
    assert parse_iso8601_duration("PT1H2M10S") == (1 * 3600 + 2 * 60 + 10) * 1000
    assert parse_iso8601_duration("PT15S") == 15 * 1000
    assert parse_iso8601_duration("PT2H") == 2 * 3600 * 1000
    assert parse_iso8601_duration("P1DT1H") == (24 * 3600 + 1 * 3600) * 1000
    assert parse_iso8601_duration("") == 0
    assert parse_iso8601_duration("invalid") == 0


def test_parse_youtube_description():
    desc_en = (
        "Provided to YouTube by DistroKid\n\n"
        "HEAVENLY JUMPSTYLE · TWXNY\n\n"
        "℗ 13207436 Records DK"
    )
    dist, label = parse_youtube_description(desc_en)
    assert dist == "DistroKid"
    assert label == "13207436 Records DK"

    desc_ru = "Предоставлено компании YouTube компанией DistroKid\n\n℗ 2026 13207436 Records DK"
    dist_ru, label_ru = parse_youtube_description(desc_ru)
    assert dist_ru == "DistroKid"
    assert label_ru == "13207436 Records DK"

    assert parse_youtube_description("") == (None, None)
    assert parse_youtube_description(None) == (None, None)


def test_hash_bytes_cropped():
    # Создаем 16:9 картинку (96x54)
    img = Image.new("RGB", (96, 54), color="blue")
    
    # Рисуем красный квадрат по центру (54x54)
    px = img.load()
    for y in range(54):
        for x in range(21, 75):
            px[x, y] = (255, 0, 0)
            
    # Записываем в байты
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    content = buf.getvalue()
    
    # Хешируем обрезанный вариант (он должен быть полностью красным квадратом 1:1)
    ph1, dh1 = hash_bytes_cropped(content)
    
    # Создаем сразу чистый красный квадрат 54x54
    red_square = Image.new("RGB", (54, 54), color="red")
    ph2 = phash(red_square)
    dh2 = dhash(red_square)
    
    # Хеши должны совпадать (или быть очень близки), так как обрезка убрала синие края
    assert ph1 == ph2
    assert dh1 == dh2


@pytest.mark.asyncio
async def test_search_tracks():
    mock_search_response = {
        "items": [
            {
                "id": {"kind": "youtube#video", "videoId": "NzL0wDrGtYM"},
                "snippet": {
                    "title": "HEAVENLY JUMPSTYLE (Slowed)",
                    "channelTitle": "TWXNY - Topic",
                    "publishedAt": "2026-07-13T00:00:00Z",
                    "thumbnails": {
                        "default": {"url": "http://example.com/thumb.jpg"}
                    }
                }
            }
        ]
    }
    
    mock_video_response = {
        "items": [
            {
                "id": "NzL0wDrGtYM",
                "snippet": {
                    "title": "HEAVENLY JUMPSTYLE (Slowed)",
                    "channelTitle": "TWXNY - Topic",
                    "description": "Provided to YouTube by DistroKid\n\n℗ 13207436 Records DK",
                    "publishedAt": "2026-07-13T00:00:00Z",
                    "thumbnails": {
                        "default": {"url": "http://example.com/thumb.jpg"}
                    }
                },
                "contentDetails": {
                    "duration": "PT2M23S"
                }
            }
        ]
    }

    from unittest.mock import MagicMock
    async def mock_get(url, *args, **kwargs):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        if "search" in str(url):
            mock_resp.json.return_value = mock_search_response
        elif "videos" in str(url):
            mock_resp.json.return_value = mock_video_response
        return mock_resp

    with patch("httpx.AsyncClient.get", side_effect=mock_get):
        with patch("app.config.settings.youtube_api_key", "test-key"):
            results = await search_tracks("TWXNY HEAVENLY JUMPSTYLE", limit=1)
            
            assert len(results) == 1
            cand = results[0]
            assert cand.native_id == "NzL0wDrGtYM"
            assert cand.title == "HEAVENLY JUMPSTYLE (Slowed)"
            assert cand.uploader == "TWXNY - Topic"
            assert cand.parsed_provider == "DistroKid"
            assert cand.parsed_plabel == "13207436 Records DK"
            assert cand.duration_ms == 143000
            assert cand.thumb_url == "http://example.com/thumb.jpg"


@pytest.mark.asyncio
async def test_scan_playlist_items():
    mock_playlist_response = {
        "items": [
            {
                "snippet": {
                    "resourceId": {"kind": "youtube#video", "videoId": "NzL0wDrGtYM"}
                }
            }
        ]
    }
    
    mock_video_response = {
        "items": [
            {
                "id": "NzL0wDrGtYM",
                "snippet": {
                    "title": "HEAVENLY JUMPSTYLE (Slowed)",
                    "channelTitle": "TWXNY - Topic",
                    "description": "Provided to YouTube by DistroKid\n\n℗ 13207436 Records DK",
                    "publishedAt": "2026-07-13T00:00:00Z",
                    "thumbnails": {
                        "default": {"url": "http://example.com/thumb.jpg"}
                    }
                },
                "contentDetails": {
                    "duration": "PT2M23S"
                }
            }
        ]
    }

    from unittest.mock import MagicMock
    async def mock_get(url, *args, **kwargs):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        if "playlistItems" in str(url):
            mock_resp.json.return_value = mock_playlist_response
        elif "videos" in str(url):
            mock_resp.json.return_value = mock_video_response
        return mock_resp

    with patch("httpx.AsyncClient.get", side_effect=mock_get):
        with patch("app.config.settings.youtube_api_key", "test-key"):
            results = await scan_playlist_items(
                "UU-uploads-playlist-id", known_video_ids=set(), limit=1
            )
            
            assert len(results) == 1
            cand = results[0]
            assert cand.native_id == "NzL0wDrGtYM"
            assert cand.title == "HEAVENLY JUMPSTYLE (Slowed)"


@pytest.mark.asyncio
async def test_youtube_key_rotation():
    from app.scanners.youtube_scan import _get_youtube_api_key
    with patch("app.config.settings.youtube_api_key", "key1,key2,key3"):
        k1 = await _get_youtube_api_key()
        k2 = await _get_youtube_api_key()
        k3 = await _get_youtube_api_key()
        k4 = await _get_youtube_api_key()

        assert {k1, k2, k3} == {"key1", "key2", "key3"}
        assert k4 == k1
