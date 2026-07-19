"""Best-effort Apple Music ℗-label scraper.

The iTunes Search API returns no label, and the official Apple Music API costs $99/yr
(out of budget, PLAN.md §3). So for Apple candidates we scrape the ℗ copyright line off
the public album web page. This is best-effort: parsing is pure/testable, the fetch
never raises, and detection degrades gracefully to metadata+cover when it fails.
"""

import re

import httpx

# Apple embeds page data as JSON; the copyright text shows up as "copyright":"℗ ..."
_JSON_COPYRIGHT_RE = re.compile(r'"copyright"\s*:\s*"([^"]{2,160})"')
# Fallback: a bare ℗ line anywhere in the markup.
_PLINE_RE = re.compile(r"[℗©]\s*([^<\"\n]{2,160})")

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)


def parse_label_from_html(html: str) -> str | None:
    """Extract the ℗ label string from an Apple Music album page (pure)."""
    m = _JSON_COPYRIGHT_RE.search(html)
    if m:
        return _clean(m.group(1))
    m = _PLINE_RE.search(html)
    if m:
        return _clean(m.group(0))
    return None


def _clean(s: str) -> str:
    s = s.replace("\\u2117", "℗").replace("\\u00a9", "©")
    s = re.sub(r"\s+", " ", s).strip()
    return s


async def fetch_label(client: httpx.AsyncClient, album_url: str) -> str | None:
    """Fetch an Apple Music album page and scrape its ℗ label. Never raises."""
    if not album_url:
        return None
    try:
        resp = await client.get(
            album_url, headers={"User-Agent": _UA, "Accept-Language": "en-US,en;q=0.9"},
            timeout=20, follow_redirects=True,
        )
        resp.raise_for_status()
        return parse_label_from_html(resp.text)
    except Exception:  # noqa: BLE001 - scraping is best-effort
        return None
