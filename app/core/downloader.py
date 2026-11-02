"""yt-dlp wrapper with built-in rookie-cookies extraction, on-demand Deno/Node JS runtime, YouTube & TikTok direct fallbacks, Cookie domain binding & static inspection, domestic proxy bypass, and isolated Cookie cleanup."""

from __future__ import annotations

import atexit
import copy
import html
import importlib
import json
import os
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

from app.core import http_client, rookie_bridge
from app.core.cancel import CancelToken
from app.core.errors import CancelledError, DownloadError
from app.core.site_cookies import cookie_domains_for, inspect_netscape, resolve_site
from app.paths import PATHS, AppPaths

ProgressCallback = Callable[[float, str], None]
LogCallback = Callable[[str], None]

_YOUTUBE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
_YOUTUBE_URL_ID_RE = re.compile(
    r"(?:v=|/(?:embed|shorts|live|v)/|youtu\.be/)([A-Za-z0-9_-]{11})",
    re.IGNORECASE,
)
_TIKTOK_VIDEO_ID_RE = re.compile(r"(?:\[TikTok\]\s*|/(?:video|photo|v|embed/v2)/)(\d{15,22})", re.IGNORECASE)
_SPACE_NETSCAPE_RE = re.compile(
    r"^(\S+)\s+(TRUE|FALSE)\s+(/\S*)\s+(TRUE|FALSE)\s+(\d+)\s+(\S+)(?:\s+(.*))?$",
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

_DOMESTIC_NO_AUTO_PROXY = ("bilibili.com", "b23.tv", "douyin.com", "iqiyi.com", "qq.com", "youku.com")
_DENO_DOWNLOAD_ATTEMPTED: bool = False


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
    cookies_browser_profile: str = ""   # 例如 "Default" / "Profile 1" / Firefox 配置目录名
    cookies_app_bound: str = "disabled"  # "disabled" (default: no browser spawn) | "injection_only" | "allow_elevated_fallback"
    allow_playlist: bool = False
    ffmpeg_dir: Optional[Path] = None
    deno_path: Optional[Path] = None


@dataclass
class _DirectMediaFallback:
    info: MediaInfo
    stream_url: str
    ext: str = "mp4"
    headers: Optional[Dict[str, str]] = None


_DIRECT_MEDIA_CACHE: Dict[str, _DirectMediaFallback] = {}
_WORKING_UA_CACHE: Dict[str, str] = {}
_COOKIE_TMP_DIRS: set = set()


def is_likely_playlist_url(url: str) -> bool:
    """Return True if `url` might be a playlist, channel, or multi-part video; False for single-video links."""
    u = url.strip().lower()
    if not u.startswith(("http://", "https://", "www.")):
        return False
    if "tiktok.com" in u:
        if any(seg in u for seg in ("vt.tiktok.com", "vm.tiktok.com", "/video/", "/photo/", "/t/")):
            return False
        return True
    if ("youtube.com/watch" in u or "youtu.be/" in u or "youtube.com/shorts/" in u) and "list=" not in u:
        return False
    if any(seg in u for seg in ("instagram.com/reel/", "instagram.com/p/", "twitter.com/", "x.com/")):
        return False
    return True


def _host_in(url: str, suffixes) -> bool:
    try:
        host = (urllib.parse.urlparse(url).hostname or "").lower()
    except ValueError:
        return False
    return any(host == s or host.endswith("." + s) for s in suffixes)


def resolve_effective_proxy(explicit_proxy: str, target_url: str = "") -> str:
    """Resolve explicit proxy (always respected) or auto-detected OS proxy (bypassed for domestic sites)."""
    p = (explicit_proxy or "").strip()
    if p:                                        # 用户显式填写：始终遵从
        return p if "://" in p else f"http://{p}"
    if target_url and _host_in(target_url, _DOMESTIC_NO_AUTO_PROXY):
        return ""                                # “自动探测到的系统代理”不接管国内站点
    try:
        sys_proxies = urllib.request.getproxies()
        detected = sys_proxies.get("https") or sys_proxies.get("http") or sys_proxies.get("all") or ""
        return detected if (not detected or "://" in detected) else f"http://{detected}"
    except Exception:
        return ""


def ensure_plugin_yt_dlp_on_path(paths: AppPaths = PATHS) -> None:
    """Prepend `plugins/` to `sys.path` if a hot-updated yt-dlp package exists."""
    plugin_root = paths.plugins_dir
    if (plugin_root / "yt_dlp").exists() or (plugin_root / "yt_dlp_ejs").exists():
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
    """Return strictly bound cookie domains for `target_url` via `site_cookies.cookie_domains_for`."""
    return cookie_domains_for(target_url)


def purge_stale_cookie_files(temp_dir: Path, max_age_sec: int = 3600) -> int:
    """删除超过 max_age_sec 的临时 Cookie 副本，避免凭据明文长期残留在临时目录。"""
    removed = 0
    cookie_dir = temp_dir / "cookies"
    if not cookie_dir.is_dir():
        return 0
    now = time.time()
    for f in cookie_dir.glob("cookies_session_*.txt"):
        try:
            if now - f.stat().st_mtime > max_age_sec:
                f.unlink()
                removed += 1
        except OSError:
            pass
    return removed


def _register_cookie_dir_cleanup(cookie_dir: Path) -> None:
    if cookie_dir in _COOKIE_TMP_DIRS:
        return
    _COOKIE_TMP_DIRS.add(cookie_dir)
    atexit.register(shutil.rmtree, str(cookie_dir), ignore_errors=True)


def _format_netscape_row(
    prefix: str,
    domain: str,
    path_str: str,
    secure: bool,
    exp_val: int,
    name: str,
    value: str,
) -> str:
    """Format a single 7-column Netscape cookie line strictly satisfying Python `MozillaCookieJar._really_load`:
    `assert (column_2 == "TRUE") == domain.startswith(".")`.
    """
    dom = domain.strip()
    if dom and not dom.startswith(".") and dom.count(".") == 1 and not dom.startswith("localhost"):
        dom = "." + dom
    inc_sub = "TRUE" if dom.startswith(".") else "FALSE"
    sec_str = "TRUE" if secure else "FALSE"
    p_str = path_str.strip() or "/"
    return f"{prefix}{dom}\t{inc_sub}\t{p_str}\t{sec_str}\t{exp_val}\t{name}\t{value}"


def normalize_cookie_text(
    raw_text: str,
    target_url: str = "",
    on_log: Optional[LogCallback] = None,
) -> Tuple[str, int, List[str]]:
    """Parse Netscape (tab/space), JSON, or F12 raw Cookie header text and return `(netscape_text, count, matched_sites)`."""
    text = raw_text.strip("\r\n ")
    if not text:
        return "", 0, []

    future_exp = int(time.time()) + 86400 * 365
    rows: List[str] = []
    domains_seen: List[str] = []

    # Case 1: JSON Cookie export from Cookie-Editor / EditThisCookie
    if text.startswith("[") or text.startswith("{"):
        try:
            parsed = json.loads(text)
            items = parsed if isinstance(parsed, list) else (parsed.get("cookies") or [])
            if isinstance(items, list) and items:
                for c in items:
                    if not isinstance(c, dict):
                        continue
                    domain = str(c.get("domain") or "").strip()
                    name = str(c.get("name") or "").strip()
                    value = str(c.get("value") or "")
                    if not domain or not name:
                        continue
                    prefix = "#HttpOnly_" if c.get("httpOnly") else ""
                    path_str = str(c.get("path") or "/").strip() or "/"
                    secure = bool(c.get("secure"))
                    exp_raw = c.get("expirationDate") or c.get("expires") or c.get("expiry") or 0
                    try:
                        exp_int = int(float(exp_raw))
                        if exp_int <= 0:
                            exp_int = future_exp
                    except (ValueError, TypeError):
                        exp_int = future_exp
                    rows.append(_format_netscape_row(prefix, domain, path_str, secure, exp_int, name, value))
                    domains_seen.append(domain.lower())
        except Exception:
            pass

    # Case 2 & 3: Netscape 7-column tab-separated or space-separated lines (preserving trailing \t for empty values!)
    if not rows:
        for raw_ln in text.splitlines():
            ln = raw_ln.rstrip("\r\n")
            s_check = ln.strip()
            if not s_check:
                continue
            is_httponly = s_check.startswith("#HttpOnly_")
            if s_check.startswith("#") and not is_httponly:
                continue

            line_clean = ln.lstrip(" ")
            prefix = "#HttpOnly_" if is_httponly else ""
            body = line_clean[len("#HttpOnly_") :] if is_httponly else line_clean

            parts = body.split("\t")
            if len(parts) < 7:
                m_space = _SPACE_NETSCAPE_RE.match(body.strip())
                if m_space:
                    parts = [
                        m_space.group(1),
                        m_space.group(2),
                        m_space.group(3),
                        m_space.group(4),
                        m_space.group(5),
                        m_space.group(6),
                        m_space.group(7) or "",
                    ]

            if len(parts) >= 7:
                dom = parts[0].strip()
                inc_sub_orig = parts[1].strip().upper()
                path_s = parts[2].strip() or "/"
                sec_s = parts[3].strip().upper() == "TRUE"
                exp_s = parts[4].strip()
                name_s = parts[5].strip()
                val_s = "\t".join(parts[6:])
                if not dom or not name_s:
                    continue
                if inc_sub_orig == "TRUE" and not dom.startswith("."):
                    dom = "." + dom
                try:
                    exp_val = int(float(exp_s))
                    if exp_val <= 0:
                        exp_val = future_exp
                except (ValueError, TypeError):
                    exp_val = future_exp
                rows.append(_format_netscape_row(prefix, dom, path_s, sec_s, exp_val, name_s, val_s))
                domains_seen.append(dom.lower())

    # Case 4: Raw HTTP `Cookie:` header string copied from browser F12 DevTools (`k1=v1; k2=v2`)
    if not rows:
        header_body = text
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
                domains = cookie_domains_for(target_url)
                if not domains:
                    if on_log:
                        on_log(
                            "[Cookies 警告] 无法根据链接确定 Cookie 所属站点，原始 Cookie 字符串未被使用；"
                            "请改用 cookies.txt 导出文件，或先输入要下载的视频链接。"
                        )
                    return "", 0, []
                for dom in domains:
                    for k, v in kv_pairs:
                        rows.append(_format_netscape_row("", dom, "/", True, future_exp, k, v))
                        domains_seen.append(dom.lower())

    if not rows:
        return "", 0, []

    site_map = [
        ("YouTube/Google", ("youtube.com", "google.com")),
        ("TikTok", ("tiktok.com",)),
        ("Bilibili", ("bilibili.com",)),
        ("Instagram", ("instagram.com",)),
        ("X/Twitter", ("x.com", "twitter.com")),
    ]
    matched_sites: List[str] = []
    for label, dom_keys in site_map:
        if any(any(dk in d for dk in dom_keys) for d in domains_seen):
            matched_sites.append(label)

    header = "# Netscape HTTP Cookie File\n# Isolated & validated by Moonlight Whisper\n\n"
    return header + "\n".join(rows) + "\n", len(rows), matched_sites


def import_cookies_from_text(
    raw_text: str,
    paths: AppPaths = PATHS,
    target_url: str = "",
) -> Tuple[Path, int, List[str]]:
    """Convert clipboard/raw cookie text (Netscape, JSON, or F12 Cookie header with target_url) and save to `data_root/cookies.txt`."""
    netscape_text, count, matched_sites = normalize_cookie_text(raw_text, target_url=target_url)
    if count <= 0 or not netscape_text:
        raise DownloadError(
            "未能从剪贴板识别到有效的 Cookie 内容（若粘贴的是原始 Cookie 请求头，需同时指定目标站点链接）。\n"
            "推荐直接启用「从浏览器读取 (rookie-cookies)」或使用「登录并获取 Cookies…」。"
        )
    paths.ensure_runtime_dirs()
    dest = paths.data_root / "cookies.txt"
    dest.write_text(netscape_text, encoding="utf-8")
    try:
        os.chmod(dest, 0o600)
    except OSError:
        pass
    return dest, count, matched_sites


def prepare_cookie_file(
    cookie_path: Path,
    temp_dir: Path,
    on_log: Optional[LogCallback] = None,
    target_url: str = "",
) -> Optional[Path]:
    """Validate and copy a user-supplied Cookies file into a fresh isolated Netscape HTTP Cookie File under `temp_dir/cookies/`."""
    if not cookie_path.exists() or not cookie_path.is_file():
        if on_log:
            on_log(f"[Cookies 警告] 指定的 Cookies 文件不存在：{cookie_path}")
        return None

    try:
        raw_text = _read_text_multi_encoding(cookie_path)
    except OSError as exc:
        if on_log:
            on_log(f"[Cookies 警告] 无法读取 Cookies 文件 ({cookie_path})：{exc}")
        return None

    if not raw_text.strip():
        if on_log:
            on_log(f"[Cookies 警告] Cookies 文件为空：{cookie_path}")
        return None

    cookie_dir = temp_dir / "cookies"
    cookie_dir.mkdir(parents=True, exist_ok=True)
    purge_stale_cookie_files(temp_dir)
    _register_cookie_dir_cleanup(cookie_dir)
    isolated_out = cookie_dir / f"cookies_session_{uuid.uuid4().hex[:8]}.txt"

    netscape_text, count, matched_sites = normalize_cookie_text(
        raw_text,
        target_url=target_url,
        on_log=on_log,
    )
    if count <= 0 or not netscape_text:
        if on_log:
            on_log(
                f"[Cookies 警告] 文件 {cookie_path.name} 未包含可识别的 Cookie，建议使用「偏好设置 -> 从浏览器读取 (rookie-cookies)」或重新导出。"
            )
        return None

    isolated_out.write_text(netscape_text, encoding="utf-8")
    try:
        os.chmod(isolated_out, 0o600)
    except OSError:
        pass
    if on_log:
        sites_str = f" (含 {', '.join(matched_sites)})" if matched_sites else ""
        on_log(f"[Cookies] 已加载并隔离保护 Cookies 文件：{cookie_path.name}（共 {count} 条记录{sites_str}）")
    return isolated_out


def ensure_deno_binary(
    proxy: str = "",
    on_log: Optional[LogCallback] = None,
    token: Optional[CancelToken] = None,
    paths: AppPaths = PATHS,
) -> Optional[Path]:
    """Auto-download `deno.exe` on Windows into `paths.user_bin_dir` if no JS runtime (`deno`/`node`/`bun`) is present."""
    global _DENO_DOWNLOAD_ATTEMPTED
    existing = paths.find_binary("deno")
    if existing and existing.exists():
        return existing
    if sys.platform != "win32" or _DENO_DOWNLOAD_ATTEMPTED:
        return None

    _DENO_DOWNLOAD_ATTEMPTED = True
    eff_proxy = resolve_effective_proxy(proxy)
    deno_urls = [
        "https://github.com/denoland/deno/releases/latest/download/deno-x86_64-pc-windows-msvc.zip",
        "https://mirror.ghproxy.com/https://github.com/denoland/deno/releases/latest/download/deno-x86_64-pc-windows-msvc.zip",
    ]
    dest_dir = paths.user_bin_dir
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_exe = dest_dir / "deno.exe"

    for dl_url in deno_urls:
        try:
            if on_log:
                on_log("[YouTube 运行时] 本地未找到 deno.exe，正在自动下载 YouTube JS 解算组件 (deno.exe)...")
            with tempfile.TemporaryDirectory(prefix="deno_dl_") as tmp:
                zip_path = Path(tmp) / "deno.zip"
                http_client.stream_download_with_resume(
                    url=dl_url,
                    dest_file=zip_path,
                    proxy=eff_proxy,
                    token=token,
                    timeout=35,
                )
                with zipfile.ZipFile(zip_path, "r") as zf:
                    zf.extract("deno.exe", Path(tmp))
                extracted = Path(tmp) / "deno.exe"
                if extracted.is_file():
                    shutil.move(str(extracted), str(dest_exe))
                    if on_log:
                        on_log(f"[YouTube 运行时] 已自动安装 deno.exe：{dest_exe}")
                    return dest_exe
        except CancelledError:
            raise
        except Exception:
            continue
    return None


def _apply_js_runtime_opts(
    ydl_opts: Dict[str, Any],
    deno_bin: Optional[Path],
    paths: AppPaths = PATHS,
    proxy: str = "",
    target_url: str = "",
    token: Optional[CancelToken] = None,
    on_log: Optional[LogCallback] = None,
) -> None:
    """Configure `js_runtimes` (`deno`, `node`, `bun`, or `qjs`) and `remote_components` for YouTube EJS solving."""
    resolved_deno = deno_bin if (deno_bin and deno_bin.exists()) else paths.find_binary("deno")
    resolved_node = paths.find_binary("node")
    resolved_bun = paths.find_binary("bun")

    # If downloading YouTube and neither deno nor node nor bun is available, try auto-installing deno.exe on Windows
    if (
        resolved_deno is None
        and resolved_node is None
        and resolved_bun is None
        and _is_youtube_url(target_url)
    ):
        resolved_deno = ensure_deno_binary(proxy=proxy, on_log=on_log, token=token, paths=paths)

    if resolved_deno and resolved_deno.exists():
        ydl_opts["js_runtimes"] = {"deno": {"path": str(resolved_deno)}}
    elif resolved_node and resolved_node.exists():
        ydl_opts["js_runtimes"] = {"node": {"path": str(resolved_node)}}
        if on_log and _is_youtube_url(target_url):
            on_log(f"[YouTube 运行时] 已启用系统 Node.js 作为 JS 挑战解析引擎：{resolved_node}")
    elif resolved_bun and resolved_bun.exists():
        ydl_opts["js_runtimes"] = {"bun": {"path": str(resolved_bun)}}
    else:
        ydl_opts["js_runtimes"] = {"deno": {}, "node": {}}
        if on_log and _is_youtube_url(target_url):
            on_log("[YouTube 警告] 未找到 deno 或 node，YouTube 部分格式可能受限；程序将自动启用多客户端与备用直链通道。")

    try:
        import yt_dlp_ejs  # noqa: F401
        has_ejs = True
    except ImportError:
        has_ejs = False
    if not has_ejs:
        ydl_opts["remote_components"] = ["ejs:github"]
        if on_log and _is_youtube_url(target_url):
            on_log("[YouTube] 未找到 yt-dlp-ejs，已允许从 GitHub 获取挑战解析脚本（兜底）")


def _find_cached_cookie_fallback(paths: AppPaths, target_url: str) -> Optional[Path]:
    """Check if the user has a previously exported/imported cookie file in `data_root/cookies/<site>.txt` or `data_root/cookies.txt`."""
    candidates: List[Path] = []
    site = resolve_site(target_url)
    if site is not None:
        candidates.append(paths.data_root / "cookies" / f"{site.key}.txt")
    candidates.append(paths.data_root / "cookies.txt")
    for c in candidates:
        if c.is_file() and c.stat().st_size > 0:
            return c
    return None


def _build_common_ydl_opts(
    opts: DownloadOptions,
    on_log: Optional[LogCallback] = None,
    paths: AppPaths = PATHS,
    target_url: str = "",
    token: Optional[CancelToken] = None,
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

    eff_proxy = resolve_effective_proxy(opts.proxy, target_url=target_url)
    if eff_proxy:
        ydl_opts["proxy"] = eff_proxy
        if on_log and not opts.proxy.strip():
            on_log(f"[网络] 已自动检测并启用系统代理：{eff_proxy}")

    deno_bin = opts.deno_path or paths.find_binary("deno")
    _apply_js_runtime_opts(
        ydl_opts,
        deno_bin,
        paths=paths,
        proxy=opts.proxy,
        target_url=target_url,
        token=token,
        on_log=on_log,
    )

    loaded_cookiefile = False
    if opts.cookies_file.strip():
        cookie_path = Path(opts.cookies_file.strip()).expanduser()
        prepared = prepare_cookie_file(cookie_path, paths.temp_dir, on_log=on_log, target_url=target_url)
        if prepared is not None and prepared.exists():
            ydl_opts["cookiefile"] = str(prepared)
            loaded_cookiefile = True
            if target_url and on_log:
                report = inspect_netscape(prepared, target_url)
                on_log(f"[Cookies 体检] {report.summary()}")
        elif on_log:
            on_log("[Cookies 警告] Cookies 文件未能加载，本次将以未登录状态请求。")

    if opts.cookies_from_browser.strip() and not loaded_cookiefile:
        browser_name = opts.cookies_from_browser.strip().lower()
        profile = opts.cookies_browser_profile.strip() or None
        if rookie_bridge.is_available():
            try:
                purge_stale_cookie_files(paths.temp_dir)
                _register_cookie_dir_cleanup(paths.temp_dir / "cookies")
                rookie_cookie_file = rookie_bridge.extract_to_netscape_file(
                    browser=browser_name,
                    profile=opts.cookies_browser_profile.strip(),
                    target_url=target_url,
                    temp_dir=paths.temp_dir,
                    app_bound=opts.cookies_app_bound,
                    token=token,
                    on_log=on_log,
                )
                if rookie_cookie_file is not None and rookie_cookie_file.exists():
                    ydl_opts["cookiefile"] = str(rookie_cookie_file)
                    opts.cookies_file = str(rookie_cookie_file)
                    loaded_cookiefile = True
                else:
                    ydl_opts["_rookie_cookie_dropped"] = True
            except CancelledError:
                raise
            except DownloadError as r_exc:
                ydl_opts["_rookie_cookie_dropped"] = True
                ydl_opts["_rookie_error_msg"] = r_exc.user_message
            except Exception as unexpected_exc:
                msg = rookie_bridge.translate_rookie_error(unexpected_exc, browser_id=browser_name)
                if on_log:
                    on_log(f"[Cookies 警告] {msg}")
                ydl_opts["_rookie_cookie_dropped"] = True
                ydl_opts["_rookie_error_msg"] = msg

            # If browser live extraction failed (e.g. browser running & locking DB or v20), check if a cached cookies.txt exists
            if not loaded_cookiefile:
                cached_fb = _find_cached_cookie_fallback(paths, target_url)
                if cached_fb is not None:
                    prepared_fb = prepare_cookie_file(cached_fb, paths.temp_dir, on_log=None, target_url=target_url)
                    if prepared_fb is not None and prepared_fb.exists():
                        ydl_opts["cookiefile"] = str(prepared_fb)
                        opts.cookies_file = str(prepared_fb)
                        loaded_cookiefile = True
                        ydl_opts.pop("_rookie_cookie_dropped", None)
                        ydl_opts.pop("_rookie_error_msg", None)
                        if on_log:
                            on_log(f"[Cookies 自动回退] 浏览器实时提取受限，已自动挂载本地缓存 Cookies：{cached_fb.name}")
        else:
            ydl_opts["cookiesfrombrowser"] = (browser_name, profile, None, None)

    if _is_tiktok_url(target_url) and "tiktok" in _WORKING_UA_CACHE:
        ydl_opts["http_headers"] = {"User-Agent": _WORKING_UA_CACHE["tiktok"]}

    return ydl_opts


def _is_tiktok_url(url: str) -> bool:
    return "tiktok.com" in (url or "").lower()


def _is_youtube_url(url: str) -> bool:
    u = (url or "").lower()
    return "youtube.com" in u or "youtu.be" in u or "youtube-nocookie.com" in u


def _extract_youtube_video_id(url: str) -> Optional[str]:
    if not url:
        return None
    m = _YOUTUBE_URL_ID_RE.search(url)
    if m:
        return m.group(1)
    parsed = urllib.parse.urlparse(url)
    qs_v = urllib.parse.parse_qs(parsed.query).get("v")
    if qs_v and _YOUTUBE_ID_RE.match(qs_v[0]):
        return qs_v[0]
    return None


def _extract_tiktok_video_id(url: str, error_text: str = "") -> Optional[str]:
    for text in (url, error_text):
        if not text:
            continue
        m = _TIKTOK_VIDEO_ID_RE.search(text)
        if m:
            return m.group(1)
    return None


def _fallback_youtube_direct(
    url: str,
    video_id: Optional[str],
    proxy: str,
    on_log: Optional[LogCallback] = None,
) -> Optional[_DirectMediaFallback]:
    """Resolve YouTube video metadata and direct audio stream via Piped / Invidious public API cluster
    when the user's proxy IP triggers YouTube's 'Sign in to confirm you're not a bot' check without cookies.
    """
    if url in _DIRECT_MEDIA_CACHE:
        return _DIRECT_MEDIA_CACHE[url]

    vid = video_id or _extract_youtube_video_id(url)
    if not vid:
        return None

    eff_proxy = resolve_effective_proxy(proxy, target_url=url)

    # 1. Try Piped API instances
    piped_bases = (
        "https://pipedapi.kavin.rocks",
        "https://pipedapi.tokhmi.xyz",
        "https://pipedapi.moomoo.me",
        "https://piped-api.garudalinux.org",
    )
    if on_log:
        on_log(f"[YouTube 备用通道 1/2] 正在通过免登录镜像接口解析音频流 (ID: {vid})...")

    for base in piped_bases:
        try:
            resp = http_client.fetch_json(f"{base}/streams/{vid}", proxy=eff_proxy, timeout=15)
            if not isinstance(resp, dict):
                continue
            audio_streams = resp.get("audioStreams")
            if not isinstance(audio_streams, list) or not audio_streams:
                continue
            valid_streams = [
                s for s in audio_streams if isinstance(s, dict) and str(s.get("url") or "").startswith("http")
            ]
            if not valid_streams:
                continue
            valid_streams.sort(key=lambda s: int(s.get("bitrate") or 0), reverse=True)
            best = valid_streams[0]
            stream_url = str(best["url"])
            mime = str(best.get("mimeType") or "").lower()
            ext = "m4a" if ("mp4" in mime or "m4a" in mime) else "webm"
            title = str(resp.get("title") or f"YouTube_{vid}").strip() or f"YouTube_{vid}"
            uploader = str(resp.get("uploader") or "YouTubeCreator").strip()
            duration = float(resp.get("duration") or 0.0)
            media_info = MediaInfo(
                url=url,
                id=vid,
                title=title,
                uploader=uploader,
                duration=duration,
                extractor="YouTubePiped",
                webpage_url=url,
            )
            resolved = _DirectMediaFallback(
                info=media_info,
                stream_url=stream_url,
                ext=ext,
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
            )
            _DIRECT_MEDIA_CACHE[url] = resolved
            if on_log:
                on_log(f"[YouTube 备用通道] 成功解析音频直链：《{title}》 (ID: {vid})")
            return resolved
        except Exception:
            continue

    # 2. Try Invidious API instances with `?local=true` (proxied stream avoids Google IP bot block)
    invidious_bases = (
        "https://inv.tux.pizza",
        "https://invidious.nerdvpn.de",
        "https://inv.nadeko.net",
        "https://yewtu.be",
    )
    if on_log:
        on_log(f"[YouTube 备用通道 2/2] 正在尝试 Invidious 节点提取音频流 (ID: {vid})...")

    for base in invidious_bases:
        try:
            resp = http_client.fetch_json(f"{base}/api/v1/videos/{vid}?local=true", proxy=eff_proxy, timeout=15)
            if not isinstance(resp, dict):
                continue
            formats = resp.get("adaptiveFormats") or resp.get("formatStreams") or []
            if not isinstance(formats, list) or not formats:
                continue
            audio_candidates = []
            for f in formats:
                if not isinstance(f, dict):
                    continue
                u = str(f.get("url") or "").strip()
                if not u:
                    continue
                if u.startswith("/"):
                    u = base + u
                ftype = str(f.get("type") or "").lower()
                if "audio" in ftype or "mp4" in ftype:
                    audio_candidates.append((int(f.get("bitrate") or 0), u, ftype))
            if not audio_candidates:
                continue
            audio_candidates.sort(key=lambda x: x[0], reverse=True)
            _, stream_url, ftype = audio_candidates[0]
            ext = "m4a" if "mp4" in ftype else "webm"
            title = str(resp.get("title") or f"YouTube_{vid}").strip() or f"YouTube_{vid}"
            uploader = str(resp.get("author") or "YouTubeCreator").strip()
            duration = float(resp.get("lengthSeconds") or 0.0)
            media_info = MediaInfo(
                url=url,
                id=vid,
                title=title,
                uploader=uploader,
                duration=duration,
                extractor="YouTubeInvidious",
                webpage_url=url,
            )
            resolved = _DirectMediaFallback(
                info=media_info,
                stream_url=stream_url,
                ext=ext,
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
            )
            _DIRECT_MEDIA_CACHE[url] = resolved
            if on_log:
                on_log(f"[YouTube 备用通道] 成功从 Invidious 节点获取音频流：《{title}》 (ID: {vid})")
            return resolved
        except Exception:
            continue

    return None


def _fallback_tiktok_direct(
    url: str,
    video_id: Optional[str],
    proxy: str,
    on_log: Optional[LogCallback] = None,
) -> Optional[_DirectMediaFallback]:
    """Resolve TikTok video metadata and direct stream URL via TikWM API or TikTok Embed v2 page."""
    if url in _DIRECT_MEDIA_CACHE:
        return _DIRECT_MEDIA_CACHE[url]

    eff_proxy = resolve_effective_proxy(proxy, target_url=url)

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
            urls_found = re.findall(r'https?:\\?/\\?/[^"\'\s<>]+?(?:tiktokcdn|tiktokv)[^"\'\s<>]+', page_text)
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
    """Run `ydl.extract_info(url, download=download)` with automatic fallbacks."""
    current_opts = copy.copy(ydl_opts)
    cookies_dropped = bool(current_opts.pop("_rookie_cookie_dropped", False))
    rookie_err_msg = str(current_opts.pop("_rookie_error_msg", "") or "")

    try:
        with yt_dlp.YoutubeDL(current_opts) as ydl:
            return ydl.extract_info(url, download=download), ydl
    except CancelledError:
        raise
    except Exception as first_exc:
        if cookies_dropped:
            first_exc.cookies_dropped = True  # type: ignore[attr-defined]
        if rookie_err_msg:
            first_exc.rookie_error_msg = rookie_err_msg  # type: ignore[attr-defined]
        err_str = str(first_exc)
        err_lower = err_str.lower()

        # Fallback 1: Running Edge/Chrome on Windows locks the Cookie database (#7271)
        if "could not copy" in err_lower and "cookie database" in err_lower and "cookiesfrombrowser" in current_opts:
            if on_log:
                on_log(
                    "[Cookies 回退] 浏览器正在运行并独占锁定了 Cookie 数据库，"
                    "正在尝试免浏览器 Cookies 匿名重试..."
                )
            current_opts.pop("cookiesfrombrowser", None)
            cookies_dropped = True
            try:
                with yt_dlp.YoutubeDL(current_opts) as ydl:
                    return ydl.extract_info(url, download=download), ydl
            except CancelledError:
                raise
            except Exception as retry_exc:
                retry_exc.cookies_dropped = True  # type: ignore[attr-defined]
                first_exc = retry_exc
                err_str = str(first_exc)
                err_lower = err_str.lower()

        # Fallback 2: YouTube multi-client & cookie-free `web_embedded,tv,mweb,web_safari,android_vr,ios` fallback
        if _is_youtube_url(url) and "failed to decrypt with dpapi" not in err_lower and not (
            "could not find" in err_lower and "cookies database" in err_lower
        ):
            had_cookies = "cookiefile" in current_opts or "cookiesfrombrowser" in current_opts

            # 2A: If cookies were used, try cookie-compatible player_clients (`web`, `mweb`, `tv`, `web_safari`, `ios`)
            if had_cookies:
                if on_log:
                    on_log("[YouTube 兼容重试 1/2] 正在切换 YouTube 多客户端 (web/mweb/tv/web_safari) 与复合流模式重试...")
                yt_cookie_opts = copy.copy(current_opts)
                if opts.cookies_file.strip():
                    fresh_c = prepare_cookie_file(
                        Path(opts.cookies_file.strip()).expanduser(),
                        paths.temp_dir,
                        on_log=None,
                        target_url=url,
                    )
                    if fresh_c and fresh_c.exists():
                        yt_cookie_opts["cookiefile"] = str(fresh_c)
                yt_cookie_opts["extractor_args"] = {
                    "youtube": {"player_client": ["web", "mweb", "tv", "web_safari", "ios"]}
                }
                if download:
                    yt_cookie_opts["format"] = "bestaudio/best*"
                try:
                    with yt_dlp.YoutubeDL(yt_cookie_opts) as ydl:
                        return ydl.extract_info(url, download=download), ydl
                except CancelledError:
                    raise
                except Exception as yt_c_exc:
                    if cookies_dropped:
                        yt_c_exc.cookies_dropped = True  # type: ignore[attr-defined]
                    if rookie_err_msg:
                        yt_c_exc.rookie_error_msg = rookie_err_msg  # type: ignore[attr-defined]
                    first_exc = yt_c_exc

            # 2B: Try cookie-free `web_embedded,tv,mweb,web_safari,android_vr,ios`
            if on_log:
                on_log("[YouTube 兼容重试 2/2] 正在尝试 YouTube 免 Cookie 嵌入式/移动端通道 (web_embedded / tv / android_vr / ios)...")
            yt_nocookie_opts = copy.copy(current_opts)
            yt_nocookie_opts.pop("cookiefile", None)
            yt_nocookie_opts.pop("cookiesfrombrowser", None)
            yt_nocookie_opts["extractor_args"] = {
                "youtube": {"player_client": ["web_embedded", "tv", "mweb", "web_safari", "android_vr", "ios"]}
            }
            if download:
                yt_nocookie_opts["format"] = "bestaudio/best*"
            try:
                with yt_dlp.YoutubeDL(yt_nocookie_opts) as ydl:
                    return ydl.extract_info(url, download=download), ydl
            except CancelledError:
                raise
            except Exception as yt_nc_exc:
                if cookies_dropped:
                    yt_nc_exc.cookies_dropped = True  # type: ignore[attr-defined]
                if rookie_err_msg:
                    yt_nc_exc.rookie_error_msg = rookie_err_msg  # type: ignore[attr-defined]
                first_exc = yt_nc_exc

        # Fallback 3: TikTok webpage anti-bot challenge (`Unexpected response from webpage request`)
        if _is_tiktok_url(url) and (
            "unexpected response from webpage request" in err_lower
            or "unable to extract" in err_lower
            or "impersonate" in err_lower
            or "status code 0" in err_lower
            or "expecting value" in err_lower
        ):
            vid = _extract_tiktok_video_id(url, err_str)
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
                    if cookies_dropped:
                        tk_exc.cookies_dropped = True  # type: ignore[attr-defined]
                    if rookie_err_msg:
                        tk_exc.rookie_error_msg = rookie_err_msg  # type: ignore[attr-defined]
                    first_exc = tk_exc

        raise first_exc


def _translate_ydl_exception(exc: BaseException, has_cookies: bool = False) -> DownloadError:
    text = str(exc)
    lower = text.lower()

    if "failed to decrypt with dpapi" in lower:
        return DownloadError(
            "无法解密 Chrome/Edge 的 Cookies：新版 Chromium（127+）启用了 App-Bound 加密，yt-dlp 无法从外部读取。"
            "请确认已安装内置 rookie-cookies 组件，或改用 Firefox、「登录并获取 Cookies」、导入 cookies.txt。",
            detail=text,
        )

    if "could not find" in lower and "cookies database" in lower:
        return DownloadError(
            "未找到所选浏览器的 Cookies 数据库，请确认浏览器已安装、已登录，并在设置中选择正确的配置文件名（如 Profile 1）。",
            detail=text,
        )

    if "could not copy" in lower and "cookie database" in lower:
        return DownloadError(
            "浏览器正在运行并独占锁定了 Cookies 数据库（#7271）。请先关闭浏览器，或使用内置 rookie-cookies / Firefox /「登录并获取 Cookies」。",
            detail=text,
        )

    if (
        "no supported javascript runtime" in lower
        or "n challenge solving failed" in lower
        or "no usable challenge solver" in lower
    ):
        return DownloadError(
            "YouTube 需要 JavaScript 运行时（deno）和 yt-dlp-ejs 组件来解析视频地址，当前环境缺失或版本不匹配。"
            "请在 bin/ 放入 deno.exe 并点击「更新 yt-dlp」，详见 README。",
            detail=text,
        )

    if "does not look like a netscape format cookies file" in lower:
        return DownloadError(
            "导入的 Cookies 文件格式无效。请打开「偏好设置 -> 网络与认证」使用「登录并获取 Cookies」或「📋 从剪贴板导入」。",
            detail=text,
        )

    if "unexpected response from webpage request" in lower or "impersonate target is available" in lower:
        return DownloadError(
            "目标站点（如 TikTok / Instagram）触发了深层反爬拦截。"
            "请检查「偏好设置 -> 代理地址」（如 http://127.0.0.1:7890）是否可用，并确认该链接在当前代理节点的浏览器中可正常播放。",
            detail=text,
        )

    if any(pat in lower for pat in _AGE_OR_AUTH_PATTERNS):
        rookie_err = getattr(exc, "rookie_error_msg", "")
        if rookie_err:
            msg = f"{rookie_err}（匿名重试后该视频仍要求登录）"
        elif getattr(exc, "cookies_dropped", False):
            msg = (
                "当前代理节点 IP 被 YouTube 要求验证登录，且 Edge 启用了 v20 (App-Bound) 加密无法在零弹窗模式下后台解密。"
                "请打开「偏好设置 -> 网络与认证」点击「登录并获取 Cookies…」登录一次（或复制 Cookie 点击「📋 从剪贴板导入」/ 改用 Firefox）。"
            )
        elif has_cookies:
            msg = "已使用你导入的 Cookies，但站点仍要求登录：Cookies 可能已过期/被轮换，或不属于该站点账号。请重新导出（YouTube 请用无痕窗口导出并立刻关闭）。"
        else:
            msg = "该视频需要登录或年龄验证，请在「偏好设置 -> 网络与认证」中配置 Cookies。"
        return DownloadError(msg, detail=text)

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
        cookies_browser_profile=opts.cookies_browser_profile,
        cookies_app_bound=opts.cookies_app_bound,
        allow_playlist=False,
        ffmpeg_dir=opts.ffmpeg_dir,
        deno_path=opts.deno_path,
    )
    items = extract_playlist_or_info(url, single_opts, token=token, on_log=on_log, paths=paths)
    if single_opts.cookies_file and not opts.cookies_file:
        opts.cookies_file = single_opts.cookies_file
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
    ydl_opts = _build_common_ydl_opts(opts, on_log=on_log, paths=paths, target_url=url, token=token)
    ydl_opts["extract_flat"] = "in_playlist" if opts.allow_playlist else False
    has_cookies = bool("cookiefile" in ydl_opts or "cookiesfrombrowser" in ydl_opts)

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
        if _is_youtube_url(url) and not opts.allow_playlist:
            yt_vid = _extract_youtube_video_id(url)
            yt_fb = _fallback_youtube_direct(url, video_id=yt_vid, proxy=opts.proxy, on_log=on_log)
            if yt_fb is not None:
                return [yt_fb.info]
        raise _translate_ydl_exception(exc, has_cookies=has_cookies) from exc

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
    eff_proxy = resolve_effective_proxy(proxy, target_url=fb.info.url)
    if on_log:
        on_log(f"[备用通道] 开始直接下载音视频流：{dest_file.name}")

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

    ydl_opts = _build_common_ydl_opts(opts, on_log=on_log, paths=paths, target_url=url, token=token)
    ydl_opts["noplaylist"] = True
    ydl_opts["format"] = "bestaudio/best*"
    ydl_opts["outtmpl"] = str(workdir / "%(id)s.%(ext)s")
    ydl_opts["progress_hooks"] = [_progress_hook]
    has_cookies = bool("cookiefile" in ydl_opts or "cookiesfrombrowser" in ydl_opts)

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
        if _is_youtube_url(url):
            yt_vid = _extract_youtube_video_id(url)
            yt_fb = _fallback_youtube_direct(url, video_id=yt_vid, proxy=opts.proxy, on_log=on_log)
            if yt_fb is not None:
                _DIRECT_MEDIA_CACHE.pop(url, None)
                return _download_direct_media_fallback(
                    fb=yt_fb,
                    workdir=workdir,
                    proxy=opts.proxy,
                    on_progress=on_progress,
                    on_log=on_log,
                    token=token,
                )
        raise _translate_ydl_exception(exc, has_cookies=has_cookies) from exc

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


def diagnose_youtube_runtime(paths: AppPaths = PATHS) -> Dict[str, str]:
    """供「关于/诊断」按钮和 `cli.py --diagnose` 使用。"""
    yt = _import_yt_dlp(paths)
    report: Dict[str, str] = {"yt-dlp": str(getattr(yt.version, "__version__", "?"))}
    deno = paths.find_binary("deno")
    node = paths.find_binary("node")
    report["deno"] = str(deno) if deno else (f"未找到 (已启用 node: {node})" if node else "未找到 (首次解析 YouTube 时自动下载)")
    try:
        import yt_dlp_ejs
        report["yt-dlp-ejs"] = str(getattr(yt_dlp_ejs, "version", "?"))
    except ImportError:
        report["yt-dlp-ejs"] = "未安装"
    try:
        from yt_dlp.extractor.youtube.jsc._builtin.vendor import _info
        report["yt-dlp 期望的 EJS 版本"] = str(_info.VERSION)
    except Exception:
        report["yt-dlp 期望的 EJS 版本"] = "未知"
    report["rookie-cookies (浏览器 Cookie 引擎)"] = rookie_bridge.get_version()
    return report


def update_ejs_from_pypi(
    pypi_mirror: str,
    proxy: str,
    on_log: Optional[LogCallback] = None,
    token: Optional[CancelToken] = None,
    paths: AppPaths = PATHS,
) -> str:
    """热更新 yt-dlp 时同步更新 yt-dlp-ejs（纯 Python wheel）。放在 update_yt_dlp_from_pypi() 成功之后调用。"""
    eff_proxy = resolve_effective_proxy(proxy)
    info = http_client.resolve_pypi_wheel("yt-dlp-ejs", mirror=pypi_mirror, proxy=eff_proxy, win_amd64_only=False)
    with tempfile.TemporaryDirectory(prefix="ejs_update_") as tmp:
        whl = Path(tmp) / str(info["filename"])
        http_client.stream_download_with_resume(
            url=str(info["url"]),
            dest_file=whl,
            proxy=eff_proxy,
            expected_sha256=str(info.get("digests", {}).get("sha256") or ""),
            token=token,
            timeout=60,
        )
        out = Path(tmp) / "x"
        with zipfile.ZipFile(whl) as zf:
            zf.extractall(out)
        if not (out / "yt_dlp_ejs").exists():
            raise DownloadError("yt-dlp-ejs wheel 中没有 yt_dlp_ejs 目录")
        paths.plugins_dir.mkdir(parents=True, exist_ok=True)
        target = paths.plugins_dir / "yt_dlp_ejs"
        staging = paths.plugins_dir / f"yt_dlp_ejs_staging_{uuid.uuid4().hex[:6]}"
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        shutil.move(str(out / "yt_dlp_ejs"), str(staging))
        if target.exists():
            shutil.rmtree(target)
        os.replace(str(staging), str(target))
    for name in list(sys.modules):
        if name == "yt_dlp_ejs" or name.startswith("yt_dlp_ejs."):
            sys.modules.pop(name, None)
    if on_log:
        on_log(f"yt-dlp-ejs 已更新至 {info.get('version')}")
    return str(info.get("version"))


def update_yt_dlp_from_pypi(
    pypi_mirror: str = "tuna",
    proxy: str = "",
    on_log: Optional[LogCallback] = None,
    token: Optional[CancelToken] = None,
    paths: AppPaths = PATHS,
) -> str:
    """Download the latest `yt-dlp` and `yt-dlp-ejs` wheels from PyPI/mirror into `plugins/`."""
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

        paths.plugins_dir.mkdir(parents=True, exist_ok=True)
        target_pkg = paths.plugins_dir / "yt_dlp"
        staging_pkg = paths.plugins_dir / f"yt_dlp_staging_{uuid.uuid4().hex[:6]}"
        if staging_pkg.exists():
            shutil.rmtree(staging_pkg, ignore_errors=True)
        shutil.move(str(src_pkg), str(staging_pkg))
        if target_pkg.exists():
            shutil.rmtree(target_pkg)
        os.replace(str(staging_pkg), str(target_pkg))

    try:
        update_ejs_from_pypi(pypi_mirror=pypi_mirror, proxy=proxy, on_log=on_log, token=token, paths=paths)
    except Exception as exc:
        if on_log:
            on_log(f"[提示] yt-dlp-ejs 同步更新跳过：{exc}")

    ensure_plugin_yt_dlp_on_path(paths)
    for mod_name in list(sys.modules.keys()):
        if mod_name in ("yt_dlp", "yt_dlp_ejs") or mod_name.startswith(("yt_dlp.", "yt_dlp_ejs.")):
            sys.modules.pop(mod_name, None)
    importlib.import_module("yt_dlp")

    if on_log:
        on_log(f"yt-dlp 已成功更新至 {latest_version}")
    return latest_version
