"""Site grabber: lists the files a web page links to, so the user can pick which to queue."""

import html as html_lib
import re
from typing import Iterable, List, Optional
from urllib.parse import unquote, urljoin, urlsplit

import httpx

from .categories import category_for, extension
from .grab import MAX_PAGE_BYTES, MEDIA_URL_PATTERN, GrabError
from .netguard import BlockedURL, check_public_url
from .segmented import make_client

LINK_PATTERN = re.compile(r"""\b(?:href|src|data-src)\s*=\s*["']([^"'#]+)["']""", re.IGNORECASE)
MAX_RESULTS = 500


def find_files(
    url: str,
    categories: Optional[Iterable[str]] = None,
    *,
    allow_private: bool = False,
    proxy: Optional[str] = None,
) -> List[dict]:
    """
    Returns [{url, name, ext, category}] for every file link on the page (one level deep),
    keeping only `categories` when given. Links to other web pages are left out.
    """
    url = url.strip()
    if not url.lower().startswith(("http://", "https://")):
        raise GrabError("Paste a full link starting with http:// or https://.")
    guard = None if allow_private else check_public_url
    wanted = set(categories or [])
    try:
        if guard:
            guard(url)
        with make_client(2, guard, proxy) as client:
            with client.stream("GET", url) as r:
                if r.status_code >= 400:
                    raise GrabError(f"The page answered HTTP {r.status_code}.")
                body = bytearray()
                for chunk in r.iter_bytes():
                    body += chunk
                    if len(body) > MAX_PAGE_BYTES:
                        break
                base = str(r.url)
                page = body.decode(r.encoding or "utf-8", "replace")
    except BlockedURL as exc:
        raise GrabError(str(exc))
    except httpx.HTTPError as exc:
        raise GrabError(f"Network error: {exc}")

    page = html_lib.unescape(page.replace("\\/", "/"))
    candidates = LINK_PATTERN.findall(page) + MEDIA_URL_PATTERN.findall(page)
    results, seen = [], set()
    for raw in candidates:
        link = urljoin(base, raw.strip())
        if not link.startswith(("http://", "https://")) or link in seen:
            continue
        seen.add(link)
        name = unquote(urlsplit(link).path.rsplit("/", 1)[-1])
        ext = extension(name)
        if ext in ("m3u8", "mpd"):
            category = "Video"
        else:
            category = category_for(name)
            if category == "Other":
                continue  # pages, scripts, stylesheets, images
        if wanted and category not in wanted:
            continue
        results.append({"url": link, "name": name or link, "ext": ext, "category": category})
        if len(results) >= MAX_RESULTS:
            break
    return results
