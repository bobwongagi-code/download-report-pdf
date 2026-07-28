"""Provider detection, bounded candidate extraction, and safe filenames."""
from __future__ import annotations

import html as html_lib
import re
from typing import Optional
from urllib.parse import parse_qsl, quote, unquote, urljoin, urlparse, urlunparse
from html.parser import HTMLParser

from artifact_paths import (
    MAX_FILENAME_BYTES,
    _decode_rfc5987,
    ensure_unique_path,
    extract_filename,
    output_paths,
    sanitize_filename,
)
from url_utils import SUPPORTED_SCHEMES, UrlValidationError, normalize_url, redact_text, redact_url


_CANDIDATE_KEYS = {
    "url",
    "target",
    "download",
    "download_url",
    "downloadurl",
    "redirect",
    "redirect_url",
    "jump",
    "dest",
    "destination",
    "src",
    "file",
    "dlink",
}


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            result.append(item)
    return result


def _hostname(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").lower().rstrip(".")
    except ValueError:
        return ""


def _host_matches(host: str, domain: str) -> bool:
    return host == domain or host.endswith(f".{domain}")


def detect_provider_label(url: str) -> Optional[str]:
    host = _hostname(url)
    if _host_matches(host, "hubspotlinks.com") or _host_matches(host, "hubspotemail.net"):
        return "HubSpot"
    if host == "drive.google.com":
        return "Google Drive"
    if _host_matches(host, "dropbox.com"):
        return "Dropbox"
    if _host_matches(host, "sharepoint.com") or _host_matches(host, "onedrive.live.com") or host == "1drv.ms":
        return "SharePoint/OneDrive"
    if host in {"pan.baidu.com", "yun.baidu.com"}:
        return "Baidu Netdisk"
    if _host_matches(host, "aliyundrive.com") or _host_matches(host, "alipan.com"):
        return "Aliyun Drive"
    if _host_matches(host, "123pan.com") or _host_matches(host, "123684.com"):
        return "123Pan"
    if _host_matches(host, "lanzou.com") or _host_matches(host, "lanzn.com") or _host_matches(host, "lanzoui.com"):
        return "Lanzou"
    if _host_matches(host, "quark.cn") and host.startswith("pan."):
        return "Quark Drive"
    if _host_matches(host, "weiyun.com"):
        return "Weiyun"
    if _host_matches(host, "feishu.cn"):
        return "Feishu"
    return None


def _replace_query_value(query: str, key: str, value: str) -> tuple[str, bool]:
    encoded_value = quote(value, safe="")
    pieces = query.split("&") if query else []
    replaced = False
    output: list[str] = []
    for piece in pieces:
        if not piece:
            output.append(piece)
            continue
        raw_key = piece.split("=", 1)[0]
        decoded_key = unquote(raw_key.replace("+", " "))
        if decoded_key == key:
            output.append(f"{raw_key}={encoded_value}")
            replaced = True
        else:
            output.append(piece)
    return "&".join(output), replaced


def replace_query(url: str, updates: dict[str, str]) -> str:
    """Update only named query values while preserving all unknown pairs verbatim."""
    parsed = urlparse(url)
    query = parsed.query
    for key, value in updates.items():
        query, replaced = _replace_query_value(query, key, value)
        if not replaced:
            separator = "&" if query else ""
            query += f"{separator}{quote(key, safe='')}={quote(value, safe='')}"
    return urlunparse(parsed._replace(query=query))


def known_provider_candidates(url: str) -> list[str]:
    parsed = urlparse(url)
    host = _hostname(url)
    candidates: list[str] = []
    query_pairs = parse_qsl(parsed.query, keep_blank_values=True)
    query = {key: value for key, value in query_pairs}

    if host == "drive.google.com":
        match = re.search(r"/file/d/([^/]+)", parsed.path)
        file_id = match.group(1) if match else query.get("id")
        if file_id:
            candidates.append(f"https://drive.google.com/uc?export=download&id={quote(file_id, safe='')}")

    if _host_matches(host, "dropbox.com"):
        candidates.append(replace_query(url, {"dl": "1"}))
        candidates.append(replace_query(url, {"raw": "1"}))

    if _host_matches(host, "sharepoint.com") or _host_matches(host, "onedrive.live.com") or host == "1drv.ms":
        candidates.append(replace_query(url, {"download": "1"}))
    if _host_matches(host, "aliyundrive.com") or _host_matches(host, "alipan.com"):
        candidates.append(replace_query(url, {"download": "1"}))
    if _host_matches(host, "123pan.com") or _host_matches(host, "123684.com"):
        candidates.append(replace_query(url, {"download": "1"}))
    if _host_matches(host, "quark.cn") and host.startswith("pan."):
        candidates.append(replace_query(url, {"download": "1"}))

    for key, value in query_pairs:
        if key in _CANDIDATE_KEYS and value.startswith(("http://", "https://")) and ".pdf" in value.lower():
            candidates.append(value)
    return _dedupe(candidates)


def extract_hubspot_second_hop(html: str) -> Optional[str]:
    anchor_match = re.search(
        r'<a\b[^>]*href=["\'](https://[^"\']+/events/public/v1/encoded/track/[^"\']+)["\']',
        html,
        re.I,
    )
    if anchor_match:
        return html_lib.unescape(anchor_match.group(1))
    var_match = re.search(r'var\s+targetURL\s*=\s*["\'](https://[^"\']+_jss=-2)["\']', html, re.I)
    if var_match:
        return var_match.group(1).replace("_jss=-2", "_jss=0")
    return None


def is_hubspot_tracking_url(url: str) -> bool:
    parsed = urlparse(url)
    return (
        _host_matches(_hostname(url), "hubspotlinks.com")
        or _host_matches(_hostname(url), "hubspotemail.net")
        or "/events/public/v1/encoded/track/" in parsed.path
    )


class _LinkParser(HTMLParser):
    def __init__(self, base_url: str):
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.candidates: list[str] = []

    def _add(self, value: str, *, require_pdf: bool = True) -> None:
        value = html_lib.unescape(value.strip())
        if not value or value.startswith(("#", "javascript:", "data:")):
            return
        candidate = urljoin(self.base_url, value)
        parsed = urlparse(candidate)
        if parsed.scheme not in SUPPORTED_SCHEMES or not parsed.hostname:
            return
        if require_pdf and ".pdf" not in candidate.lower():
            return
        self.candidates.append(candidate)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        attributes = {key.lower(): value for key, value in attrs if value is not None}
        if tag.lower() in {"a", "link", "iframe", "object", "embed"}:
            for key in ("href", "src", "data"):
                if key in attributes:
                    self._add(
                        attributes[key],
                        require_pdf=attributes.get("type", "").lower() != "application/pdf",
                    )
        if tag.lower() == "meta" and attributes.get("http-equiv", "").lower() == "refresh":
            match = re.search(r"(?:^|;)\s*url\s*=\s*(.+)$", attributes.get("content", ""), re.I)
            if match:
                self._add(match.group(1).strip(" \t\"'"))

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        self.handle_starttag(tag, attrs)


def extract_pdf_candidates(html: str, base_url: str) -> list[str]:
    candidates: list[str] = []
    parser = _LinkParser(base_url)
    try:
        parser.feed(html)
        candidates.extend(parser.candidates)
    except Exception:
        # The JSON-like fallback below is deliberately narrow and PDF-only.
        pass

    escaped = html.replace("\\/", "/")
    for match in re.finditer(r"https?://[^\"'\s<>]+?\.pdf(?:\?[^\"'\s<>]*)?", escaped, re.I):
        candidates.append(match.group(0))
    for match in re.finditer(
        r"[\"'](?:url|target|download|download_url|downloadurl|redirect|redirect_url|jump|dest|destination|src|file|dlink)[\"']\s*:\s*[\"']([^\"']+\.pdf(?:\?[^\"']*)?)[\"']",
        escaped,
        re.I,
    ):
        candidates.append(urljoin(base_url, html_lib.unescape(match.group(1))))
    return _dedupe(candidates)
