"""
Notification Builder — Discord V2 Component payloads for chapter release alerts.

Constructs raw JSON component payloads using the IS_COMPONENTS_V2 flag (32768).
Follows the same raw-HTTP pattern used by view.py.
"""
from datetime import datetime, timezone

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
) -> dict:
    """
    Builds the full Discord message payload (flags + components) for a new chapter notification.

    Returns a dict ready to be sent via raw HTTP POST/PATCH.
    """
    platform_key = platform.lower()
    accent_color = PLATFORM_COLORS.get(platform_key, 0x2b2d31)
    platform_display = PLATFORM_NAMES.get(platform_key, platform.upper())

    now = datetime.now(timezone.utc)
    release_date = now.strftime("%d %b, %Y")
    release_time = now.strftime("%H:%M UTC")

    # --- Header: role ping + platform ---
    role_mention = f" <@&{role_id}>" if role_id else ""
    header_text = f"### <:Peeking1:1482425554627203233> New Chapter{role_mention} of {{{platform_display}}}"

    # --- Date/Time row ---
    date_time_text = (
        f"-# **Release Date**\u2003\u2003\u2003\u2003\u2003**Release Time**\n"
        f"-# {release_date}\u2003\u2003\u2003\u2003\u2003\u2003{release_time}"
    )

    # --- Title block ---
    if custom_title:
        title_text = f"## \u29fc{custom_title}\u29fd\n{series_title}"
    else:
        title_text = f"## {series_title}"

    # --- Footer IDs ---
    footer_text = f"-# N-ID: {notification_id} | S-ID: {series_id}"

    # --- Assemble inner components ---
    inner = []

    # 1. Header
    inner.append({"type": 10, "content": header_text})
    inner.append({"type": 14, "divider": True, "spacing": 1})

    # 2. Date/Time
    inner.append({"type": 10, "content": date_time_text})
    inner.append({"type": 14, "divider": True, "spacing": 1})

    # 3. Poster (Media Gallery for big centered image)
    if poster_url:
        inner.append({
            "type": 12,
            "items": [{"media": {"url": poster_url}}]
        })
        inner.append({"type": 14, "divider": True, "spacing": 1})

    # 4. Title(s)
    inner.append({"type": 10, "content": title_text})
    inner.append({"type": 14, "divider": True, "spacing": 1})

    # 5. Links
    inner.append({"type": 10, "content": "**Links:**"})
    inner.append({
        "type": 1,  # Action Row
        "components": [{
            "type": 2,        # Button
            "style": 5,       # Link
            "label": "Series Page",
            "url": series_url,
        }]
    })
    inner.append({"type": 14, "divider": True, "spacing": 1})

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
