"""yt-dlp wrapper with TLS impersonation fallback, smart Cookie format conversion (Netscape/JSON), browser DB lock fallback, and self-updating."""

from __future__ import annotations

import copy
import importlib
import json
import re
import shutil
import sys
import tempfile
import urllib.parse
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from app.core import http_client
from app.core.cancel import CancelToken
from app.core.errors import CancelledError, DownloadError
from app.paths import PATHS, AppPaths

ProgressCallback = Callable[[float, str], None]
LogCallback = Callable[[str], None]

_YOUTUBE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")

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


def prepare_cookie_file(
    cookie_path: Path,
    temp_dir: Path,
    on_log: Optional[LogCallback] = None,
) -> Optional[Path]:
    """Validate and normalize a user-supplied Cookies file into Netscape HTTP Cookie File format.

    Supports:
    1. Standard Netscape `cookies.txt` (with or without `# Netscape HTTP Cookie File` header)
    2. JSON array exported by browser extensions like *Cookie-Editor* or *EditThisCookie*
    """
    if not cookie_path.exists() or not cookie_path.is_file():
        if on_log:
            on_log(f"[Cookies 警告] 指定的 Cookies 文件不存在：{cookie_path}")
        return None

    try:
        raw_text = cookie_path.read_text(encoding="utf-8-sig", errors="replace").strip()
    except OSError as exc:
        if on_log:
            on_log(f"[Cookies 警告] 无法读取 Cookies 文件 ({cookie_path})：{exc}")
        return None

    if not raw_text:
        if on_log:
            on_log(f"[Cookies 警告] Cookies 文件为空：{cookie_path}")
        return None

    temp_dir.mkdir(parents=True, exist_ok=True)
    normalized_out = temp_dir / "normalized_cookies.txt"

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
                        exp_int = max(0, int(float(exp_raw)))
                    except (ValueError, TypeError):
                        exp_int = 0
                    lines.append(f"{domain}\t{include_sub}\t{path_str}\t{secure_str}\t{exp_int}\t{name}\t{value}")
                    valid_count += 1

                if valid_count > 0:
                    normalized_out.write_text("\n".join(lines) + "\n", encoding="utf-8")
                    if on_log:
                        on_log(f"[Cookies] 已自动将 JSON 格式 Cookies 转换为 Netscape 格式（共 {valid_count} 条记录）")
                    return normalized_out
        except Exception as exc:
            if on_log:
                on_log(f"[Cookies 警告] 解析 JSON 格式 Cookies 失败：{exc}")
            return None

    # Case 2: Netscape tab-separated format
    raw_lines = raw_text.splitlines()
    cookie_rows = [
        ln for ln in raw_lines
        if ln.strip() and (not ln.lstrip().startswith("#") or ln.lstrip().startswith("#HttpOnly_"))
        and len(ln.split("\t")) >= 7
    ]
    if cookie_rows:
        has_magic_header = any(
            ln.startswith("# Netscape HTTP Cookie File") or ln.startswith("# HTTP Cookie File")
            for ln in raw_lines[:3]
        )
        if not has_magic_header:
            normalized_out.write_text("# Netscape HTTP Cookie File\n" + raw_text + "\n", encoding="utf-8")
            if on_log:
                on_log(f"[Cookies] 已加载并补全 Netscape 头部：{cookie_path.name}（共 {len(cookie_rows)} 条记录）")
            return normalized_out

        if on_log:
            on_log(f"[Cookies] 已加载 Cookies 文件：{cookie_path.name}（共 {len(cookie_rows)} 条记录）")
        return cookie_path

    if on_log:
        on_log(
            f"[Cookies 警告] 文件 {cookie_path.name} 不是有效的 Netscape 7 列制表符格式或 JSON 格式，建议使用 Get cookies.txt LOCALLY 插件重新导出。"
        )
    return cookie_path


def _build_common_ydl_opts(
    opts: DownloadOptions,
    on_log: Optional[LogCallback] = None,
    paths: AppPaths = PATHS,
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

    if opts.proxy.strip():
        ydl_opts["proxy"] = opts.proxy.strip()

    loaded_cookiefile = False
    if opts.cookies_file.strip():
        cookie_path = Path(opts.cookies_file.strip()).expanduser()
        prepared = prepare_cookie_file(cookie_path, paths.temp_dir, on_log=on_log)
        if prepared is not None and prepared.exists():
            ydl_opts["cookiefile"] = str(prepared)
            loaded_cookiefile = True

    # Only use `cookiesfrombrowser` when a valid `cookiefile` is not already active,
    # preventing running Edge/Chrome SQLite exclusive locks (#7271) from aborting downloads when cookies.txt is provided.
    if opts.cookies_from_browser.strip() and not loaded_cookiefile:
        browser_name = opts.cookies_from_browser.strip().lower()
        ydl_opts["cookiesfrombrowser"] = (browser_name,)

    return ydl_opts


def _is_tiktok_url(url: str) -> bool:
    u = url.lower()
    return "tiktok.com" in u


def _run_ydl_extract_with_fallbacks(
    yt_dlp: Any,
    ydl_opts: Dict[str, Any],
    url: str,
    download: bool,
    on_log: Optional[LogCallback] = None,
) -> Tuple[Any, Optional[Any]]:
    """Run `ydl.extract_info(url, download=download)` with automatic fallbacks for:
    1. Windows Edge/Chrome SQLite cookie database exclusive lock (`Could not copy Chrome cookie database`)
    2. TikTok anti-bot webpage challenge (`Unexpected response from webpage request`)
    Returns `(info, ydl_instance)`.
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
            or "unable to extract webpage video data" in err_lower
            or "impersonate" in err_lower
        ):
            fallback_uas = [
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
                "abc",  # yt-dlp#15629 maintainer workaround to skip TikTok JS challenge page
            ]
            for idx, ua in enumerate(fallback_uas, start=1):
                if on_log:
                    on_log(f"[TikTok 兼容重试 {idx}/{len(fallback_uas)}] 检测到网页反爬响应，尝试备用请求头策略...")
                retry_opts = copy.copy(current_opts)
                headers = dict(retry_opts.get("http_headers") or {})
                headers["User-Agent"] = ua
                retry_opts["http_headers"] = headers
                try:
                    with yt_dlp.YoutubeDL(retry_opts) as ydl:
                        return ydl.extract_info(url, download=download), ydl
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

    if "unexpected response from webpage request" in lower or "impersonate target is available" in lower:
        return DownloadError(
            "目标站点（如 TikTok / Instagram）触发了 TLS 指纹或网页反爬拦截。"
            "请确认已使用包含 curl_cffi 指纹伪装组件的最新构建版本（v1.2.2+），并在设置中配置可用代理或导入 Netscape 格式 cookies.txt。",
            detail=text,
        )

    if any(pat in lower for pat in _AGE_OR_AUTH_PATTERNS):
        return DownloadError(
            "该视频需要登录或年龄验证，请在「偏好设置 -> 网络与认证」中导入有效的 cookies.txt 文件（或先关闭浏览器再启用浏览器读取）。",
            detail=text,
        )

    if "timed out" in lower or "proxy" in lower or "connection" in lower or "ssl" in lower:
        return DownloadError(
            "网络连接超时或代理错误，请检查网络与代理设置后重试。",
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
    ydl_opts = _build_common_ydl_opts(opts, on_log=on_log, paths=paths)
    ydl_opts["extract_flat"] = "in_playlist" if opts.allow_playlist else False

    try:
        info, _ = _run_ydl_extract_with_fallbacks(
            yt_dlp=yt_dlp,
            ydl_opts=ydl_opts,
            url=url,
            download=False,
            on_log=on_log,
        )
    except CancelledError:
        raise
    except Exception as exc:
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

    yt_dlp = _import_yt_dlp(paths)
    workdir.mkdir(parents=True, exist_ok=True)

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

    ydl_opts = _build_common_ydl_opts(opts, on_log=on_log, paths=paths)
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
            on_log=on_log,
        )
        if not downloaded_files and info and ydl is not None:
            prepared = Path(ydl.prepare_filename(info)).resolve()
            if prepared.exists():
                downloaded_files.append(prepared)
    except CancelledError:
        raise
    except Exception as exc:
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

    if on_log:
        on_log(f"正在从镜像源 ({pypi_mirror}) 查询 yt-dlp 最新版本...")

    try:
        wheel_info = http_client.resolve_pypi_wheel(
            "yt-dlp",
            mirror=pypi_mirror,
            proxy=proxy,
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
                proxy=proxy,
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
