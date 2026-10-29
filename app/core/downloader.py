"""yt-dlp wrapper for metadata extraction, playlist expansion, audio download, and self-updating."""

from __future__ import annotations

import importlib
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

    if opts.cookies_file.strip():
        cookie_path = Path(opts.cookies_file.strip()).expanduser()
        if cookie_path.exists():
            ydl_opts["cookiefile"] = str(cookie_path)

    if opts.cookies_from_browser.strip():
        browser_name = opts.cookies_from_browser.strip().lower()
        ydl_opts["cookiesfrombrowser"] = (browser_name,)

    return ydl_opts


def _translate_ydl_exception(exc: BaseException) -> DownloadError:
    text = str(exc)
    lower = text.lower()
    if "sign in" in lower or "login" in lower or "cookies" in lower or "age" in lower or "members-only" in lower:
        return DownloadError(
            "该视频需要登录或年龄验证，请在「设置」中导入 Cookies 或选择从浏览器读取 Cookies。",
            detail=text,
        )
    if "timed out" in lower or "proxy" in lower or "connection" in lower or "ssl" in lower:
        return DownloadError(
            "网络连接超时或代理错误，请检查网络与代理设置后重试。",
            detail=text,
        )
    if "unsupported url" in lower or "no suitable InfoExtractor" in text:
        return DownloadError(
            "不支持该链接或站点规则已变更，请检查链接或在设置中点击「更新 yt-dlp」。",
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

    # Handle Bilibili multi-part (?p=N) videos when flat entry shares base URL
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
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
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
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            if not downloaded_files and info:
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
