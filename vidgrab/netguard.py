"""
Blocks links that resolve to private or internal addresses.

A downloader running on a server would otherwise let anyone make it fetch internal URLs
(cloud metadata endpoints, admin panels on localhost, other machines on the network).
"""

import ipaddress
import socket
from urllib.parse import urlsplit


class BlockedURL(ValueError):
    pass


def check_public_url(url: str) -> None:
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise BlockedURL("Only http:// and https:// links are supported.")
    host = parts.hostname
    if not host:
        raise BlockedURL("That link has no host name.")
    try:
        infos = socket.getaddrinfo(host, parts.port or 443, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        raise BlockedURL(f"Could not find the site {host}.")
    for *_, sockaddr in infos:
        ip = ipaddress.ip_address(sockaddr[0].split("%")[0])
        mapped = getattr(ip, "ipv4_mapped", None)
        if not ip.is_global or (mapped and not mapped.is_global):
            raise BlockedURL(f"{host} points to a private network address, which is blocked.")
