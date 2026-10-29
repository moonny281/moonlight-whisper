"""Unified HTTP/SOCKS5 client with PEP 503 Simple Index fallback and chunked Range resume."""

from __future__ import annotations

import hashlib
import json
import re
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from app.core.cancel import CancelToken
from app.core.errors import CancelledError

USER_AGENT = "MoonlightWhisper/1.0"

_PEP503_LINK_RE = re.compile(
    r'<a\s+[^>]*href=["\']([^"\']+\.whl(?:#sha256=[0-9a-fA-F]+)?)["\'][^>]*>([^<]+)</a>',
    re.IGNORECASE,
)


def _proxies_dict(proxy: str) -> Optional[Dict[str, str]]:
    p = (proxy or "").strip()
    if not p:
        return None
    # Convert socks5:// to socks5h:// for remote DNS resolution when using requests
    return {"http": p, "https": p}


def fetch_bytes(url: str, proxy: str = "", timeout: int = 25, headers: Optional[Dict[str, str]] = None) -> bytes:
    """Perform a GET request supporting both HTTP(S) and SOCKS5 proxies."""
    req_headers = {"User-Agent": USER_AGENT}
    if headers:
        req_headers.update(headers)

    try:
        import requests  # type: ignore
        resp = requests.get(
            url,
            headers=req_headers,
            proxies=_proxies_dict(proxy),
            timeout=timeout,
        )
        resp.raise_for_status()
        return bytes(resp.content)
    except ImportError:
        pass

    handlers = []
    if proxy.strip():
        handlers.append(urllib.request.ProxyHandler({"http": proxy.strip(), "https": proxy.strip()}))
    opener = urllib.request.build_opener(*handlers)
    req = urllib.request.Request(url, headers=req_headers)
    with opener.open(req, timeout=timeout) as resp:
        return resp.read()


def fetch_json(url: str, proxy: str = "", timeout: int = 25) -> Dict[str, Any]:
    raw = fetch_bytes(url, proxy=proxy, timeout=timeout)
    return dict(json.loads(raw.decode("utf-8")))


def post_json(
    url: str,
    payload: Dict[str, Any],
    headers: Optional[Dict[str, str]] = None,
    proxy: str = "",
    timeout: int = 45,
) -> Dict[str, Any]:
    """Perform a JSON POST request supporting both HTTP(S) and SOCKS5 proxies."""
    req_headers = {
        "Content-Type": "application/json",
        "User-Agent": USER_AGENT,
    }
    if headers:
        req_headers.update(headers)

    try:
        import requests  # type: ignore
        resp = requests.post(
            url,
            json=payload,
            headers=req_headers,
            proxies=_proxies_dict(proxy),
            timeout=timeout,
        )
        resp.raise_for_status()
        return dict(resp.json())
    except ImportError:
        pass

    handlers = []
    if proxy.strip():
        handlers.append(urllib.request.ProxyHandler({"http": proxy.strip(), "https": proxy.strip()}))
    opener = urllib.request.build_opener(*handlers)
    data_bytes = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data_bytes, headers=req_headers, method="POST")
    with opener.open(req, timeout=timeout) as resp:
        return dict(json.loads(resp.read().decode("utf-8")))


def _extract_version_tuple(filename: str) -> Tuple[int, ...]:
    # Wheel format: {distribution}-{version}(-{build})?-{python}-{abi}-{platform}.whl
    parts = filename.split("-")
    if len(parts) < 2:
        return (0,)
    ver_str = parts[1]
    nums = [int(x) for x in re.findall(r"\d+", ver_str)]
    return tuple(nums) if nums else (0,)


def parse_pep503_simple_index(
    html_text: str,
    base_url: str,
    win_amd64_only: bool = False,
) -> List[Dict[str, Any]]:
    """Parse a PEP 503 Simple Repository API HTML page and return sorted wheel descriptors."""
    candidates: List[Dict[str, Any]] = []
    for match in _PEP503_LINK_RE.finditer(html_text):
        raw_href = match.group(1).strip()
        filename = match.group(2).strip()
        if not filename.endswith(".whl"):
            filename = Path(urllib.parse.urlparse(raw_href).path).name
        if win_amd64_only and "win_amd64" not in filename:
            continue

        sha256 = ""
        if "#sha256=" in raw_href:
            url_part, frag = raw_href.split("#sha256=", 1)
            sha256 = frag.split("&")[0].strip()
        else:
            url_part = raw_href

        full_url = urllib.parse.urljoin(base_url, url_part)
        ver_tuple = _extract_version_tuple(filename)
        parts = filename.split("-")
        ver_str = parts[1] if len(parts) >= 2 else "unknown"

        candidates.append(
            {
                "filename": filename,
                "url": full_url,
                "version": ver_str,
                "version_tuple": ver_tuple,
                "size": 0,
                "digests": {"sha256": sha256},
            }
        )

    candidates.sort(key=lambda item: item["version_tuple"], reverse=True)
    return candidates


def resolve_pypi_wheel(
    pkg_name: str,
    mirror: str = "tuna",
    proxy: str = "",
    win_amd64_only: bool = False,
) -> Dict[str, Any]:
    """Resolve the latest wheel metadata for `pkg_name` across Tuna, Aliyun, or Official PyPI.

    Supports both Warehouse JSON API and PEP 503 Simple Index HTML (used by Aliyun).
    """
    json_urls: List[str] = []
    simple_urls: List[str] = []

    if mirror == "tuna":
        json_urls.append(f"https://pypi.tuna.tsinghua.edu.cn/pypi/{pkg_name}/json")
        simple_urls.append(f"https://pypi.tuna.tsinghua.edu.cn/simple/{pkg_name}/")
    elif mirror == "aliyun":
        simple_urls.append(f"https://mirrors.aliyun.com/pypi/simple/{pkg_name}/")
        json_urls.append(f"https://pypi.tuna.tsinghua.edu.cn/pypi/{pkg_name}/json")
    else:
        json_urls.append(f"https://pypi.org/pypi/{pkg_name}/json")
        simple_urls.append(f"https://pypi.org/simple/{pkg_name}/")

    last_err: Optional[BaseException] = None

    # For Aliyun, try PEP 503 Simple Index first since Warehouse JSON endpoint often 404s
    search_order = (
        [("simple", u) for u in simple_urls] + [("json", u) for u in json_urls]
        if mirror == "aliyun"
        else [("json", u) for u in json_urls] + [("simple", u) for u in simple_urls]
    )

    for kind, endpoint in search_order:
        try:
            if kind == "json":
                data = fetch_json(endpoint, proxy=proxy, timeout=25)
                version_str = str(data.get("info", {}).get("version") or "")
                urls = data.get("urls") or []
                matches = [
                    u for u in urls
                    if str(u.get("filename", "")).endswith(".whl")
                    and (not win_amd64_only or "win_amd64" in str(u.get("filename", "")))
                ]
                if not matches:
                    releases = data.get("releases") or {}
                    for ver in sorted(releases.keys(), reverse=True):
                        rel_matches = [
                            u for u in releases[ver]
                            if str(u.get("filename", "")).endswith(".whl")
                            and (not win_amd64_only or "win_amd64" in str(u.get("filename", "")))
                        ]
                        if rel_matches:
                            matches = rel_matches
                            version_str = ver
                            break
                if matches:
                    chosen = dict(matches[0])
                    chosen.setdefault("version", version_str)
                    return chosen
            else:
                html_bytes = fetch_bytes(endpoint, proxy=proxy, timeout=25)
                parsed = parse_pep503_simple_index(
                    html_bytes.decode("utf-8", errors="replace"),
                    base_url=endpoint,
                    win_amd64_only=win_amd64_only,
                )
                if parsed:
                    return parsed[0]
        except Exception as exc:
            last_err = exc
            continue

    raise RuntimeError(f"无法从镜像源 ({mirror}) 获取 {pkg_name} 的安装包信息: {last_err}")


def compute_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            chunk = f.read(1024 * 1024)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def stream_download_with_resume(
    url: str,
    dest_file: Path,
    proxy: str = "",
    expected_sha256: str = "",
    on_chunk_progress: Optional[Callable[[int, int], None]] = None,
    token: Optional[CancelToken] = None,
    timeout: int = 60,
) -> Path:
    """Download `url` to `dest_file` with HTTP Range resume, SOCKS5 support, and per-chunk cancellation.

    `on_chunk_progress(downloaded_bytes, total_bytes)` is invoked on every 128KB chunk.
    """
    if token:
        token.check()

    dest_file.parent.mkdir(parents=True, exist_ok=True)
    part_file = dest_file.with_suffix(dest_file.suffix + ".part")

    if dest_file.exists():
        if not expected_sha256 or compute_sha256(dest_file).lower() == expected_sha256.lower():
            sz = dest_file.stat().st_size
            if on_chunk_progress:
                on_chunk_progress(sz, sz)
            return dest_file
        dest_file.unlink()

    existing_bytes = part_file.stat().st_size if part_file.exists() else 0
    headers = {"User-Agent": USER_AGENT}
    if existing_bytes > 0:
        headers["Range"] = f"bytes={existing_bytes}-"

    try:
        import requests  # type: ignore
        with requests.get(
            url,
            headers=headers,
            proxies=_proxies_dict(proxy),
            stream=True,
            timeout=timeout,
        ) as resp:
            if resp.status_code == 416:
                # Range not satisfiable -> restart cleanly
                existing_bytes = 0
                part_file.unlink(missing_ok=True)
                return stream_download_with_resume(
                    url=url,
                    dest_file=dest_file,
                    proxy=proxy,
                    expected_sha256=expected_sha256,
                    on_chunk_progress=on_chunk_progress,
                    token=token,
                    timeout=timeout,
                )
            resp.raise_for_status()
            is_partial = resp.status_code == 206 and existing_bytes > 0
            mode = "ab" if is_partial else "wb"
            if not is_partial:
                existing_bytes = 0

            content_len = int(resp.headers.get("Content-Length") or 0)
            total_bytes = (existing_bytes + content_len) if content_len > 0 else 0
            downloaded = existing_bytes
            if on_chunk_progress:
                on_chunk_progress(downloaded, total_bytes)

            with part_file.open(mode) as out_f:
                for chunk in resp.iter_content(chunk_size=128 * 1024):
                    if token:
                        token.check()
                    if not chunk:
                        continue
                    out_f.write(chunk)
                    downloaded += len(chunk)
                    if on_chunk_progress:
                        on_chunk_progress(downloaded, total_bytes)
    except CancelledError:
        raise
    except ImportError:
        # Fallback to urllib if requests is not installed
        handlers = []
        if proxy.strip():
            handlers.append(urllib.request.ProxyHandler({"http": proxy.strip(), "https": proxy.strip()}))
        opener = urllib.request.build_opener(*handlers)
        req = urllib.request.Request(url, headers=headers)
        with opener.open(req, timeout=timeout) as resp:
            status_code = getattr(resp, "status", 200)
            is_partial = status_code == 206 and existing_bytes > 0
            mode = "ab" if is_partial else "wb"
            if not is_partial:
                existing_bytes = 0
            content_len = int(resp.headers.get("Content-Length") or 0)
            total_bytes = (existing_bytes + content_len) if content_len > 0 else 0
            downloaded = existing_bytes
            if on_chunk_progress:
                on_chunk_progress(downloaded, total_bytes)

            with part_file.open(mode) as out_f:
                while True:
                    if token:
                        token.check()
                    chunk = resp.read(128 * 1024)
                    if not chunk:
                        break
                    out_f.write(chunk)
                    downloaded += len(chunk)
                    if on_chunk_progress:
                        on_chunk_progress(downloaded, total_bytes)

    if expected_sha256:
        actual_sha = compute_sha256(part_file)
        if actual_sha.lower() != expected_sha256.lower():
            part_file.unlink(missing_ok=True)
            raise ValueError(
                f"文件 {dest_file.name} SHA256 校验未通过 (expected={expected_sha256}, actual={actual_sha})"
            )

    part_file.replace(dest_file)
    return dest_file
