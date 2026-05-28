"""Provider detection, URL candidate generation, and filename extraction."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qsl, quote, unquote, urlencode, urlparse, urlunparse


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            result.append(item)
    return result


def detect_provider_label(url: str) -> Optional[str]:
    host = urlparse(url).netloc.lower()
    if "hubspotlinks.com" in host or "hubspotemail.net" in host:
        return "HubSpot"
    if "drive.google.com" == host:
        return "Google Drive"
    if "dropbox.com" in host:
        return "Dropbox"
    if "sharepoint.com" in host or "onedrive.live.com" in host or host == "1drv.ms":
        return "SharePoint/OneDrive"
    if "pan.baidu.com" in host or "yun.baidu.com" in host:
        return "Baidu Netdisk"
    if "aliyundrive.com" in host or "alipan.com" in host:
        return "Aliyun Drive"
    if "123pan.com" in host or "123684.com" in host:
        return "123Pan"
    if "lanzou" in host or "lanzn.com" in host:
        return "Lanzou"
    if "pan.quark.cn" in host:
        return "Quark Drive"
    if "weiyun.com" in host:
        return "Weiyun"
    if "feishu.cn" in host:
        return "Feishu"
    return None


def replace_query(url: str, updates: dict[str, str]) -> str:
    parsed = urlparse(url)
    pairs = dict(parse_qsl(parsed.query, keep_blank_values=True))
    pairs.update(updates)
    return urlunparse(parsed._replace(query=urlencode(pairs)))


def known_provider_candidates(url: str) -> list[str]:
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    candidates: list[str] = []
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))

    if host == "drive.google.com":
        match = re.search(r"/file/d/([^/]+)", parsed.path)
        if match:
            file_id = match.group(1)
            candidates.append(f"https://drive.google.com/uc?export=download&id={quote(file_id)}")
        file_id = query.get("id")
        if file_id:
            candidates.append(f"https://drive.google.com/uc?export=download&id={quote(file_id)}")

    if "dropbox.com" in host:
        candidates.append(replace_query(url, {"dl": "1"}))
        candidates.append(replace_query(url, {"raw": "1"}))

    if "sharepoint.com" in host or "onedrive.live.com" in host or host == "1drv.ms":
        candidates.append(replace_query(url, {"download": "1"}))

    if "aliyundrive.com" in host or "alipan.com" in host:
        candidates.append(replace_query(url, {"download": "1"}))

    if "123pan.com" in host or "123684.com" in host:
        candidates.append(replace_query(url, {"download": "1"}))

    if "pan.quark.cn" in host:
        candidates.append(replace_query(url, {"download": "1"}))

    for key in [
        "url", "target", "download", "download_url", "downloadurl",
        "redirect", "redirect_url", "jump", "dest", "destination", "src", "file", "dlink",
    ]:
        value = query.get(key)
        if value and value.startswith(("http://", "https://")):
            candidates.append(value)

    return _dedupe(candidates)


def extract_hubspot_second_hop(html: str) -> Optional[str]:
    anchor_match = re.search(r'<a href="(https://[^"]+/events/public/v1/encoded/track/[^"]+)"', html)
    if anchor_match:
        return anchor_match.group(1)

    var_match = re.search(r'var targetURL = "(https://[^"]+_jss=-2)";', html)
    if var_match:
        return var_match.group(1).replace("_jss=-2", "_jss=0")
    return None


def is_hubspot_tracking_url(url: str) -> bool:
    host = urlparse(url).netloc.lower()
    return "hubspotlinks.com" in host or "/events/public/v1/encoded/track/" in url


def extract_pdf_candidates(html: str, base_url: str) -> list[str]:
    candidates: list[str] = []

    for match in re.finditer(r'https://[^"\']+?\.pdf(?:\?[^"\']*)?', html, re.I):
        candidates.append(match.group(0))

    for match in re.finditer(r'https:\\/\\/[^"\']+?\.pdf(?:\\\/?[^"\']*)?', html, re.I):
        candidates.append(match.group(0).replace("\\/", "/"))

    for match in re.finditer(r'href="([^"]+?\.pdf(?:\?[^"]*)?)"', html, re.I):
        candidate = match.group(1)
        if candidate.startswith("http://") or candidate.startswith("https://"):
            candidates.append(candidate)
        elif candidate.startswith("/"):
            parsed = urlparse(base_url)
            candidates.append(urlunparse((parsed.scheme, parsed.netloc, candidate, "", "", "")))

    for match in re.finditer(r'(?:window\.location(?:\.replace)?|document\.location)\s*=\s*"([^"]+?\.pdf(?:\?[^"]*)?)"', html, re.I):
        candidate = match.group(1)
        if candidate.startswith("http://") or candidate.startswith("https://"):
            candidates.append(candidate)
        elif candidate.startswith("/"):
            parsed = urlparse(base_url)
            candidates.append(urlunparse((parsed.scheme, parsed.netloc, candidate, "", "", "")))

    for match in re.finditer(
        r'<meta[^>]+http-equiv="refresh"[^>]+content="[^"]*url=([^"]+)"',
        html,
        re.I,
    ):
        candidate = match.group(1).strip()
        if candidate.startswith("http://") or candidate.startswith("https://"):
            candidates.append(candidate)
        elif candidate.startswith("/"):
            parsed = urlparse(base_url)
            candidates.append(urlunparse((parsed.scheme, parsed.netloc, candidate, "", "", "")))

    for match in re.finditer(r'(?:href|src)="([^"]+)"', html, re.I):
        candidate = match.group(1)
        if ".pdf" not in candidate.lower():
            continue
        if candidate.startswith("http://") or candidate.startswith("https://"):
            candidates.append(candidate)
        elif candidate.startswith("/"):
            parsed = urlparse(base_url)
            candidates.append(urlunparse((parsed.scheme, parsed.netloc, candidate, "", "", "")))

    for key in [
        "url", "target", "download", "download_url", "downloadurl",
        "redirect", "redirect_url", "jump", "dest", "destination", "src", "file", "dlink",
    ]:
        pattern = rf'"{key}"\s*:\s*"([^"]+)"'
        for match in re.finditer(pattern, html, re.I):
            candidate = match.group(1).replace("\\/", "/")
            if ".pdf" in candidate.lower() or candidate.startswith(("http://", "https://")):
                candidates.append(candidate)

    return _dedupe(candidates)


def normalize_url(url: str) -> str:
    url = url.strip()
    if not url.lower().startswith(("http://", "https://")):
        raise ValueError(f"Only http and https URLs are supported, got: {url!r}")
    return url


def extract_filename(headers: dict[str, str], url: str) -> str:
    disposition = headers.get("content-disposition", "")
    match = re.search(r"filename\\*=UTF-8''([^;]+)", disposition, re.I)
    if match:
        return sanitize_filename(unquote(match.group(1)))
    match = re.search(r'filename="?([^";]+)"?', disposition, re.I)
    if match:
        return sanitize_filename(match.group(1))

    path_name = Path(unquote(urlparse(url).path)).name
    if path_name:
        return sanitize_filename(path_name)
    return "download.pdf"


def sanitize_filename(name: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip(".-")
    if not cleaned:
        cleaned = "download.pdf"
    if not cleaned.lower().endswith(".pdf"):
        cleaned += ".pdf"
    return cleaned


def ensure_unique_path(path: Path, max_attempts: int = 9999) -> Path:
    if not path.exists():
        return path
    stem = path.stem
    suffix = path.suffix
    for counter in range(2, 2 + max_attempts):
        candidate = path.with_name(f"{stem}-{counter}{suffix}")
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"Could not find unique filename after {max_attempts} attempts: {path}")
