"""Perceptual image hashing for cover art (pHash + dHash), pure Pillow + numpy.

Implemented here (rather than via imagehash/scipy) to keep the image lean and the
build fast. Hashes are 64-bit, stored as 16-char hex strings. Hamming distance over
these is the M2 "same cover" signal.
"""

import io
import os

import httpx
import numpy as np
from PIL import Image

_HASH_SIZE = 8


def _bits_to_hex(bits: np.ndarray) -> str:
    value = 0
    for bit in bits.flatten():
        value = (value << 1) | int(bit)
    return f"{value:016x}"


def dhash(image: Image.Image) -> str:
    """Difference hash: compare horizontally adjacent pixels of a 9x8 grayscale."""
    img = image.convert("L").resize((_HASH_SIZE + 1, _HASH_SIZE), Image.LANCZOS)
    arr = np.asarray(img, dtype=np.int16)
    diff = arr[:, 1:] > arr[:, :-1]
    return _bits_to_hex(diff)


def _dct_matrix(n: int) -> np.ndarray:
    x = np.arange(n)
    k = x.reshape(-1, 1)
    m = np.cos(np.pi * (2 * x + 1) * k / (2 * n))
    m[0] *= 1 / np.sqrt(2)
    return m * np.sqrt(2 / n)


_DCT32 = _dct_matrix(32)


def phash(image: Image.Image) -> str:
    """Perceptual hash: DCT of a 32x32 grayscale, low-freq 8x8 vs. its median."""
    img = image.convert("L").resize((32, 32), Image.LANCZOS)
    arr = np.asarray(img, dtype=np.float64)
    dct = _DCT32 @ arr @ _DCT32.T
    low = dct[:_HASH_SIZE, :_HASH_SIZE]
    med = np.median(low)
    return _bits_to_hex(low > med)


def hamming_hex(a: str, b: str) -> int:
    """Bit distance between two hex hashes (0 = identical)."""
    return bin(int(a, 16) ^ int(b, 16)).count("1")


def hash_bytes(content: bytes) -> tuple[str, str]:
    """Return (phash, dhash) for raw image bytes."""
    with Image.open(io.BytesIO(content)) as img:
        img.load()
        return phash(img), dhash(img)


def hash_bytes_cropped(content: bytes) -> tuple[str, str]:
    """Загружает изображение, обрезает его до квадрата 1:1 по центру и возвращает (phash, dhash)."""
    with Image.open(io.BytesIO(content)) as img:
        img.load()
        width, height = img.size
        min_dim = min(width, height)
        left = (width - min_dim) // 2
        top = (height - min_dim) // 2
        right = left + min_dim
        bottom = top + min_dim
        cropped = img.crop((left, top, right, bottom))
        return phash(cropped), dhash(cropped)


def save_cover(content: bytes, dest_dir: str, stem: str) -> str:
    """Persist cover bytes; return the stored filename (basename)."""
    os.makedirs(dest_dir, exist_ok=True)
    try:
        with Image.open(io.BytesIO(content)) as img:
            fmt = (img.format or "JPEG").lower()
    except Exception:  # noqa: BLE001
        fmt = "jpeg"
    ext = "png" if fmt == "png" else "jpg"
    filename = f"{stem}.{ext}"
    with open(os.path.join(dest_dir, filename), "wb") as f:
        f.write(content)
    return filename


async def fetch(client: httpx.AsyncClient, url: str) -> bytes:
    resp = await client.get(url, timeout=20)
    resp.raise_for_status()
    return resp.content
