"""yt-dlp wrapper with system proxy auto-detection, isolated 4-format Cookie normalization, browser DB lock fallback, and multi-stage TikTok direct fallback."""

from __future__ import annotations

import copy
import html
import importlib
import json
import re
import shutil
import sys
import tempfile
import time
import urllib.parse
import urllib.request
import uuid
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from app.core import http_client
from app.core.cancel import CancelToken
from app.core.errors import CancelledError, DownloadError
from app.paths import PATHS, AppPaths

ProgressCallback = Callable[[float, str], None]
LogCallback = Callable[[str], None]

_YOUTUBE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
_TIKTOK_VIDEO_ID_RE = re.compile(r"(?:\[TikTok\]\s*|/(?:video|photo|v|embed/v2)/)(\d{15,22})", re.IGNORECASE)
_SPACE_NETSCAPE_RE = re.compile(
    r"^(\S+)\s+(TRUE|FALSE)\s+(/\S*)\s+(TRUE|FALSE)\s+(\d+)\s+(\S+)\s+(.*)$",
    re.IGNORECASE,
)

_AGE_OR_AUTH_PATTERNS = (
    "sign in to confirm",
    "sign in to view",
    "please log in",
    "login required",
    "authentication required",
    "age-restricted",
    "age restricted",
    "confirm your age",
    "age verification",
    "inappropriate for some users",
    "members-only",
    "members only",
    "private video",
    "this video is private",
    "need to log in",
)


@dataclass
class MediaInfo:
    url: str
    id: str
    title: str
    uploader: str
    duration: float
    extractor: str
    webpage_url: str = ""


@dataclass
class DownloadOptions:
    proxy: str = ""
    cookies_file: str = ""
    cookies_from_browser: str = ""
    allow_playlist: bool = False
    ffmpeg_dir: Optional[Path] = None
    deno_path: Optional[Path] = None


@dataclass
class _DirectMediaFallback:
    info: MediaInfo
    stream_url: str
    ext: str = "mp4"
    headers: Optional[Dict[str, str]] = None


# Short-lived cache mapping input URL -> resolved _DirectMediaFallback so `fetch_info` + `download_audio` never double-request
_DIRECT_MEDIA_CACHE: Dict[str, _DirectMediaFallback] = {}
# Cache working User-Agent per domain when a fallback UA succeeds
_WORKING_UA_CACHE: Dict[str, str] = {}


def is_likely_playlist_url(url: str) -> bool:
    """Return True if `url` might be a playlist, channel, or multi-part video; False for single-video links."""
    u = url.strip().lower()
    if not u.startswith(("http://", "https://", "www.")):
        return False
    # Single TikTok video or short share link is never a multi-video playlist
    if "tiktok.com" in u:
        if any(seg in u for seg in ("vt.tiktok.com", "vm.tiktok.com", "/video/", "/photo/", "/t/")):
            return False
        return True
    # Single YouTube watch/short link without `list=` is a single video
    if ("youtube.com/watch" in u or "youtu.be/" in u or "youtube.com/shorts/" in u) and "list=" not in u:
        return False
    # Single Instagram reel/post or X/Twitter status is a single video
    if any(seg in u for seg in ("instagram.com/reel/", "instagram.com/p/", "twitter.com/", "x.com/")):
        return False
    return True


def resolve_effective_proxy(explicit_proxy: str) -> str:
    """Resolve explicit proxy (auto-adding `http://` if scheme is omitted) or auto-detect Windows/OS system proxy."""
    p = (explicit_proxy or "").strip()
    if p:
        if "://" not in p:
            p = f"http://{p}"
        return p
    try:
        sys_proxies = urllib.request.getproxies()
        detected = sys_proxies.get("https") or sys_proxies.get("http") or sys_proxies.get("all") or ""
        if detected:
            if "://" not in detected:
                detected = f"http://{detected}"
            return detected
    except Exception:
        pass
    return ""


def ensure_plugin_yt_dlp_on_path(paths: AppPaths = PATHS) -> None:
    """Prepend `plugins/` to `sys.path` if a hot-updated yt-dlp package exists."""
    plugin_root = paths.plugins_dir
    if (plugin_root / "yt_dlp").exists():
        plugin_str = str(plugin_root.resolve())
        if plugin_str not in sys.path:
            sys.path.insert(0, plugin_str)


def _import_yt_dlp(paths: AppPaths = PATHS) -> Any:
    ensure_plugin_yt_dlp_on_path(paths)
    try:
        import yt_dlp  # type: ignore
        return yt_dlp
    except ImportError as exc:
        raise DownloadError(
            "未安装 yt-dlp 组件，无法解析或下载在线视频。",
            detail=str(exc),
        ) from exc


class _YtDlpLogger:
    """Forwards yt-dlp internal messages to the core log callback."""

    def __init__(self, on_log: Optional[LogCallback] = None) -> None:
        self._on_log = on_log

    def debug(self, msg: str) -> None:
        if self._on_log and not msg.startswith("[debug] "):
            self._on_log(msg)

    def info(self, msg: str) -> None:
        if self._on_log:
            self._on_log(msg)

    def warning(self, msg: str) -> None:
        if self._on_log:
            self._on_log(f"[yt-dlp 警告] {msg}")

    def error(self, msg: str) -> None:
        if self._on_log:
            self._on_log(f"[yt-dlp 错误] {msg}")


def _read_text_multi_encoding(path: Path) -> str:
    raw_bytes = path.read_bytes()
    for enc in ("utf-8-sig", "utf-8", "utf-16", "gbk"):
        try:
            return raw_bytes.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw_bytes.decode("utf-8", errors="replace")


def _infer_cookie_domains(target_url: str) -> List[str]:
    domains = [".tiktok.com", ".youtube.com", ".bilibili.com", ".instagram.com", ".x.com", ".twitter.com"]
    if target_url:
        try:
            host = (urllib.parse.urlparse(target_url).hostname or "").lower()
            if host:
                parts = host.split(".")
                base_dom = "." + ".".join(parts[-2:]) if len(parts) >= 2 else f".{host}"
                if base_dom not in domains:
                    domains.insert(0, base_dom)
        except Exception:
            pass
    return domains


def prepare_cookie_file(
    cookie_path: Path,
    temp_dir: Path,
    on_log: Optional[LogCallback] = None,
    target_url: str = "",
) -> Optional[Path]:
    """Validate and copy a user-supplied Cookies file into a fresh isolated Netscape HTTP Cookie File.

    Always writes a fresh temporary copy so `yt-dlp` (`MozillaCookieJar.save()`) NEVER mutates or
    wipes session cookies in the user's original file.
    Supports:
    1. Standard 7-column tab-separated Netscape `cookies.txt`
    2. Space-separated 7-column Netscape `cookies.txt`
    3. JSON array/object exported by browser extensions (*Cookie-Editor*, *EditThisCookie*)
    4. Raw HTTP `Cookie:` header string (`k1=v1; k2=v2`) copied from Chrome/Edge F12 DevTools
    """
    if not cookie_path.exists() or not cookie_path.is_file():
        if on_log:
            on_log(f"[Cookies 警告] 指定的 Cookies 文件不存在：{cookie_path}")
        return None

    try:
        raw_text = _read_text_multi_encoding(cookie_path).strip()
    except OSError as exc:
        if on_log:
            on_log(f"[Cookies 警告] 无法读取 Cookies 文件 ({cookie_path})：{exc}")
        return None

    if not raw_text:
        if on_log:
            on_log(f"[Cookies 警告] Cookies 文件为空：{cookie_path}")
        return None

    temp_dir.mkdir(parents=True, exist_ok=True)
    isolated_out = temp_dir / f"cookies_session_{uuid.uuid4().hex[:8]}.txt"
    future_exp = int(time.time()) + 86400 * 365

    # Case 1: JSON Cookie export from Cookie-Editor / EditThisCookie
    if raw_text.startswith("[") or raw_text.startswith("{"):
        try:
            parsed = json.loads(raw_text)
            items = parsed if isinstance(parsed, list) else (parsed.get("cookies") or [])
            if isinstance(items, list) and items:
                lines = ["# Netscape HTTP Cookie File", "# Auto-converted from JSON by Moonlight Whisper", ""]
                valid_count = 0
                for c in items:
                    if not isinstance(c, dict):
                        continue
                    domain = str(c.get("domain") or "").strip()
                    name = str(c.get("name") or "").strip()
                    value = str(c.get("value") or "").strip()
                    if not domain or not name:
                        continue
                    include_sub = "TRUE" if domain.startswith(".") else "FALSE"
                    path_str = str(c.get("path") or "/").strip() or "/"
                    secure_str = "TRUE" if c.get("secure") else "FALSE"
                    exp_raw = c.get("expirationDate") or c.get("expires") or c.get("expiry") or 0
                    try:
                        exp_int = int(float(exp_raw))
                        if exp_int <= 0:
                            exp_int = future_exp
                    except (ValueError, TypeError):
                        exp_int = future_exp
                    lines.append(f"{domain}\t{include_sub}\t{path_str}\t{secure_str}\t{exp_int}\t{name}\t{value}")
                    valid_count += 1

                if valid_count > 0:
                    isolated_out.write_text("\n".join(lines) + "\n", encoding="utf-8")
                    if on_log:
                        on_log(f"[Cookies] 已将 JSON 格式 Cookies 转换为标准 Netscape 格式（共 {valid_count} 条记录）")
                    return isolated_out
        except Exception as exc:
            if on_log:
                on_log(f"[Cookies 警告] 解析 JSON 格式 Cookies 失败：{exc}")
            return None

    # Case 2 & 3: Netscape tab-separated OR space-separated 7-column format
    raw_lines = raw_text.splitlines()
    normalized_rows: List[str] = []
    for ln in raw_lines:
        s = ln.strip()
        if not s:
            continue
        is_httponly = s.startswith("#HttpOnly_")
        if s.startswith("#") and not is_httponly:
            continue
        prefix = "#HttpOnly_" if is_httponly else ""
        body = s[len("#HttpOnly_") :] if is_httponly else s

        parts = body.split("\t")
        if len(parts) < 7:
            m_space = _SPACE_NETSCAPE_RE.match(body)
            if m_space:
                parts = list(m_space.groups())

        if len(parts) >= 7:
            dom, inc_sub, path_s, sec_s, exp_s, name_s = (
                parts[0].strip(),
                parts[1].strip().upper(),
                parts[2].strip() or "/",
                parts[3].strip().upper(),
                parts[4].strip(),
                parts[5].strip(),
            )
            val_s = "\t".join(parts[6:]).strip()
            if dom and not dom.startswith(".") and inc_sub == "TRUE":
                dom = "." + dom
            try:
                exp_val = int(float(exp_s))
                if exp_val <= 0:
                    exp_val = future_exp
            except (ValueError, TypeError):
                exp_val = future_exp
            inc_sub = "TRUE" if dom.startswith(".") else ("TRUE" if inc_sub == "TRUE" else "FALSE")
            sec_s = "TRUE" if sec_s == "TRUE" else "FALSE"
            normalized_rows.append(f"{prefix}{dom}\t{inc_sub}\t{path_s}\t{sec_s}\t{exp_val}\t{name_s}\t{val_s}")

    if normalized_rows:
        content = "# Netscape HTTP Cookie File\n# Isolated session copy generated by Moonlight Whisper\n\n" + "\n".join(normalized_rows) + "\n"
        isolated_out.write_text(content, encoding="utf-8")
        if on_log:
            on_log(f"[Cookies] 已加载并隔离保护 Cookies 文件：{cookie_path.name}（共 {len(normalized_rows)} 条有效记录）")
        return isolated_out

    # Case 4: Raw HTTP `Cookie:` header string copied from browser F12 DevTools (`k1=v1; k2=v2`)
    header_body = raw_text
    if header_body.lower().startswith("cookie:"):
        header_body = header_body.split(":", 1)[1].strip()
    if "=" in header_body and (";" in header_body or "\n" not in header_body):
        kv_pairs: List[Tuple[str, str]] = []
        for token_part in re.split(r"[;\r\n]+", header_body):
            token_part = token_part.strip()
            if "=" in token_part:
                k, v = token_part.split("=", 1)
                k, v = k.strip(), v.strip()
                if k and not k.startswith("$") and " " not in k:
                    kv_pairs.append((k, v))
        if kv_pairs:
            domains = _infer_cookie_domains(target_url)
            lines = ["# Netscape HTTP Cookie File", "# Auto-converted from raw Cookie header by Moonlight Whisper", ""]
            for dom in domains:
                for k, v in kv_pairs:
                    lines.append(f"{dom}\tTRUE\t/\tTRUE\t{future_exp}\t{k}\t{v}")
            isolated_out.write_text("\n".join(lines) + "\n", encoding="utf-8")
            if on_log:
                on_log(f"[Cookies] 已将浏览器请求头 Cookie 字符串转换为 Netscape 格式（共 {len(kv_pairs)} 个键值）")
            return isolated_out

    if on_log:
        on_log(
            f"[Cookies 警告] 文件 {cookie_path.name} 格式无法识别，请使用 Get cookies.txt LOCALLY 插件导出 Netscape 格式、JSON 格式或直接粘贴 F12 Cookie 字符串。"
        )
    return None


def _build_common_ydl_opts(
    opts: DownloadOptions,
    on_log: Optional[LogCallback] = None,
    paths: AppPaths = PATHS,
    target_url: str = "",
) -> Dict[str, Any]:
    ydl_opts: Dict[str, Any] = {
        "noplaylist": not opts.allow_playlist,
        "quiet": True,
        "no_warnings": True,
        "windowsfilenames": True,
        "logger": _YtDlpLogger(on_log),
        "socket_timeout": 30,
        "retries": 3,
    }

    ffmpeg_dir = opts.ffmpeg_dir or paths.bin_dir
    if ffmpeg_dir and ffmpeg_dir.exists():
        ydl_opts["ffmpeg_location"] = str(ffmpeg_dir)

    deno_bin = opts.deno_path or paths.find_binary("deno")
    if deno_bin and deno_bin.exists():
        ydl_opts["js_runtimes"] = {"deno": {"path": str(deno_bin)}}

    eff_proxy = resolve_effective_proxy(opts.proxy)
    if eff_proxy:
        ydl_opts["proxy"] = eff_proxy
        if on_log and not opts.proxy.strip():
            on_log(f"[网络] 已自动检测并启用系统代理：{eff_proxy}")

    loaded_cookiefile = False
    if opts.cookies_file.strip():
        cookie_path = Path(opts.cookies_file.strip()).expanduser()
        prepared = prepare_cookie_file(cookie_path, paths.temp_dir, on_log=on_log, target_url=target_url)
        if prepared is not None and prepared.exists():
            ydl_opts["cookiefile"] = str(prepared)
            loaded_cookiefile = True

    if opts.cookies_from_browser.strip() and not loaded_cookiefile:
        browser_name = opts.cookies_from_browser.strip().lower()
        ydl_opts["cookiesfrombrowser"] = (browser_name,)

    if _is_tiktok_url(target_url) and "tiktok" in _WORKING_UA_CACHE:
        ydl_opts["http_headers"] = {"User-Agent": _WORKING_UA_CACHE["tiktok"]}

    return ydl_opts


def _is_tiktok_url(url: str) -> bool:
    return "tiktok.com" in (url or "").lower()


def _extract_tiktok_video_id(url: str, error_text: str = "") -> Optional[str]:
    for text in (url, error_text):
        if not text:
            continue
        m = _TIKTOK_VIDEO_ID_RE.search(text)
        if m:
            return m.group(1)
    return None


def _fallback_tiktok_direct(
    url: str,
    video_id: Optional[str],
    proxy: str,
    on_log: Optional[LogCallback] = None,
) -> Optional[_DirectMediaFallback]:
    """Resolve TikTok video metadata and direct stream URL via TikWM API or TikTok Embed v2 page
    when `yt-dlp`'s webpage extractor is blocked by TikTok WAF (`Unexpected response from webpage request`).
    """
    if url in _DIRECT_MEDIA_CACHE:
        return _DIRECT_MEDIA_CACHE[url]

    eff_proxy = resolve_effective_proxy(proxy)

    # Strategy A: TikWM public resolution API (accepts both vt.tiktok.com short links and full video links)
    try:
        if on_log:
            on_log("[TikTok 备用通道 1/2] 正在通过免验证解析接口获取音视频直链...")
        api_url = f"https://www.tikwm.com/api/?url={urllib.parse.quote(url, safe='')}"
        resp = http_client.fetch_json(api_url, proxy=eff_proxy, timeout=20)
        data = resp.get("data") if isinstance(resp, dict) else None
        if isinstance(data, dict):
            vid = str(data.get("id") or video_id or "tiktok_video")
            title = str(data.get("title") or f"TikTok_{vid}").strip() or f"TikTok_{vid}"
            author_obj = data.get("author") or {}
            uploader = str(
                author_obj.get("nickname")
                or author_obj.get("unique_id")
                or "TikTokCreator"
            ).strip()
            duration = float(data.get("duration") or 0.0)
            # Prefer video stream (`play` / `wmplay`) so ffmpeg extracts highest-fidelity speech track, fallback to `music`
            stream_url = str(data.get("play") or data.get("wmplay") or data.get("music") or "").strip()
            if stream_url:
                if stream_url.startswith("/"):
                    stream_url = "https://www.tikwm.com" + stream_url
                ext = "mp3" if (not data.get("play") and not data.get("wmplay") and data.get("music")) else "mp4"
                media_info = MediaInfo(
                    url=url,
                    id=vid,
                    title=title,
                    uploader=uploader,
                    duration=duration,
                    extractor="TikTok",
                    webpage_url=url,
                )
                resolved = _DirectMediaFallback(
                    info=media_info,
                    stream_url=stream_url,
                    ext=ext,
                    headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36", "Referer": "https://www.tiktok.com/"},
                )
                _DIRECT_MEDIA_CACHE[url] = resolved
                if on_log:
                    on_log(f"[TikTok 备用通道] 成功解析视频直链：《{title}》 (ID: {vid})")
                return resolved
    except Exception as exc:
        if on_log:
            on_log(f"[TikTok 备用通道 1/2] 接口未命中 ({exc})，尝试官方 Embed 页面提取...")

    # Strategy B: TikTok Official Embed Page (`https://www.tiktok.com/embed/v2/{video_id}`)
    if video_id:
        try:
            embed_url = f"https://www.tiktok.com/embed/v2/{video_id}"
            html_bytes = http_client.fetch_bytes(
                embed_url,
                proxy=eff_proxy,
                timeout=20,
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:140.0) Gecko/20100101 Firefox/140.0",
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                },
            )
            page_text = html_bytes.decode("utf-8", errors="replace")
            # Search for playAddr or direct video_mp4 URL inside embed state JSON
            urls_found = re.findall(r'https?:\\?/\\?/[^"\'\s<>]+?(?:tiktokcdn| tiktokv)[^"\'\s<>]+', page_text)
            if not urls_found:
                urls_found = re.findall(r'"(?:playAddr|downloadAddr|url)"\s*:\s*"(https?:\\?/\\?/[^"]+)"', page_text)
            for raw_u in urls_found:
                clean_u = raw_u.encode("utf-8").decode("unicode_escape") if "\\u002" in raw_u or "\\/" in raw_u else raw_u.replace("\\/", "/")
                clean_u = html.unescape(clean_u)
                if clean_u.startswith("http") and ("video" in clean_u or "mime_type" in clean_u or "mp4" in clean_u):
                    title_m = re.search(r'"desc"\s*:\s*"([^"]+)"', page_text)
                    title_val = title_m.group(1) if title_m else f"TikTok_{video_id}"
                    media_info = MediaInfo(
                        url=url,
                        id=video_id,
                        title=title_val,
                        uploader="TikTok",
                        duration=0.0,
                        extractor="TikTokEmbed",
                        webpage_url=url,
                    )
                    resolved = _DirectMediaFallback(
                        info=media_info,
                        stream_url=clean_u,
                        ext="mp4",
                        headers={
                            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:140.0) Gecko/20100101 Firefox/140.0",
                            "Referer": "https://www.tiktok.com/",
                        },
                    )
                    _DIRECT_MEDIA_CACHE[url] = resolved
                    if on_log:
                        on_log(f"[TikTok 备用通道 2/2] 已从官方 Embed 页面提取视频流 (ID: {video_id})")
                    return resolved
        except Exception as exc:
            if on_log:
                on_log(f"[TikTok 备用通道 2/2] Embed 页面提取未命中：{exc}")

    return None


def _run_ydl_extract_with_fallbacks(
    yt_dlp: Any,
    ydl_opts: Dict[str, Any],
    url: str,
    download: bool,
    opts: DownloadOptions,
    paths: AppPaths,
    on_log: Optional[LogCallback] = None,
) -> Tuple[Any, Optional[Any]]:
    """Run `ydl.extract_info(url, download=download)` with automatic fallbacks for:
    1. Windows Edge/Chrome SQLite cookie database exclusive lock (`Could not copy Chrome cookie database`)
    2. TikTok anti-bot webpage challenge (`Unexpected response from webpage request`) with fresh cookie copies & proven UAs
    """
    current_opts = copy.copy(ydl_opts)

    try:
        with yt_dlp.YoutubeDL(current_opts) as ydl:
            return ydl.extract_info(url, download=download), ydl
    except CancelledError:
        raise
    except Exception as first_exc:
        err_str = str(first_exc)
        err_lower = err_str.lower()

        # Fallback 1: Running Edge/Chrome on Windows locks the Cookie database (yt-dlp#7271)
        if "could not copy" in err_lower and "cookie database" in err_lower and "cookiesfrombrowser" in current_opts:
            if on_log:
                on_log(
                    "[Cookies 回退] Windows 下 Edge/Chrome 运行期间独占锁定了 Cookie 数据库 (#7271)，"
                    "正在自动切换为免浏览器数据库模式重试..."
                )
            current_opts.pop("cookiesfrombrowser", None)
            try:
                with yt_dlp.YoutubeDL(current_opts) as ydl:
                    return ydl.extract_info(url, download=download), ydl
            except CancelledError:
                raise
            except Exception as retry_exc:
                first_exc = retry_exc
                err_str = str(first_exc)
                err_lower = err_str.lower()

        # Fallback 2: TikTok webpage anti-bot challenge (`Unexpected response from webpage request`)
        if _is_tiktok_url(url) and (
            "unexpected response from webpage request" in err_lower
            or "unable to extract" in err_lower
            or "impersonate" in err_lower
            or "status code 0" in err_lower
            or "expecting value" in err_lower
        ):
            vid = _extract_tiktok_video_id(url, err_str)
            # If the short link (vt.tiktok.com) already resolved a video ID in the error log, use canonical URL
            retry_url = f"https://www.tiktok.com/@_/video/{vid}" if (vid and ("vt.tiktok.com" in url or "vm.tiktok.com" in url)) else url

            fallback_uas = [
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:140.0) Gecko/20100101 Firefox/140.0",
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36 OPR/118.0.0.0",
                "Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:129.0) Gecko/20100101 Firefox/129.0",
            ]
            for idx, ua in enumerate(fallback_uas, start=1):
                if on_log:
                    on_log(f"[TikTok 兼容重试 {idx}/{len(fallback_uas)}] 正在使用浏览器 UA 策略与全新 Cookie 会话重试...")
                retry_opts = copy.copy(current_opts)
                # Regenerate a fresh isolated cookiefile so any previous failed attempt's save() cannot pollute this retry
                if opts.cookies_file.strip():
                    fresh_cookie = prepare_cookie_file(
                        Path(opts.cookies_file.strip()).expanduser(),
                        paths.temp_dir,
                        on_log=None,
                        target_url=retry_url,
                    )
                    if fresh_cookie and fresh_cookie.exists():
                        retry_opts["cookiefile"] = str(fresh_cookie)
                headers = dict(retry_opts.get("http_headers") or {})
                headers["User-Agent"] = ua
                retry_opts["http_headers"] = headers
                try:
                    with yt_dlp.YoutubeDL(retry_opts) as ydl:
                        info = ydl.extract_info(retry_url, download=download)
                        _WORKING_UA_CACHE["tiktok"] = ua
                        return info, ydl
                except CancelledError:
                    raise
                except Exception as tk_exc:
                    first_exc = tk_exc

        raise first_exc


def _translate_ydl_exception(exc: BaseException) -> DownloadError:
    text = str(exc)
    lower = text.lower()

    if "could not copy" in lower and "cookie database" in lower:
        return DownloadError(
            "无法直接读取正在运行的 Edge/Chrome 浏览器 Cookies（Windows 系统文件独占锁限制）。"
            "请完全关闭浏览器（含后台驻留进程）后重试，或使用浏览器插件（如 Get cookies.txt LOCALLY）导出 cookies.txt 文件并在设置中导入。",
            detail=text,
        )

    if "does not look like a netscape format cookies file" in lower:
        return DownloadError(
            "导入的 Cookies 文件格式无效。请使用 Get cookies.txt LOCALLY 或 Cookie-Editor 插件导出（支持 Netscape / JSON / F12 Cookie 字符串）。",
            detail=text,
        )

    if "unexpected response from webpage request" in lower or "impersonate target is available" in lower:
        return DownloadError(
            "目标站点（如 TikTok / Instagram）触发了深层反爬拦截。"
            "请检查「偏好设置 -> 代理地址」（如 http://127.0.0.1:7890）是否可用，并确认该链接在当前代理节点的浏览器中可正常播放。",
            detail=text,
        )

    if any(pat in lower for pat in _AGE_OR_AUTH_PATTERNS):
        return DownloadError(
            "该视频需要登录或年龄验证，请在「偏好设置 -> 网络与认证」中导入有效的 cookies.txt 文件（或先关闭浏览器再启用浏览器读取）。",
            detail=text,
        )

    if "timed out" in lower or "proxy" in lower or "connection" in lower or "ssl" in lower:
        return DownloadError(
            "网络连接超时或代理错误，请检查「偏好设置 -> 代理地址」（如 http://127.0.0.1:7890）后重试。",
            detail=text,
        )

    if "unsupported url" in lower or "no suitable infoextractor" in lower:
        return DownloadError(
            "不支持该链接或站点规则已变更，请检查链接或在底部点击「更新 yt-dlp」。",
            detail=text,
        )

    return DownloadError(f"视频解析或下载失败：{text}", detail=text)


def normalize_entry_url(
    entry: Dict[str, Any],
    fallback_url: str,
    playlist_index: Optional[int] = None,
) -> str:
    """Resolve a complete, runnable video URL from a potentially flat playlist entry (Fixes P1-2)."""
    webpage_url = str(entry.get("webpage_url") or "").strip()
    raw_url = str(entry.get("url") or "").strip()
    vid = str(entry.get("id") or "").strip()
    extractor = str(entry.get("ie_key") or entry.get("extractor_key") or entry.get("extractor") or "").lower()

    if webpage_url.startswith(("http://", "https://")):
        resolved = webpage_url
    elif raw_url.startswith(("http://", "https://")):
        resolved = raw_url
    elif ("youtube" in extractor or "youtube.com" in fallback_url or "youtu.be" in fallback_url) and (
        _YOUTUBE_ID_RE.match(raw_url) or _YOUTUBE_ID_RE.match(vid)
    ):
        yt_id = raw_url if _YOUTUBE_ID_RE.match(raw_url) else vid
        resolved = f"https://www.youtube.com/watch?v={yt_id}"
    else:
        resolved = fallback_url

    if playlist_index is not None and "bilibili.com/video/" in resolved:
        parsed = urllib.parse.urlparse(resolved)
        qs = urllib.parse.parse_qs(parsed.query)
        if "p" not in qs:
            qs["p"] = [str(playlist_index)]
            new_query = urllib.parse.urlencode(qs, doseq=True)
            resolved = urllib.parse.urlunparse(parsed._replace(query=new_query))

    return resolved


def _entry_to_media_info(
    entry: Dict[str, Any],
    fallback_url: str,
    playlist_index: Optional[int] = None,
) -> MediaInfo:
    vid = str(entry.get("id") or "unknown")
    title = str(entry.get("title") or vid).strip()
    uploader = str(entry.get("uploader") or entry.get("channel") or "未知作者").strip()
    duration = float(entry.get("duration") or 0.0)
    extractor = str(entry.get("extractor_key") or entry.get("ie_key") or entry.get("extractor") or "generic")
    full_url = normalize_entry_url(entry, fallback_url=fallback_url, playlist_index=playlist_index)
    return MediaInfo(
        url=full_url,
        id=vid,
        title=title,
        uploader=uploader,
        duration=duration,
        extractor=extractor,
        webpage_url=full_url,
    )


def fetch_info(
    url: str,
    opts: DownloadOptions,
    token: Optional[CancelToken] = None,
    on_log: Optional[LogCallback] = None,
    paths: AppPaths = PATHS,
) -> MediaInfo:
    """Fetch metadata for a single video URL without downloading media streams."""
    single_opts = DownloadOptions(
        proxy=opts.proxy,
        cookies_file=opts.cookies_file,
        cookies_from_browser=opts.cookies_from_browser,
        allow_playlist=False,
        ffmpeg_dir=opts.ffmpeg_dir,
        deno_path=opts.deno_path,
    )
    items = extract_playlist_or_info(url, single_opts, token=token, on_log=on_log, paths=paths)
    if not items:
        raise DownloadError("未能解析到有效的视频信息", detail=url)
    return items[0]


def extract_playlist_or_info(
    url: str,
    opts: DownloadOptions,
    token: Optional[CancelToken] = None,
    on_log: Optional[LogCallback] = None,
    paths: AppPaths = PATHS,
) -> List[MediaInfo]:
    """Extract metadata for a single video or expand a playlist / multi-part video into a list of `MediaInfo`."""
    if token:
        token.check()

    yt_dlp = _import_yt_dlp(paths)
    ydl_opts = _build_common_ydl_opts(opts, on_log=on_log, paths=paths, target_url=url)
    ydl_opts["extract_flat"] = "in_playlist" if opts.allow_playlist else False

    try:
        info, _ = _run_ydl_extract_with_fallbacks(
            yt_dlp=yt_dlp,
            ydl_opts=ydl_opts,
            url=url,
            download=False,
            opts=opts,
            paths=paths,
            on_log=on_log,
        )
    except CancelledError:
        raise
    except Exception as exc:
        if _is_tiktok_url(url):
            vid = _extract_tiktok_video_id(url, str(exc))
            direct_fb = _fallback_tiktok_direct(url, video_id=vid, proxy=opts.proxy, on_log=on_log)
            if direct_fb is not None:
                return [direct_fb.info]
        raise _translate_ydl_exception(exc) from exc

    if token:
        token.check()

    if not info:
        raise DownloadError("未获取到视频元数据", detail=url)

    if "entries" in info and info["entries"] is not None:
        entries_list = [e for e in info["entries"] if e]
        total_entries = len(entries_list)
        results: List[MediaInfo] = []
        for idx, entry in enumerate(entries_list, start=1):
            if token:
                token.check()
            p_idx = int(entry.get("playlist_index") or idx) if total_entries > 1 else None
            item = _entry_to_media_info(entry, fallback_url=url, playlist_index=p_idx)
            if not item.title or item.title == item.id:
                playlist_title = info.get("title") or "播放列表"
                item.title = f"{playlist_title} - P{idx}"
            results.append(item)
        if results:
            return results

    return [_entry_to_media_info(info, fallback_url=url)]


def _download_direct_media_fallback(
    fb: _DirectMediaFallback,
    workdir: Path,
    proxy: str,
    on_progress: Optional[ProgressCallback],
    on_log: Optional[LogCallback],
    token: Optional[CancelToken],
) -> Path:
    dest_file = workdir / f"{fb.info.id}.{fb.ext}"
    eff_proxy = resolve_effective_proxy(proxy)
    if on_log:
        on_log(f"[TikTok 备用通道] 开始直接下载音视频流：{dest_file.name}")

    def _on_chunk(cur: int, total: int) -> None:
        if on_progress and total > 0:
            ratio = min(1.0, max(0.0, cur / total))
            on_progress(ratio, f"备用通道下载中 {ratio * 100:.1f}%")

    http_client.stream_download_with_resume(
        url=fb.stream_url,
        dest_file=dest_file,
        proxy=eff_proxy,
        on_chunk_progress=_on_chunk,
        token=token,
        timeout=60,
    )
    if on_progress:
        on_progress(1.0, "音频流下载完成")
    return dest_file


def download_audio(
    url: str,
    workdir: Path,
    opts: DownloadOptions,
    on_progress: Optional[ProgressCallback] = None,
    on_log: Optional[LogCallback] = None,
    token: Optional[CancelToken] = None,
    paths: AppPaths = PATHS,
) -> Path:
    """Download the best audio stream into `workdir` and return the downloaded file path."""
    if token:
        token.check()

    workdir.mkdir(parents=True, exist_ok=True)

    # If `fetch_info` already resolved this TikTok URL via the direct API/Embed fallback, download directly!
    if url in _DIRECT_MEDIA_CACHE:
        fb = _DIRECT_MEDIA_CACHE.pop(url)
        return _download_direct_media_fallback(
            fb=fb,
            workdir=workdir,
            proxy=opts.proxy,
            on_progress=on_progress,
            on_log=on_log,
            token=token,
        )

    yt_dlp = _import_yt_dlp(paths)
    downloaded_files: List[Path] = []

    def _progress_hook(d: Dict[str, Any]) -> None:
        if token:
            token.check()
        status = d.get("status")
        if status == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            downloaded = d.get("downloaded_bytes") or 0
            ratio = (downloaded / total) if total > 0 else 0.0
            speed_str = d.get("_speed_str", "").strip()
            eta_str = d.get("_eta_str", "").strip()
            detail = f"下载中 {ratio * 100:.1f}%"
            if speed_str:
                detail += f" ({speed_str})"
            if eta_str:
                detail += f" 剩余 {eta_str}"
            if on_progress:
                on_progress(min(1.0, max(0.0, ratio)), detail)
        elif status == "finished":
            filename = d.get("filename")
            if filename:
                downloaded_files.append(Path(filename).resolve())
            if on_progress:
                on_progress(1.0, "音频流下载完成")

    ydl_opts = _build_common_ydl_opts(opts, on_log=on_log, paths=paths, target_url=url)
    ydl_opts["noplaylist"] = True
    ydl_opts["format"] = "bestaudio/best"
    ydl_opts["outtmpl"] = str(workdir / "%(id)s.%(ext)s")
    ydl_opts["progress_hooks"] = [_progress_hook]

    try:
        info, ydl = _run_ydl_extract_with_fallbacks(
            yt_dlp=yt_dlp,
            ydl_opts=ydl_opts,
            url=url,
            download=True,
            opts=opts,
            paths=paths,
            on_log=on_log,
        )
        if not downloaded_files and info and ydl is not None:
            prepared = Path(ydl.prepare_filename(info)).resolve()
            if prepared.exists():
                downloaded_files.append(prepared)
    except CancelledError:
        raise
    except Exception as exc:
        if _is_tiktok_url(url):
            vid = _extract_tiktok_video_id(url, str(exc))
            direct_fb = _fallback_tiktok_direct(url, video_id=vid, proxy=opts.proxy, on_log=on_log)
            if direct_fb is not None:
                _DIRECT_MEDIA_CACHE.pop(url, None)
                return _download_direct_media_fallback(
                    fb=direct_fb,
                    workdir=workdir,
                    proxy=opts.proxy,
                    on_progress=on_progress,
                    on_log=on_log,
                    token=token,
                )
        raise _translate_ydl_exception(exc) from exc

    if token:
        token.check()

    for candidate in downloaded_files:
        if candidate.exists() and candidate.is_file():
            return candidate

    candidates = sorted(
        [p for p in workdir.iterdir() if p.is_file() and not p.name.endswith(".part")],
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not candidates:
        raise DownloadError("下载结束但未找到音频文件", detail=str(workdir))
    return candidates[0]


def get_yt_dlp_version(paths: AppPaths = PATHS) -> str:
    """Return the currently active yt-dlp version string."""
    try:
        yt_dlp = _import_yt_dlp(paths)
        return str(getattr(yt_dlp.version, "__version__", "unknown"))
    except Exception:
        return "未安装"


def update_yt_dlp_from_pypi(
    pypi_mirror: str = "tuna",
    proxy: str = "",
    on_log: Optional[LogCallback] = None,
    token: Optional[CancelToken] = None,
    paths: AppPaths = PATHS,
) -> str:
    """Download the latest `yt-dlp` wheel from PyPI/mirror (supporting SOCKS5 & PEP 503) into `plugins/`."""
    if token:
        token.check()

    eff_proxy = resolve_effective_proxy(proxy)
    if on_log:
        on_log(f"正在从镜像源 ({pypi_mirror}) 查询 yt-dlp 最新版本...")

    try:
        wheel_info = http_client.resolve_pypi_wheel(
            "yt-dlp",
            mirror=pypi_mirror,
            proxy=eff_proxy,
            win_amd64_only=False,
        )
    except Exception as exc:
        raise DownloadError("无法获取 yt-dlp 版本信息，请检查网络或切换 PyPI 镜像。", detail=str(exc)) from exc

    if token:
        token.check()

    latest_version = str(wheel_info.get("version") or "latest")
    wheel_url = str(wheel_info["url"])
    filename = str(wheel_info["filename"])
    expected_sha256 = str(wheel_info.get("digests", {}).get("sha256") or "")

    if on_log:
        on_log(f"发现最新版本 {latest_version}，开始下载：{wheel_url}")

    with tempfile.TemporaryDirectory(prefix="ytdlp_update_") as tmp_dir_str:
        tmp_dir = Path(tmp_dir_str)
        whl_path = tmp_dir / filename
        try:
            http_client.stream_download_with_resume(
                url=wheel_url,
                dest_file=whl_path,
                proxy=eff_proxy,
                expected_sha256=expected_sha256,
                token=token,
                timeout=60,
            )
        except CancelledError:
            raise
        except Exception as exc:
            raise DownloadError("下载 yt-dlp 更新包失败", detail=str(exc)) from exc

        extract_dir = tmp_dir / "extracted"
        with zipfile.ZipFile(whl_path, "r") as zf:
            zf.extractall(extract_dir)

        src_pkg = extract_dir / "yt_dlp"
        if not src_pkg.exists():
            raise DownloadError("下载的 wheel 包中不包含 yt_dlp 目录")

        target_pkg = paths.plugins_dir / "yt_dlp"
        paths.plugins_dir.mkdir(parents=True, exist_ok=True)
        if target_pkg.exists():
            shutil.rmtree(target_pkg)
        shutil.move(str(src_pkg), str(target_pkg))

    ensure_plugin_yt_dlp_on_path(paths)
    for mod_name in list(sys.modules.keys()):
        if mod_name == "yt_dlp" or mod_name.startswith("yt_dlp."):
            sys.modules.pop(mod_name, None)
    importlib.import_module("yt_dlp")

    if on_log:
        on_log(f"yt-dlp 已成功更新至 {latest_version}")
    return latest_version
