import asyncio
import logging
import os
import re
from typing import NamedTuple, Optional

logger = logging.getLogger(__name__)

PANAKO_JAR = "/app/bin/panako.jar"
ORIGINALS_DIR = "/data/originals"
DB_DIR = "/data/panako_dbs"


class MatchResult(NamedTuple):
    matched: bool
    track_id: Optional[int] = None
    true_stretch: Optional[float] = None
    score: Optional[int] = None
    matched_path: Optional[str] = None


def setup_panako_dir():
    """Self-healing setup for Panako storage directory using symlinks."""
    os.makedirs(DB_DIR, exist_ok=True)
    os.makedirs(ORIGINALS_DIR, exist_ok=True)

    home_dir = os.path.expanduser("~")
    panako_home = os.path.join(home_dir, ".panako")

    # If it's a regular directory instead of a symlink, back it up or delete it
    if os.path.exists(panako_home) and not os.path.islink(panako_home):
        logger.warning(f"{panako_home} is a regular directory, removing it to replace with symlink")
        import shutil
        try:
            shutil.rmtree(panako_home)
        except Exception as e:
            logger.error(f"Failed to remove regular directory {panako_home}: {e}")

    if not os.path.exists(panako_home):
        try:
            os.symlink(DB_DIR, panako_home)
            logger.info(f"Created symlink {panako_home} -> {DB_DIR}")
        except OSError as e:
            logger.warning(f"Failed to create symlink for Panako: {e}")


async def _run_panako_cmd(*args: str) -> tuple[int, str, str]:
    """Runs a Panako CLI command with standard Java memory configurations and options."""
    setup_panako_dir()

    cmd = [
        "java",
        "-Xmx512m",
        "--add-opens",
        "java.base/java.nio=ALL-UNNAMED",
        "--add-exports",
        "java.base/sun.nio.ch=ALL-UNNAMED",
        "-jar",
        PANAKO_JAR,
    ] + list(args)

    logger.debug(f"Running Panako command: {' '.join(cmd)}")
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=5.0)
    except asyncio.TimeoutError:
        proc.kill()
        return 1, "", "Panako command timed out"
    return proc.returncode, stdout.decode("utf-8", errors="replace"), stderr.decode("utf-8", errors="replace")


async def store_reference(track_id: int, original_file_path: str) -> bool:
    """Ingests a reference audio file into Panako database.

    Generates 3 speed variants (0.8, 1.0, 1.25) to cover pitch/time-stretch changes.
    """
    if not os.path.exists(original_file_path):
        logger.error(f"Original file not found: {original_file_path}")
        return False

    setup_panako_dir()
    speeds = [0.8, 1.0, 1.25]
    success_all = True

    for speed in speeds:
        variant_path = os.path.join(ORIGINALS_DIR, f"{track_id}_{speed:.2f}.wav")

        # 1. Generate speed variant using ffmpeg
        if speed == 1.0:
            # Just resample to 16kHz mono WAV
            ffmpeg_cmd = [
                "ffmpeg",
                "-y",
                "-i",
                original_file_path,
                "-ar",
                "16000",
                "-ac",
                "1",
                variant_path,
            ]
        else:
            # Change speed using asetrate and resample
            ffmpeg_cmd = [
                "ffmpeg",
                "-y",
                "-i",
                original_file_path,
                "-af",
                f"asetrate=16000*{speed},aresample=16000",
                variant_path,
            ]

        logger.debug(f"Generating speed variant {speed}: {' '.join(ffmpeg_cmd)}")
        proc = await asyncio.create_subprocess_exec(
            *ffmpeg_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            logger.error(f"ffmpeg failed to generate speed variant {speed}: {stderr.decode()}")
            success_all = False
            continue

        # Verify that the variant file was actually created
        if not os.path.exists(variant_path):
            logger.error(f"Variant file not created: {variant_path}")
            success_all = False
            continue

        # 2. Store variant in Panako
        code, stdout, stderr = await _run_panako_cmd("store", variant_path)
        if code != 0:
            logger.error(f"Panako failed to store variant {variant_path}: {stderr}")
            success_all = False
        else:
            logger.info(f"Successfully stored reference variant {variant_path} in Panako")
    return success_all


async def query_candidate(candidate_file_path: str) -> MatchResult:
    """Queries an audio candidate against the Panako database.

    Parses the semicolon-separated query output and resolves matching reference file.
    """
    if not os.path.exists(candidate_file_path):
        logger.error(f"Candidate file not found: {candidate_file_path}")
        return MatchResult(matched=False)

    code, stdout, stderr = await _run_panako_cmd("query", candidate_file_path)
    logger.debug(f"Panako query stdout: {stdout}")
    if code != 0:
        logger.error(f"Panako query failed for {candidate_file_path}: {stderr}")
        return MatchResult(matched=False)

    # Parse Panako output lines
    lines = [line.strip() for line in stdout.split("\n") if line.strip()]
    for line in lines:
        # Skip header or irrelevant lines
        if line.startswith("Index;") or line.startswith("Index ") or "Query path" in line:
            continue
        parts = [p.strip() for p in line.split(";")]
        # Panako output columns:
        # 0: Index, 1: Total, 2: Query path, 3: Query start, 4: Query stop,
        # 5: Match path, 6: Match id, 7: Match start, 8: Match stop,
        # 9: Match score, 10: Time factor, 11: Frequency factor, 12: Seconds with match
        if len(parts) < 6:
            continue
        match_path = parts[5]
        if not match_path or match_path == "null":
            continue
        # Extract score if present (typically column 9)
        score = 0
        if len(parts) > 9:
            try:
                score = int(parts[9])
            except (ValueError, IndexError):
                score = 0
        # Extract time factor if present (typically column 10 like "1.000 %")
        time_factor = 1.0
        if len(parts) > 10:
            tf = parts[10].replace("%", "").strip()
            try:
                time_factor = float(tf)
            except ValueError:
                time_factor = 1.0
        # Parse filename to obtain track_id and variant speed
        filename = os.path.basename(match_path)
        m = re.match(r"^(\d+)_(\d+\.\d+)\.wav$", filename)
        if m:
            track_id = int(m.group(1))
            variant_speed = float(m.group(2))
            true_stretch = variant_speed * time_factor
            logger.info(
                f"Audio match found: Track {track_id} at {true_stretch:.3f}x speed "
                f"(variant {variant_speed}x * factor {time_factor:.3f}), score: {score}"
            )
            return MatchResult(
                matched=True,
                track_id=track_id,
                true_stretch=true_stretch,
                score=score,
                matched_path=match_path,
            )
        # Fallback when filename pattern is unexpected
        logger.info(f"Audio match found (unparsed path): {match_path}, score: {score}")
        return MatchResult(matched=True, track_id=None, true_stretch=None, score=score, matched_path=match_path)
    # No match found
    return MatchResult(matched=False)


async def clear_database() -> bool:
    """Clears all entries from Panako database."""
    code, _, stderr = await _run_panako_cmd("clear")
    if code != 0:
        logger.error(f"Failed to clear Panako database: {stderr}")
        return False
    logger.info("Panako database cleared successfully")
    return True
