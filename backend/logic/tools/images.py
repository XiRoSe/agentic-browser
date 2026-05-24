"""Extract image refs from raw HTML.

Used side-channel from fetch_url and browse_goto so the synthesizer can lay
out a richer view without paying LLM tokens to forward image URLs through
the scraper sub-agent's structured output.
"""

import re
from urllib.parse import urljoin

_IMG_TAG = re.compile(r"<img\b([^>]*)>", re.I)
_ATTR = re.compile(r"""(\w[\w-]*)\s*=\s*(?:"([^"]*)"|'([^']*)')""", re.I)
_TRACKING_HOSTS = (
    "doubleclick.net", "google-analytics.com", "googletagmanager.com",
    "scorecardresearch.com", "facebook.com/tr", "quantserve.com",
)


def extract_images(html: str, base_url: str, source_url: str, max_imgs: int = 3) -> list[dict]:
    """Best-effort image extraction.

    Returns up to `max_imgs` dicts with shape `{"url", "alt", "source_url"}`.
    Filters tracking pixels, data: URIs, and obvious icons. Prefers images
    with alt text (a weak signal that they're editorial, not chrome).
    """
    out: list[dict] = []
    seen: set[str] = set()
    if not html:
        return out

    for m in _IMG_TAG.finditer(html):
        attrs: dict[str, str] = {}
        for a in _ATTR.finditer(m.group(1)):
            attrs[a.group(1).lower()] = (a.group(2) if a.group(2) is not None else a.group(3)) or ""

        src = (
            attrs.get("src")
            or attrs.get("data-src")
            or attrs.get("data-original")
            or attrs.get("data-lazy-src")
            or ""
        ).strip()
        if not src or src.startswith("data:") or src.startswith("javascript:"):
            continue

        # Pick a candidate from srcset's largest entry if there's no plain src.
        if not src and attrs.get("srcset"):
            parts = [p.strip().split(" ", 1) for p in attrs["srcset"].split(",")]
            if parts:
                src = parts[-1][0]

        abs_url = urljoin(base_url, src)
        if abs_url in seen:
            continue
        if any(h in abs_url for h in _TRACKING_HOSTS):
            continue

        # Skip declared tiny dimensions (tracking pixels, sprite icons).
        try:
            w = int(attrs.get("width", "")) if attrs.get("width", "").isdigit() else None
            h = int(attrs.get("height", "")) if attrs.get("height", "").isdigit() else None
            if (w is not None and w < 80) or (h is not None and h < 80):
                continue
        except (ValueError, TypeError):
            pass

        # Skip favicons & common icon paths.
        low = abs_url.lower()
        if any(s in low for s in ("favicon", "/sprite", "/icons/", "logo")):
            continue

        alt = (attrs.get("alt") or "").strip()
        out.append({"url": abs_url, "alt": alt[:200], "source_url": source_url})
        seen.add(abs_url)

        if len(out) >= max_imgs:
            break

    # Re-sort: alt-text first (more likely to be editorial), preserve relative order.
    out.sort(key=lambda x: 0 if x["alt"] else 1)
    return out
