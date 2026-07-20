import math
import os
import shutil
import struct
import tempfile
import wave

import pytest

from app.services import audio_downloader, panako

# Skipped implicitly if ffmpeg/JRE are missing; guaranteed present inside Docker.
pytestmark = pytest.mark.asyncio


async def _generate_beep_audio(dest_path: str, duration_sec: int = 15, speed: float = 1.0):
    """Synthesizes a rich arpeggio melody with frequency sweeps and decay envelopes.

    This ensures plenty of spectrotemporal peaks and transient onsets for Panako's indexer.
    """
    sample_rate = 16000
    num_samples = int(duration_sec * sample_rate)
    notes = [
        220.0, 275.0, 330.0, 440.0, 550.0, 660.0, 880.0,
        1100.0, 660.0, 550.0, 440.0, 330.0, 275.0, 220.0,
    ]
    
    original_samples = []
    note_duration_samples = int(0.5 * sample_rate)
    
    for i in range(num_samples):
        note_idx = (i // note_duration_samples) % len(notes)
        freq = notes[note_idx]
        time_in_note = (i % note_duration_samples) / sample_rate
        # Add frequency sweep (chirp) to enrich the constellation map
        sweep_freq = freq * (1.0 + 0.2 * math.sin(2 * math.pi * 5.0 * time_in_note))
        
        # Constant amplitude to maximize fingerprint density
        val = math.sin(2 * math.pi * sweep_freq * (i / sample_rate)) * 16383
        original_samples.append(int(val))
        
    if speed == 1.0:
        samples = original_samples
    else:
        # Linear interpolation resampling for precise time stretching & pitch shifting
        samples = []
        stretched_length = int(num_samples / speed)
        for j in range(stretched_length):
            orig_pos = j * speed
            idx1 = int(orig_pos)
            idx2 = min(idx1 + 1, len(original_samples) - 1)
            frac = orig_pos - idx1
            if idx1 < len(original_samples):
                val = (1.0 - frac) * original_samples[idx1] + frac * original_samples[idx2]
                samples.append(int(val))
            else:
                samples.append(0)
                
    # Write WAV file
    os.makedirs(os.path.dirname(dest_path), exist_ok=True)
    with wave.open(dest_path, "w") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        data = struct.pack("<" + "h" * len(samples), *samples)
        w.writeframes(data)


async def test_panako_lifecycle():
    # 1. Clear database
    assert await panako.clear_database() is True
    
    # Clean up any existing files in originals and cache to avoid "resource already stored" skips
    if os.path.exists(panako.ORIGINALS_DIR):
        shutil.rmtree(panako.ORIGINALS_DIR)
    os.makedirs(panako.ORIGINALS_DIR, exist_ok=True)

    cache_dir = os.path.expanduser("~/.panako/dbs/olaf_cache")
    if os.path.exists(cache_dir):
        shutil.rmtree(cache_dir)
    os.makedirs(cache_dir, exist_ok=True)

    with tempfile.TemporaryDirectory() as tmpdir:
        original_path = os.path.join(tmpdir, "original.wav")
        query_path = os.path.join(tmpdir, "query.wav")
        query_slow_path = os.path.join(tmpdir, "query_slow.wav")

        # Generate 15-second multi-tone original audio
        await _generate_beep_audio(original_path, duration_sec=15)

        # 2. Store reference track (ID = 999)
        # This will generate variants: 999_0.80.wav, 999_1.00.wav, 999_1.25.wav
        assert await panako.store_reference(999, original_path) is True

        # Generate exact query audio (copy of original)
        await _generate_beep_audio(query_path, duration_sec=15)

        # 3. Query candidate (expect 1.0x speed match)
        result = await panako.query_candidate(query_path)
        assert result.matched is True
        assert result.track_id == 999
        assert abs(result.true_stretch - 1.0) < 0.05
        assert result.score > 100

        # Generate slowed query audio (0.8x speed)
        await _generate_beep_audio(query_slow_path, duration_sec=15, speed=0.8)

        # 4. Query candidate (expect 0.8x speed match against the 0.80 variant)
        result_slow = await panako.query_candidate(query_slow_path)
        assert result_slow.matched is True
        assert result_slow.track_id == 999
        assert abs(result_slow.true_stretch - 0.8) < 0.05
        assert result_slow.score > 100


async def test_audio_converter():
    with tempfile.TemporaryDirectory() as tmpdir:
        src = os.path.join(tmpdir, "src.wav")
        dest = os.path.join(tmpdir, "dest.wav")

        await _generate_beep_audio(src, duration_sec=3)
        assert await audio_downloader.convert_to_wav_16k_mono(src, dest) is True
        assert os.path.exists(dest)
