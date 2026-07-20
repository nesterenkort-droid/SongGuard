import asyncio
import logging
import os
import tempfile

import httpx
import yt_dlp
from yt_dlp.utils import download_range_func

logger = logging.getLogger(__name__)


async def convert_to_wav_16k_mono(src_path: str, dest_path: str) -> bool:
    """Converts any input audio file to WAV 16kHz mono format for Panako using ffmpeg."""
    if not os.path.exists(src_path):
        logger.error(f"Source audio file not found: {src_path}")
        return False

    os.makedirs(os.path.dirname(dest_path), exist_ok=True)

    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        src_path,
        "-ar",
        "16000",
        "-ac",
        "1",
        dest_path,
    ]

    logger.debug(f"Running ffmpeg conversion: {' '.join(cmd)}")
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()

    if proc.returncode != 0:
        logger.error(f"ffmpeg conversion failed: {stderr.decode()}")
        return False

    return True


async def download_preview_audio(url: str, dest_path: str) -> bool:
    """Downloads a direct audio link (e.g., iTunes/Spotify 30s preview) and converts it to WAV."""
    os.makedirs(os.path.dirname(dest_path), exist_ok=True)

    with tempfile.TemporaryDirectory() as tmpdir:
        temp_file = os.path.join(tmpdir, "preview_temp")

        # 1. Download file via HTTP
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.get(url, follow_redirects=True)
                if response.status_code != 200:
                    logger.error(
                        "Failed to download preview from %s, status code: %s",
                        url, response.status_code,
                    )
                    return False

                with open(temp_file, "wb") as f:
                    f.write(response.content)
        except Exception as e:
            logger.error(f"HTTP error downloading preview: {e}")
            return False

        # 2. Convert to WAV 16kHz mono
        return await convert_to_wav_16k_mono(temp_file, dest_path)


async def download_youtube_audio(youtube_url: str, dest_path: str) -> bool:
    """Downloads first 60 seconds of YouTube audio using yt-dlp and converts to WAV."""
    os.makedirs(os.path.dirname(dest_path), exist_ok=True)

    with tempfile.TemporaryDirectory() as tmpdir:
        # Use a template path for yt-dlp download
        temp_output_path = os.path.join(tmpdir, "youtube_temp.%(ext)s")
        temp_file_wildcard = os.path.join(tmpdir, "youtube_temp.*")

        ydl_opts = {
            "format": "bestaudio/best",
            "outtmpl": temp_output_path,
            "quiet": True,
            "no_warnings": True,
            "download_ranges": download_range_func(None, [(0.0, 60.0)]),
            "force_keyframes_at_cuts": True,
            "socket_timeout": 30,
        }

        # Run yt-dlp in a thread pool to keep loop non-blocking
        loop = asyncio.get_running_loop()
        try:
            def _download():
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    ydl.download([youtube_url])

            await loop.run_in_executor(None, _download)
        except Exception as e:
            logger.error(f"yt-dlp download failed for {youtube_url}: {e}")
            return False

        # Find the actual downloaded file (since extension varies: .m4a, .webm, etc.)
        import glob
        downloaded_files = glob.glob(temp_file_wildcard)
        if not downloaded_files:
            logger.error(f"No downloaded file found for {youtube_url} in {tmpdir}")
            return False

        downloaded_file = downloaded_files[0]

        # Convert to WAV 16kHz mono
        success = await convert_to_wav_16k_mono(downloaded_file, dest_path)
        return success
