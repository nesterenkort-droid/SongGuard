"""iTunes lookup parsing (pure, no network)."""

from app.importers.itunes import parse_lookup

SAMPLE = {
    "results": [
        {"wrapperType": "artist", "artistName": "TWXNY", "artistId": 1718381786},
        {
            "wrapperType": "track",
            "kind": "song",
            "trackName": "HEAVENLY JUMPSTYLE",
            "artistName": "TWXNY, Sxilwix & Innxcence",
            "releaseDate": "2025-11-07T12:00:00Z",
            "trackTimeMillis": 114462,
            "trackId": 1859638952,
            "collectionId": 1859638749,
            "artworkUrl100": "https://x/100x100bb.jpg",
            "previewUrl": "https://p/preview.m4a",
        },
        {
            "wrapperType": "track",
            "kind": "song",
            "trackName": "HEAVENLY JUMPSTYLE (Slowed)",
            "artistName": "TWXNY",
            "releaseDate": "2025-11-07T12:00:00Z",
            "trackTimeMillis": 128478,
            "trackId": 1859639035,
            "collectionId": 1859638749,
            "artworkUrl100": "https://x/100x100bb.jpg",
        },
    ]
}


def test_parse_lookup_basic():
    artist = parse_lookup(SAMPLE, "1718381786")
    assert artist.name == "TWXNY"
    assert artist.apple_artist_id == "1718381786"
    assert len(artist.tracks) == 2


def test_parse_lookup_fields():
    artist = parse_lookup(SAMPLE, "1718381786")
    t0 = artist.tracks[0]
    assert t0.title == "HEAVENLY JUMPSTYLE"
    assert t0.duration_ms == 114462
    assert t0.apple_track_id == 1859638952
    assert t0.release_date.isoformat() == "2025-11-07"
    assert t0.cover_url.endswith("600x600bb.jpg")
    assert t0.preview_url == "https://p/preview.m4a"
    assert t0.source == "itunes"
