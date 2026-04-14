"""
Notification Builder — Discord V2 Component payloads for chapter release alerts.

Constructs raw JSON component payloads using the IS_COMPONENTS_V2 flag (32768).
Follows the same raw-HTTP pattern used by view.py.
"""
from datetime import datetime, timezone
from typing import Any

# Platform accent colors (matching view.py COLORS)
# Platform accent colors (matching view.py COLORS)
PLATFORM_COLORS = {
    "piccoma": 0xffd600,
    "mecha": 0xe67e22,
    "jumptoon": 0x9b59b6,
}

# Platform display names
PLATFORM_NAMES = {
    "piccoma": "PICCOMA",
    "mecha": "MECHA",
    "jumptoon": "JUMPTOON",
}

# Platform Emojis (Custom IDs from the guild)
PLATFORM_EMOJIS = {
    "piccoma": "<:Piccoma:1478368704164134912>", 
    "mecha": "<:Mechacomic:1478369141957333083>",
    "jumptoon": "<:Jumptoon:1478367963928068168>",
}


def build_notification_payload(
    *,
    platform: str,
    role_id: int | None,
    series_title: str,
    custom_title: str | None,
    poster_url: str | None,
    series_url: str,
    series_id: str,
    notification_id: int,
    chapter_id: str | None = None,
    chapter_number: str | None = None, # 🟢 NEW: Actual notation (e.g. 第38話)
    use_attachment_proxy: bool = False, # 🟢 NEW: Use attachment:// instead of URL
) -> dict:
    """
    Builds the full Discord message payload (flags + components) for a new chapter notification.

    Returns a dict ready to be sent via raw HTTP POST/PATCH.
    """
    platform_key = platform.lower()
    accent_color = PLATFORM_COLORS.get(platform_key, 0x2b2d31)
    platform_display = PLATFORM_NAMES.get(platform_key, platform.upper())

    # --- Header: role ping + platform ---
    # Role ID provided by user example: 1419398048152551514
    role_mention = f"<@&{role_id}>" if role_id else "@Updates"
    platform_emoji = PLATFORM_EMOJIS.get(platform_key, "📖")
    
    # 🟢 New Layout: Corrected link format per user request
    header_text = f"New Chapter {role_mention} of **[ {platform_emoji} [{platform_display}]({series_url}) ]**"

    # --- Poster Logic ---
    final_poster_url = poster_url
    if use_attachment_proxy:
        final_poster_url = "attachment://poster.png"

    # --- Title block ---
    subtitle = f"-# **{chapter_number}**" if chapter_number else ""
    if custom_title:
        title_text = f"## {custom_title}\n{subtitle}"
    else:
        title_text = f"## {series_title}\n{subtitle}"

    # --- Footer IDs ---
    footer_text = f"-# N-ID: {notification_id} | S-ID: {series_id}"

    # --- Assemble inner components ---
    inner: list[dict[str, Any]] = []

    # 1. Header
    inner.append({"type": 10, "content": header_text})
    inner.append({"type": 14, "divider": True, "spacing": 1}) # Separator

    # 3. Poster (Media Gallery)
    if final_poster_url:
        inner.append({
            "type": 12,
            "items": [{"media": {"url": final_poster_url}}]
        })
        inner.append({"type": 14, "divider": True, "spacing": 1}) # Separator

    # 4. Title block
    inner.append({"type": 10, "content": title_text})
    inner.append({"type": 14, "divider": True, "spacing": 1}) # Separator

    # 5. Buttons (Series Link + Direct Download)
    action_buttons = [
        {
            "type": 2,        # Button
            "style": 5,       # Link
            "label": "Series Page",
            "url": series_url,
        }
    ]
    
    if chapter_id:
        action_buttons.append({
            "type": 2,
            "style": 3, # Success (Green)
            "label": "Download",
            "emoji": {"id": "1486828932425846994", "name": "download_all"}, # Updated Name
            "custom_id": f"discovery:download_chapter:{platform_key}:{series_id}:{chapter_id}",
        })

    inner.append({
        "type": 1,  # Action Row
        "components": action_buttons
    })
    inner.append({"type": 14, "divider": True, "spacing": 1}) # Separator

    # 6. Footer
    inner.append({"type": 10, "content": footer_text})

    # --- Wrap in Container ---
    components = [{
        "type": 17,
        "accent_color": accent_color,
        "components": inner,
    }]

    payload = {
        "flags": 32768,
        "components": components,
    }

    # Enable role pings
    if role_id:
        payload["allowed_mentions"] = {"roles": [str(role_id)]}

    return payload


def build_new_series_notification_payload(
    *,
    platform: str,
    series_title: str,
    poster_url: str | None,
    series_url: str,
    series_id: str,
) -> dict:
    """
    Builds the Discord message payload for a NEW series premiere.
    Updated to Premium V2 Layout with separators.
    """
    platform_key = platform.lower()
    accent_color = PLATFORM_COLORS.get(platform_key, 0x2b2d31)
    platform_display = PLATFORM_NAMES.get(platform_key, platform.upper())
    platform_emoji = PLATFORM_EMOJIS.get(platform_key, "🆕")

    # --- 1. Header (Handled in components section) ---

    # --- 2. Title ---
    title_text = f"## {series_title}"

    # --- 3. Footer ---
    # S-ID: | Detected at:
    now_utc = datetime.now(timezone.utc).strftime('%H:%M UTC')
    footer_text = f"-# S-ID: {series_id} | Detected at: {now_utc}"

    # --- Assemble inner components ---
    inner: list[dict[str, Any]] = []

    # A. Header (Single line normal style)
    inner.append({"type": 10, "content": f"-# New Series @Updates of **[ {platform_emoji} [{platform_display}]({series_url}) ]**"})
    inner.append({"type": 14, "divider": True, "spacing": 1})

    # B. Poster + Divider
    if poster_url:
        inner.append({
            "type": 12,
            "items": [{"media": {"url": poster_url}}]
        })
        inner.append({"type": 14, "divider": True, "spacing": 1})

    # C. Title + Divider
    inner.append({"type": 10, "content": title_text})
    inner.append({"type": 14, "divider": True, "spacing": 1})

    # D. Buttons (Preview + Download All) + Divider
    # [Preview] (Link) and [Download All] (Callback)
    inner.append({
        "type": 1,
        "components": [
            {
                "type": 2,
                "style": 2, # Secondary (Grey)
                "label": "Preview",
                "emoji": {"id": "1486813709698465832", "name": "Preview"},
                "custom_id": f"discovery:preview_first:{platform_key}:{series_id}",
            },
            {
                "type": 2,
                "style": 3, # Success (Green)
                "label": "Download All",
                "emoji": {"id": "1486828932425846994", "name": "download_all"},
                "custom_id": f"discovery:download_all:{platform_key}:{series_id}",
            }
        ]
    })
    inner.append({"type": 14, "divider": True, "spacing": 1})

    # E. Footer
    inner.append({"type": 10, "content": footer_text})

    # --- Wrap in Container ---
    components = [{
        "type": 17,
        "accent_color": accent_color,
        "components": inner,
    }]

    return {
        "flags": 32768,
        "components": components,
    }
