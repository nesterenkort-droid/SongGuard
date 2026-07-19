"""Perceptual hashing sanity checks."""

from PIL import Image

from app.services.images import dhash, hamming_hex, phash


def _gradient(seed: int) -> Image.Image:
    img = Image.new("RGB", (64, 64))
    px = img.load()
    for y in range(64):
        for x in range(64):
            px[x, y] = ((x * seed) % 256, (y * seed) % 256, ((x + y) * seed) % 256)
    return img


def test_hash_length_is_16_hex():
    img = _gradient(3)
    assert len(phash(img)) == 16
    assert len(dhash(img)) == 16


def test_self_distance_is_zero():
    img = _gradient(3)
    assert hamming_hex(phash(img), phash(img)) == 0
    assert hamming_hex(dhash(img), dhash(img)) == 0


def test_different_images_differ():
    a, b = _gradient(3), _gradient(11)
    assert hamming_hex(dhash(a), dhash(b)) > 0
