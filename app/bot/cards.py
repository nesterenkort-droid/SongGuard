"""Finding card rendering: pure text + button layout, no aiogram/network dependency.

Kept framework-free so the layout can be unit-tested directly. `app/bot/main.py`
wraps `keyboard_rows` into an aiogram `InlineKeyboardMarkup` at send time.

Callback data is short and stable: `f:<action>:<finding_id>` (Telegram caps callback
data at 64 bytes; finding ids are small ints, well within budget).
"""

from dataclasses import dataclass

from app.config import settings
from app.models import Finding, PlatformCandidate, Track

ACTION_CONFIRM = "confirm"
ACTION_DISMISS = "dismiss"
ACTION_TOLERATE = "tolerate"
ACTION_WHITELIST_CHANNEL = "wl"

STATUS_EMOJI = {
    "detected": "🆕",
    "pending_review": "👀",
    "remix_review": "🎛",
    "confirmed": "🚨",
    "dismissed": "✗",
    "tolerated": "🕊",
}


@dataclass
class ButtonSpec:
    text: str
    callback_data: str | None = None
    url: str | None = None


def callback_data(action: str, finding_id: int) -> str:
    return f"f:{action}:{finding_id}"


def parse_callback_data(data: str) -> tuple[str, int] | None:
    parts = data.split(":")
    if len(parts) != 3 or parts[0] != "f":
        return None
    try:
        return parts[1], int(parts[2])
    except ValueError:
        return None


def build_finding_text(
    finding: Finding, cand: PlatformCandidate, track: Track, artist_name: str
) -> str:
    """Human-readable RU card body: what we found, against what, and why (PLAN.md §9)."""
    emoji = STATUS_EMOJI.get(finding.status, "🆕")
    lines = [
        f"{emoji} <b>{_esc(cand.title)}</b>",
        f"площадка: {cand.platform}" + (f" · {_esc(cand.uploader)}" if cand.uploader else ""),
        f"наш трек: «{_esc(track.title)}» ({_esc(artist_name)})",
    ]
    if cand.parsed_plabel or cand.parsed_provider:
        lines.append(f"лейбл/дистрибьютор: {_esc(cand.parsed_plabel or cand.parsed_provider)}")
    if cand.published_at:
        lines.append(f"дата: {cand.published_at.date().isoformat()}")
    lines.append(f"\nскор: <b>{finding.score}</b> ({finding.band})")
    for s in finding.signals or []:
        lines.append(f"• {s['label']} (+{s['contribution']})")
    return "\n".join(lines)


def build_finding_buttons(finding: Finding) -> list[list[ButtonSpec]]:
    row1 = [
        ButtonSpec("✓ Пиратка", callback_data=callback_data(ACTION_CONFIRM, finding.id)),
        ButtonSpec("✗ Ложное", callback_data=callback_data(ACTION_DISMISS, finding.id)),
        ButtonSpec("🕊 Разрешить", callback_data=callback_data(ACTION_TOLERATE, finding.id)),
    ]
    row2 = [ButtonSpec("🌐 На сайте", url=f"{settings.base_url}/findings")]
    return [row1, row2]


def build_finding_card(
    finding: Finding, cand: PlatformCandidate, track: Track, artist_name: str
) -> tuple[str, list[list[ButtonSpec]]]:
    return build_finding_text(finding, cand, track, artist_name), build_finding_buttons(finding)


def _esc(s: str | None) -> str:
    if not s:
        return ""
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
