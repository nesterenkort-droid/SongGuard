"""Сканер YouTube: поиск видео (Ярус 1) и мониторинг Topic-каналов через плейлисты (Ярус 0).

Все комментарии и лог-сообщения написаны на русском языке.
"""

import logging
import re
from datetime import datetime

import httpx

from app.config import settings
from app.scanners.base import RawCandidate

logger = logging.getLogger("trackguard.youtube_scan")

PLATFORM = "youtube"


class YouTubeAPIError(Exception):
    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


async def _get_youtube_api_key() -> str | None:
    """Возвращает следующий API ключ YouTube из списка настроенных, чередуя их."""
    keys_str = settings.youtube_api_key
    if not keys_str:
        return None
    keys = [k.strip() for k in keys_str.split(",") if k.strip()]
    if not keys:
        return None
    if len(keys) == 1:
        return keys[0]

    try:
        from app.redis_client import redis_client

        idx = await redis_client.incr("youtube_api_key_index")
        selected_key = keys[idx % len(keys)]
        return selected_key
    except Exception:
        return keys[0]


def parse_iso8601_duration(duration_str: str) -> int:
    """Парсит ISO 8601 длительность (например, PT3M25S) и возвращает миллисекунды."""
    if not duration_str:
        return 0
    pattern = re.compile(
        r"P(?:(?P<days>\d+)D)?T(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+)S)?"
    )
    match = pattern.match(duration_str)
    if not match:
        return 0
    parts = match.groupdict()
    days = int(parts.get("days") or 0)
    hours = int(parts.get("hours") or 0)
    minutes = int(parts.get("minutes") or 0)
    seconds = int(parts.get("seconds") or 0)
    total_seconds = days * 86400 + hours * 3600 + minutes * 60 + seconds
    return total_seconds * 1000


def parse_youtube_description(description: str | None) -> tuple[str | None, str | None]:
    """Парсит описание видео на YouTube для извлечения дистрибьютора/лейбла.

    Ищет шаблоны вроде 'Provided to YouTube by <Distributor>' и '℗ <Year> <Label>'.
    Возвращает кортеж (distributor, plabel).
    """
    if not description:
        return None, None

    distributor = None
    plabel = None

    # Ищем 'Provided to YouTube by <Distributor>' или на русском
    # 'Предоставлено YouTube компанией <Distributor>'
    prov_match = re.search(
        r"(?:Provided to YouTube by|"
        r"Предоставлено компании YouTube компанией|"
        r"Предоставлено YouTube компанией)\s+([^\n\r]+)",
        description,
        re.IGNORECASE,
    )
    if prov_match:
        distributor = prov_match.group(1).strip()

    # Ищем '℗ <Year> <Label>' (где Year - ровно 4 цифры и пробел) или просто '℗ <Label>'
    copy_match = re.search(
        r"℗\s*(\d{4})\s+([^\n\r]+)",
        description,
    )
    if copy_match:
        plabel = copy_match.group(2).strip()
    else:
        copy_match_no_year = re.search(
            r"℗\s*([^\n\r]+)",
            description,
        )
        if copy_match_no_year:
            plabel = copy_match_no_year.group(1).strip()

    return distributor, plabel


async def _fetch_video_details(
    client: httpx.AsyncClient, video_ids: list[str], api_key: str | None = None
) -> dict[str, dict]:
    """Запрашивает детальную информацию по списку ID видео (длительность, описание).

    Расход квоты: 1 единица.
    """
    key = api_key or await _get_youtube_api_key()
    if not key:
        logger.warning("YouTube API key не настроен.")
        return {}
    if not video_ids:
        return {}

    url = "https://www.googleapis.com/youtube/v3/videos"
    params = {
        "part": "snippet,contentDetails",
        "id": ",".join(video_ids),
        "key": key,
    }

    try:
        resp = await client.get(url, params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()

        details = {}
        for item in data.get("items", []):
            vid = item["id"]
            snippet = item.get("snippet", {})
            content_details = item.get("contentDetails", {})

            # Находим лучшую доступную миниатюру
            thumbs = snippet.get("thumbnails", {})
            thumb_url = None
            for key in ["maxres", "standard", "high", "medium", "default"]:
                if key in thumbs and thumbs[key].get("url"):
                    thumb_url = thumbs[key]["url"]
                    break

            description = snippet.get("description", "")
            distributor, plabel = parse_youtube_description(description)
            duration_ms = parse_iso8601_duration(content_details.get("duration", ""))

            published_at = None
            pub_str = snippet.get("publishedAt")
            if pub_str:
                try:
                    published_at = datetime.fromisoformat(pub_str.replace("Z", "+00:00"))
                except ValueError:
                    pass

            details[vid] = {
                "title": snippet.get("title", ""),
                "uploader": snippet.get("channelTitle", ""),
                "description_raw": description,
                "parsed_provider": distributor,
                "parsed_plabel": plabel,
                "duration_ms": duration_ms,
                "published_at": published_at,
                "thumb_url": thumb_url,
                "raw_json": item,
            }
        return details
    except httpx.HTTPStatusError as e:
        raise YouTubeAPIError(str(e), e.response.status_code) from e


async def search_tracks(query: str, limit: int = 10) -> list[RawCandidate]:
    """Tier 1: Поиск треков на YouTube.

    Расход квоты: 100 единиц (поиск) + 1 единица (детали).
    """
    key = await _get_youtube_api_key()
    if not key:
        logger.warning("YouTube API key не настроен.")
        return []

    async with httpx.AsyncClient() as client:
        url = "https://www.googleapis.com/youtube/v3/search"
        params = {
            "part": "snippet",
            "q": query,
            "type": "video",
            "maxResults": limit,
            "key": key,
        }

        try:
            resp = await client.get(url, params=params, timeout=15)
            resp.raise_for_status()
            data = resp.json()

            video_ids = [
                item["id"]["videoId"]
                for item in data.get("items", [])
                if item.get("id", {}).get("kind") == "youtube#video"
            ]
            if not video_ids:
                return []

            details = await _fetch_video_details(client, video_ids, api_key=key)

            candidates = []
            for vid in video_ids:
                det = details.get(vid)
                if not det:
                    continue
                candidates.append(
                    RawCandidate(
                        platform=PLATFORM,
                        native_id=vid,
                        title=det["title"],
                        url=f"https://www.youtube.com/watch?v={vid}",
                        uploader=det["uploader"],
                        description_raw=det["description_raw"],
                        parsed_provider=det["parsed_provider"],
                        parsed_plabel=det["parsed_plabel"],
                        published_at=det["published_at"].date() if det["published_at"] else None,
                        duration_ms=det["duration_ms"],
                        thumb_url=det["thumb_url"],
                        cover_url=det["thumb_url"],
                        raw_json=det["raw_json"],
                    )
                )
            return candidates
        except httpx.HTTPStatusError as e:
            raise YouTubeAPIError(str(e), e.response.status_code) from e


async def scan_playlist_items(
    playlist_id: str, known_video_ids: set[str], limit: int = 50
) -> list[RawCandidate]:
    """Tier 0: Мониторинг плейлиста загрузок канала (Topic-канала или пирата).

    Расход квоты: 1 единица (плейлист) + 1 единица (детали).
    """
    key = await _get_youtube_api_key()
    if not key:
        logger.warning("YouTube API key не настроен.")
        return []

    async with httpx.AsyncClient() as client:
        url = "https://www.googleapis.com/youtube/v3/playlistItems"
        params = {
            "part": "snippet",
            "playlistId": playlist_id,
            "maxResults": limit,
            "key": key,
        }

        try:
            video_ids = []
            page_token = None
            
            while True:
                if page_token:
                    params["pageToken"] = page_token
                    
                resp = await client.get(url, params=params, timeout=15)
                resp.raise_for_status()
                data = resp.json()

                for item in data.get("items", []):
                    vid = item.get("snippet", {}).get("resourceId", {}).get("videoId")
                    if vid and vid not in known_video_ids and vid not in video_ids:
                        video_ids.append(vid)

                page_token = data.get("nextPageToken")
                if not page_token or len(video_ids) >= limit:
                    break

            video_ids = video_ids[:limit]

            if not video_ids:
                return []

            details = await _fetch_video_details(client, video_ids, api_key=key)

            candidates = []
            for vid in video_ids:
                det = details.get(vid)
                if not det:
                    continue
                candidates.append(
                    RawCandidate(
                        platform=PLATFORM,
                        native_id=vid,
                        title=det["title"],
                        url=f"https://www.youtube.com/watch?v={vid}",
                        uploader=det["uploader"],
                        description_raw=det["description_raw"],
                        parsed_provider=det["parsed_provider"],
                        parsed_plabel=det["parsed_plabel"],
                        published_at=det["published_at"].date() if det["published_at"] else None,
                        duration_ms=det["duration_ms"],
                        thumb_url=det["thumb_url"],
                        cover_url=det["thumb_url"],
                        raw_json=det["raw_json"],
                    )
                )
            return candidates
        except httpx.HTTPStatusError as e:
            raise YouTubeAPIError(str(e), e.response.status_code) from e
