"""Built-in bridge to `rookie-cookies` (https://github.com/teng-lin/rookie-cookies) with concurrent zero-latency Chromium locked-DB snapshot, v20 browser_id decryption, and persistent validated Cookie caching."""

from __future__ import annotations

import base64
import contextlib
import ctypes
import ctypes.wintypes
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from app.core.cancel import CancelToken
from app.core.errors import CancelledError, DownloadError
from app.core.site_cookies import (
    SITES,
    cookie_domains_for,
    inspect_netscape,
    resolve_site,
    rows_have_site_auth,
)

LogCallback = Callable[[str], None]

APP_BOUND_POLICIES: Tuple[Tuple[str, str], ...] = (
    ("injection_only", "无特权静默解密 v20 (injection_only，默认推荐：单实例抑制无窗口)"),
    ("smart", "智能缓存优先 (smart：优先复用缓存，缺失时挂起态无窗口解密 v20 并持久化)"),
    ("allow_elevated_fallback", "进程内提权解密 v20 (allow_elevated_fallback：优先同权限，支持进程内 DPAPI/CNG 兜底)"),
    ("disabled", "仅明文库 / DPAPI v10 / 本地缓存 (disabled：跳过 v20 实时解密)"),
)

DEFAULT_BROWSER_CATALOG: Tuple[Tuple[str, str], ...] = (
    ("firefox", "Firefox（推荐：明文库 + 会话恢复，无需启动浏览器进程）"),
    ("edge", "Microsoft Edge（内置 rookie-cookies + 并发锁库快照解锁 + 智能缓存）"),
    ("chrome", "Google Chrome（内置 rookie-cookies + 并发锁库快照解锁 + 智能缓存）"),
    ("brave", "Brave（内置 rookie-cookies + 并发锁库快照解锁 + 智能缓存）"),
    ("chromium", "Chromium"),
    ("arc", "Arc"),
    ("vivaldi", "Vivaldi"),
    ("opera", "Opera"),
    ("opera_gx", "Opera GX"),
    ("librewolf", "LibreWolf"),
    ("zen", "Zen Browser"),
    ("qq", "QQ 浏览器"),
    ("360", "360 安全浏览器"),
    ("360x", "360 极速浏览器 X"),
    ("sogou", "搜狗高速浏览器"),
    ("coccoc", "Cốc Cốc"),
    ("yandex", "Yandex Browser"),
)

_GECKO_BROWSERS = {"firefox", "librewolf", "zen", "cachy"}
_CHROMIUM_BROWSERS = {
    "edge",
    "chrome",
    "brave",
    "chromium",
    "vivaldi",
    "opera",
    "opera_gx",
    "qq",
    "360",
    "360x",
    "sogou",
    "coccoc",
    "yandex",
    "arc",
}
_APP_BOUND_CHROMIUM_BROWSERS = {"edge", "chrome", "brave", "coccoc", "avast"}


@dataclass
class BrowserProfileInfo:
    profile_id: str
    display_name: str
    is_default: bool = False


def _import_rookie() -> Optional[Any]:
    try:
        import rookie_cookies  # type: ignore
        return rookie_cookies
    except ImportError:
        return None


def is_available() -> bool:
    """Return True if the `rookie_cookies` native package is importable."""
    return _import_rookie() is not None


def get_version() -> str:
    """Return the installed `rookie_cookies` version string, or `'未安装'`."""
    rc = _import_rookie()
    if rc is None:
        return "未安装"
    try:
        ver_fn = getattr(rc, "version", None)
        if callable(ver_fn):
            return str(ver_fn())
        return str(getattr(rc, "__version__", "已安装"))
    except Exception:
        return "已安装"


def list_supported_browsers() -> List[Dict[str, str]]:
    """Return supported browsers on the current OS, combining `rookie_cookies.supported_browsers()` with friendly labels."""
    rc = _import_rookie()
    default_labels = dict(DEFAULT_BROWSER_CATALOG)
    if rc is not None:
        supp_fn = getattr(rc, "supported_browsers", None)
        if callable(supp_fn):
            try:
                raw_list = supp_fn()
                results: List[Dict[str, str]] = []
                by_id: Dict[str, Dict[str, Any]] = {}
                for item in raw_list or []:
                    if isinstance(item, dict) and item.get("id"):
                        by_id[str(item["id"]).lower()] = item
                priority_order = [b_id for b_id, _ in DEFAULT_BROWSER_CATALOG if b_id in by_id]
                remaining_order = [b_id for b_id in by_id if b_id not in set(priority_order)]
                for b_id in priority_order + remaining_order:
                    item = by_id[b_id]
                    disp = default_labels.get(b_id) or str(item.get("display_name") or b_id)
                    results.append(
                        {
                            "id": b_id,
                            "display_name": disp,
                            "engine": str(item.get("engine") or ""),
                        }
                    )
                if results:
                    return results
            except Exception:
                pass
    return [{"id": b_id, "display_name": label, "engine": ""} for b_id, label in DEFAULT_BROWSER_CATALOG]


def list_browser_profiles(browser_id: str, timeout: float = 5.0) -> List[BrowserProfileInfo]:
    """Discover installed profiles for `browser_id` via `rookie_cookies.profiles(browser_id)`."""
    b_id = (browser_id or "").strip().lower()
    if not b_id:
        return []
    rc = _import_rookie()
    if rc is None:
        return []

    prof_fn = getattr(rc, "profiles", None) or getattr(rc, "browser_profiles", None)
    if not callable(prof_fn):
        return []

    try:
        try:
            raw_profiles = prof_fn(b_id, timeout=int(timeout))
        except TypeError:
            raw_profiles = prof_fn(b_id)
    except Exception:
        return []

    items: List[BrowserProfileInfo] = []
    for entry in raw_profiles or []:
        if not isinstance(entry, dict):
            continue
        prof_obj = entry.get("profile") if isinstance(entry.get("profile"), dict) else entry
        pid = str(prof_obj.get("profile_id") or prof_obj.get("id") or prof_obj.get("name") or "").strip()
        disp = str(prof_obj.get("display_name") or prof_obj.get("name") or pid).strip()
        is_def = bool(prof_obj.get("is_default") or entry.get("is_default"))
        if pid:
            items.append(BrowserProfileInfo(profile_id=pid, display_name=disp or pid, is_default=is_def))
    return items


def extraction_domains_for(target_url: str) -> Optional[List[str]]:
    """Determine domain filter list for `rookie_cookies.extract(..., domains=...)`."""
    if not (target_url or "").strip():
        all_doms: List[str] = ["youtube.com", "google.com"]
        for s in SITES:
            for d in s.cookie_domains:
                bare = d.lstrip(".").lower()
                if bare and bare not in all_doms:
                    all_doms.append(bare)
        return all_doms

    site = resolve_site(target_url)
    if site and site.key == "youtube":
        return ["youtube.com", "google.com"]

    bound = cookie_domains_for(target_url)
    bare_list: List[str] = []
    for d in bound:
        bare = d.lstrip(".").lower()
        if bare and bare not in bare_list:
            bare_list.append(bare)
    return bare_list or None


def _records_to_netscape_fallback(records: List[Dict[str, Any]]) -> str:
    """Fallback serializer converting 8-field `rookie_cookies` dicts to Netscape format."""
    future_exp = int(time.time()) + 86400 * 30
    lines = ["# Netscape HTTP Cookie File", "# Extracted by Moonlight Whisper via rookie-cookies", ""]
    for c in records:
        if not isinstance(c, dict):
            continue
        dom = str(c.get("domain") or "").strip()
        name = str(c.get("name") or "").strip()
        val = str(c.get("value") or "")
        if not dom or not name:
            continue
        if not dom.startswith(".") and dom.count(".") >= 1 and not dom.startswith("localhost"):
            dom = "." + dom
        inc_sub = "TRUE" if dom.startswith(".") else "FALSE"
        path_str = str(c.get("path") or "/").strip() or "/"
        sec_str = "TRUE" if c.get("secure") else "FALSE"
        exp_raw = c.get("expires")
        try:
            exp_i = int(float(exp_raw)) if exp_raw is not None else 0
            if exp_i <= 0:
                exp_i = future_exp
        except (TypeError, ValueError):
            exp_i = future_exp
        prefix = "#HttpOnly_" if (c.get("http_only") or c.get("httpOnly")) else ""
        safe_val = val.replace("\t", "%09").replace("\r", "%0D").replace("\n", "%0A")
        lines.append(f"{prefix}{dom}\t{inc_sub}\t{path_str}\t{sec_str}\t{exp_i}\t{name}\t{safe_val}")
    return "\n".join(lines) + "\n"


def _is_token_cancelled(token: Optional[CancelToken]) -> bool:
    """Safely check whether `token` is cancelled regardless of whether `is_cancelled` is a bool property or method."""
    if token is None:
        return False
    attr = getattr(token, "is_cancelled", False)
    return bool(attr() if callable(attr) else attr)


@contextlib.contextmanager
def _suppress_chromium_singleton(user_data_dir: Optional[Path]):
    """Hardened Windows context manager that prevents spawned Chromium child processes in rookie-cookies injector.rs
    from discovering running Chrome/Edge ProcessSingleton windows or forwarding WM_COPYDATA start commands.
    This completely eliminates popup windows and opening tabs in the user's active browser!
    """
    if sys.platform != "win32" or user_data_dir is None:
        yield
        return

    mutex_handles: List[Any] = []
    renamed_windows: List[Tuple[int, str]] = []

    try:
        user32 = getattr(ctypes, "windll", None) and getattr(ctypes.windll, "user32", None)
        kernel32 = getattr(ctypes, "windll", None) and getattr(ctypes.windll, "kernel32", None)

        if user32 and kernel32:
            # Ensure 64-bit ctypes argtypes and restype are defined to avoid argument truncation crashes
            WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.wintypes.BOOL, ctypes.wintypes.HWND, ctypes.wintypes.LPARAM)
            user32.EnumWindows.argtypes = [WNDENUMPROC, ctypes.wintypes.LPARAM]
            user32.EnumWindows.restype = ctypes.wintypes.BOOL
            user32.GetWindowTextLengthW.argtypes = [ctypes.wintypes.HWND]
            user32.GetWindowTextLengthW.restype = ctypes.c_int
            user32.GetWindowTextW.argtypes = [ctypes.wintypes.HWND, ctypes.wintypes.LPWSTR, ctypes.c_int]
            user32.GetWindowTextW.restype = ctypes.c_int
            user32.SetWindowTextW.argtypes = [ctypes.wintypes.HWND, ctypes.wintypes.LPCWSTR]
            user32.SetWindowTextW.restype = ctypes.wintypes.BOOL
            user32.FindWindowExW.argtypes = [ctypes.wintypes.HWND, ctypes.wintypes.HWND, ctypes.wintypes.LPCWSTR, ctypes.wintypes.LPCWSTR]
            user32.FindWindowExW.restype = ctypes.wintypes.HWND

            kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.wintypes.BOOL, ctypes.wintypes.LPCWSTR]
            kernel32.CreateMutexW.restype = ctypes.wintypes.HANDLE
            kernel32.WaitForSingleObject.argtypes = [ctypes.wintypes.HANDLE, ctypes.wintypes.DWORD]
            kernel32.WaitForSingleObject.restype = ctypes.wintypes.DWORD
            kernel32.ReleaseMutex.argtypes = [ctypes.wintypes.HANDLE]
            kernel32.ReleaseMutex.restype = ctypes.wintypes.BOOL
            kernel32.CloseHandle.argtypes = [ctypes.wintypes.HANDLE]
            kernel32.CloseHandle.restype = ctypes.wintypes.BOOL

            # 1. Acquire both Chrome and Edge ProcessSingletonStartup! mutexes
            for m_name in (
                "Local\\ChromeProcessSingletonStartup!",
                "Local\\EdgeProcessSingletonStartup!",
                "Local\\MicrosoftEdgeProcessSingletonStartup!",
            ):
                try:
                    h = kernel32.CreateMutexW(None, True, m_name)
                    if h:
                        kernel32.WaitForSingleObject(h, 50)
                        mutex_handles.append(h)
                except Exception:
                    pass

            # 2. Find any ProcessSingleton message window whose title matches user_data_dir
            u_str = str(user_data_dir).rstrip("\\/").lower()
            target_candidates = {u_str, u_str + "\\"}

            def _check_and_hold_window(hwnd: int) -> None:
                try:
                    length = user32.GetWindowTextLengthW(hwnd)
                    if length > 0:
                        buf = ctypes.create_unicode_buffer(length + 1)
                        user32.GetWindowTextW(hwnd, buf, length + 1)
                        title = buf.value
                        clean_t = title.rstrip("\\/").lower()
                        if clean_t in target_candidates or (clean_t + "\\") in target_candidates:
                            user32.SetWindowTextW(hwnd, title + "__mw_hold")
                            renamed_windows.append((hwnd, title))
                except Exception:
                    pass

            def _enum_proc(hwnd, lparam):
                _check_and_hold_window(hwnd)
                return True

            cb = WNDENUMPROC(_enum_proc)
            try:
                user32.EnumWindows(cb, 0)
            except Exception:
                pass

            # Also check message-only windows under HWND_MESSAGE
            try:
                HWND_MESSAGE = ctypes.c_void_p(-3).value
                for cls_name in (
                    "Chrome_WidgetWin_0",
                    "Chrome_WidgetWin_1",
                    "Edge_WidgetWin_0",
                    "Edge_WidgetWin_1",
                    "Chrome_ProcessMessageWindow",
                    "Edge_ProcessMessageWindow",
                ):
                    child = None
                    for _ in range(30):
                        child = user32.FindWindowExW(HWND_MESSAGE, child, cls_name, None)
                        if not child:
                            break
                        _check_and_hold_window(child)
            except Exception:
                pass
    except Exception:
        pass

    try:
        yield
    finally:
        if sys.platform == "win32":
            try:
                user32 = getattr(ctypes, "windll", None) and getattr(ctypes.windll, "user32", None)
                if user32:
                    for hwnd, original_title in renamed_windows:
                        try:
                            user32.SetWindowTextW(hwnd, original_title)
                        except Exception:
                            pass
            except Exception:
                pass
            for h in mutex_handles:
                try:
                    kernel32 = getattr(ctypes, "windll", None) and getattr(ctypes.windll, "kernel32", None)
                    if kernel32:
                        kernel32.ReleaseMutex(h)
                        kernel32.CloseHandle(h)
                except Exception:
                    pass


def _chromium_user_data_dir(browser_id: str) -> Optional[Path]:
    """Return the Windows `User Data` directory for Chromium-family browsers."""
    local = os.environ.get("LOCALAPPDATA", "")
    roaming = os.environ.get("APPDATA", "")
    if not local:
        return None
    mapping = {
        "edge": Path(local) / "Microsoft" / "Edge" / "User Data",
        "chrome": Path(local) / "Google" / "Chrome" / "User Data",
        "brave": Path(local) / "BraveSoftware" / "Brave-Browser" / "User Data",
        "chromium": Path(local) / "Chromium" / "User Data",
        "vivaldi": Path(local) / "Vivaldi" / "User Data",
        "opera": Path(roaming) / "Opera Software" / "Opera Stable" if roaming else None,
        "opera_gx": Path(roaming) / "Opera Software" / "Opera GX Stable" if roaming else None,
    }
    cand = mapping.get(browser_id.lower())
    return cand if (cand and cand.is_dir()) else None


def _locate_chromium_cookie_db(browser_id: str, profile: Optional[str]) -> Tuple[Optional[Path], Optional[Path]]:
    """Return `(cookie_db_path, local_state_path)` for `browser_id` and `profile`."""
    user_data = _chromium_user_data_dir(browser_id)
    if user_data is None:
        return None, None
    local_state = user_data / "Local State"
    prof_candidates: List[str] = []
    if profile and profile.strip():
        prof_candidates.append(profile.strip())
    prof_candidates.extend(["Default", "Profile 1", "Profile 2", "Profile 3"])
    for p_name in prof_candidates:
        for rel in (Path(p_name) / "Network" / "Cookies", Path(p_name) / "Cookies"):
            db_path = user_data / rel
            if db_path.is_file():
                return db_path, (local_state if local_state.is_file() else None)
    return None, (local_state if local_state.is_file() else None)


def _win32_copy_shared_file(src: Path, dst: Path) -> bool:
    """Attempt to read `src` on Windows using `CreateFileW` with `FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE` (0x07)."""
    if sys.platform != "win32":
        try:
            shutil.copy2(src, dst)
            return True
        except OSError:
            return False

    GENERIC_READ = 0x80000000
    FILE_SHARE_ALL = 0x00000001 | 0x00000002 | 0x00000004
    OPEN_EXISTING = 3
    FILE_ATTRIBUTE_NORMAL = 0x80
    INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    kernel32.CreateFileW.restype = ctypes.wintypes.HANDLE
    handle = kernel32.CreateFileW(
        str(src),
        GENERIC_READ,
        FILE_SHARE_ALL,
        None,
        OPEN_EXISTING,
        FILE_ATTRIBUTE_NORMAL,
        None,
    )
    if handle == INVALID_HANDLE_VALUE or not handle:
        return False
    try:
        chunks: List[bytes] = []
        buf = ctypes.create_string_buffer(65536)
        bytes_read = ctypes.wintypes.DWORD(0)
        while True:
            ok = kernel32.ReadFile(handle, buf, 65536, ctypes.byref(bytes_read), None)
            if not ok or bytes_read.value == 0:
                break
            chunks.append(buf.raw[: bytes_read.value])
        data = b"".join(chunks)
        if not data:
            return False
        dst.write_bytes(data)
        return dst.is_file() and dst.stat().st_size > 0
    except Exception:
        return False
    finally:
        kernel32.CloseHandle(handle)


def _copy_sidecars(src_db: Path, dst_db: Path) -> None:
    for suffix in ("-wal", "-journal"):
        side = src_db.with_name(src_db.name + suffix)
        if side.is_file():
            _win32_copy_shared_file(side, dst_db.with_name(dst_db.name + suffix))


def _copy_locked_chromium_cookies_db(
    browser_id: str,
    profile: Optional[str],
    temp_dir: Path,
    on_log: Optional[LogCallback] = None,
) -> Tuple[Optional[Path], Optional[Path]]:
    """Unlock and copy a running Chromium browser's `Network/Cookies` SQLite file to `temp_dir/cookies/`
    WITHOUT closing any browser window and WITHOUT opening any new browser window.
    """
    src_db, local_state = _locate_chromium_cookie_db(browser_id, profile)
    if src_db is None:
        return None, None

    snap_dir = temp_dir / "cookies" / f"snap_{browser_id}_{uuid.uuid4().hex[:6]}"
    snap_dir.mkdir(parents=True, exist_ok=True)
    dst_db = snap_dir / "Cookies"
    dst_ps = snap_dir / "Cookies_ps"

    # Attempt 1: Try shared-mode copy (`FILE_SHARE_READ | WRITE | DELETE`)
    if _win32_copy_shared_file(src_db, dst_db):
        _copy_sidecars(src_db, dst_db)
        return dst_db, local_state

    if sys.platform != "win32":
        return None, None

    # Attempt 2: Concurrently poll `CreateFileW` every 2ms in Python WHILE PowerShell restarts ONLY the headless
    # `network.mojom.NetworkService` (`--service-sandbox-type=network`) utility subprocess and immediately copies `$src` -> `$dst_ps`.
    exe_map = {
        "edge": "msedge.exe",
        "chrome": "chrome.exe",
        "brave": "brave.exe",
        "vivaldi": "vivaldi.exe",
        "opera": "opera.exe",
        "opera_gx": "opera.exe",
    }
    exe_name = exe_map.get(browser_id.lower())
    if exe_name:
        ps_src = str(src_db).replace("'", "''")
        ps_dst = str(dst_ps).replace("'", "''")
        ps_script = (
            f"$src = '{ps_src}'; $dst = '{ps_dst}'; "
            f"Get-CimInstance Win32_Process -Filter \"Name='{exe_name}'\" | "
            "Where-Object { $_.CommandLine -like '*network.mojom.NetworkService*' -or $_.CommandLine -like '*--service-sandbox-type=network*' } | "
            "ForEach-Object { "
            "  Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue; "
            "  for ($i=0; $i -lt 80; $i++) { "
            "    try { [System.IO.File]::Copy($src, $dst, $true); if ((Get-Item $dst -ErrorAction SilentlyContinue).Length -gt 0) { break } } "
            "    catch { Start-Sleep -Milliseconds 3 } "
            "  } "
            "}"
        )
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        proc: Optional[subprocess.Popen] = None
        try:
            proc = subprocess.Popen(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps_script],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=flags,
            )
            deadline = time.time() + 2.5
            while time.time() < deadline:
                if _win32_copy_shared_file(src_db, dst_db):
                    _copy_sidecars(src_db, dst_db)
                    if on_log:
                        on_log(f"[Cookies] 已通过后台毫秒级快照解锁读取 {browser_id} 的 Cookies 数据库")
                    return dst_db, local_state
                if dst_ps.is_file() and dst_ps.stat().st_size > 0:
                    try:
                        shutil.copy2(dst_ps, dst_db)
                        _copy_sidecars(src_db, dst_db)
                        if on_log:
                            on_log(f"[Cookies] 已通过后台无感快照解锁读取 {browser_id} 的 Cookies 数据库")
                        return dst_db, local_state
                    except OSError:
                        pass
                if proc.poll() is not None:
                    if dst_ps.is_file() and dst_ps.stat().st_size > 0:
                        shutil.copy2(dst_ps, dst_db)
                        _copy_sidecars(src_db, dst_db)
                        return dst_db, local_state
                    break
                time.sleep(0.002)
        except Exception:
            pass
        finally:
            if proc is not None and proc.poll() is None:
                try:
                    proc.terminate()
                except Exception:
                    pass

    # Attempt 3: Try Windows `esentutl.exe /y` file copy utility
    try:
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        subprocess.run(
            ["esentutl.exe", "/y", str(src_db), "/d", str(dst_db), "/o"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
            creationflags=flags,
            check=False,
        )
        if dst_db.is_file() and dst_db.stat().st_size > 0:
            _copy_sidecars(src_db, dst_db)
            return dst_db, local_state
    except Exception:
        pass

    return None, None


def _dpapi_unprotect(encrypted_bytes: bytes) -> Optional[bytes]:
    """Decrypt bytes using Windows `CryptUnprotectData` (current user DPAPI)."""
    if sys.platform != "win32" or not encrypted_bytes:
        return None

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", ctypes.wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    crypt32 = ctypes.windll.crypt32  # type: ignore[attr-defined]
    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    in_buf = ctypes.create_string_buffer(encrypted_bytes, len(encrypted_bytes))
    in_blob = DATA_BLOB(len(encrypted_bytes), ctypes.cast(in_buf, ctypes.POINTER(ctypes.c_char)))
    out_blob = DATA_BLOB()
    if not crypt32.CryptUnprotectData(ctypes.byref(in_blob), None, None, None, None, 0, ctypes.byref(out_blob)):
        return None
    try:
        raw = ctypes.string_at(out_blob.pbData, out_blob.cbData)
        return raw
    finally:
        kernel32.LocalFree(out_blob.pbData)


def _decrypt_aes_gcm_cookie_value(enc_bytes: bytes, key: bytes) -> Optional[str]:
    """Decrypt a Chromium `v10`/`v11`/`v20` AES-256-GCM cookie payload, stripping the 32-byte SHA-256 domain digest on Chromium 130+."""
    if len(enc_bytes) < 3 + 12 + 16 or len(key) != 32:
        return None
    try:
        from Cryptodome.Cipher import AES  # type: ignore
    except ImportError:
        return None
    try:
        nonce = enc_bytes[3:15]
        ciphertext = enc_bytes[15:-16]
        tag = enc_bytes[-16:]
        cipher = AES.new(key, AES.MODE_GCM, nonce=nonce)
        plain = cipher.decrypt_and_verify(ciphertext, tag)
        if len(plain) > 32:
            prefix32 = plain[:32]
            has_binary_prefix = False
            try:
                prefix_str = prefix32.decode("utf-8")
                if any(ord(ch) < 0x20 and ch not in ("\t", "\n", "\r") for ch in prefix_str):
                    has_binary_prefix = True
            except UnicodeDecodeError:
                has_binary_prefix = True
            if has_binary_prefix:
                return plain[32:].decode("utf-8", errors="replace")
        return plain.decode("utf-8", errors="replace")
    except Exception:
        return None


_V20_KEY_CACHE: Dict[str, bytes] = {}


def _decrypt_chromium_v20_master_key(local_state_path: Optional[Path], browser_id: str = "edge") -> Optional[bytes]:
    """In-process decryption of Chromium v20 master key from Local State without spawning any browser process.
    On Windows:
    1. Read `Local State` JSON -> `os_crypt.app_bound_encrypted_key` (base64 string).
    2. Strip 4-byte `APPB` prefix -> `encrypted_key_bytes`.
    3. Call Windows `CryptUnprotectData` (User DPAPI).
       In Microsoft Edge, the decrypted payload's trailing 32 bytes is directly the AES-256 master key!
    """
    if sys.platform != "win32" or not local_state_path or not local_state_path.is_file():
        return None

    cache_key = f"{browser_id.lower()}:{local_state_path.resolve()}"
    if cache_key in _V20_KEY_CACHE:
        return _V20_KEY_CACHE[cache_key]

    try:
        ls_text = local_state_path.read_text(encoding="utf-8", errors="replace")
        ls_json = json.loads(ls_text)
        app_bound_b64 = (ls_json.get("os_crypt") or {}).get("app_bound_encrypted_key") or ""
        if not app_bound_b64:
            return None
        raw_appb = base64.b64decode(app_bound_b64)
        if not raw_appb.startswith(b"APPB") or len(raw_appb) <= 4:
            return None
        enc_payload = raw_appb[4:]

        unprotected = _dpapi_unprotect(enc_payload)
        if unprotected and len(unprotected) >= 32:
            candidate_key = unprotected[-32:]
            _V20_KEY_CACHE[cache_key] = candidate_key
            return candidate_key
    except Exception:
        pass

    return None


def _decrypt_chromium_sqlite_cookies(
    db_path: Path,
    local_state_path: Optional[Path],
    domains: Optional[List[str]],
    v20_key: Optional[bytes] = None,
    browser_id: str = "edge",
) -> Tuple[List[Dict[str, Any]], int]:
    """Read an unlocked Chromium `Cookies` SQLite file and decrypt plaintext + `v10` DPAPI (+ `v20` if `v20_key` provided) cookies.
    Returns `(decrypted_rows, v20_skipped_count)`.
    """
    v10_key: Optional[bytes] = None
    if local_state_path and local_state_path.is_file():
        try:
            ls_data = json.loads(local_state_path.read_text(encoding="utf-8", errors="replace"))
            enc_key_b64 = (ls_data.get("os_crypt") or {}).get("encrypted_key") or ""
            if enc_key_b64:
                raw_key = base64.b64decode(enc_key_b64)
                if raw_key.startswith(b"DPAPI"):
                    v10_key = _dpapi_unprotect(raw_key[5:])
        except Exception:
            v10_key = None
        if v20_key is None:
            v20_key = _decrypt_chromium_v20_master_key(local_state_path, browser_id=browser_id)

    rows_out: List[Dict[str, Any]] = []
    v20_count = 0
    conn: Optional[sqlite3.Connection] = None
    try:
        conn = sqlite3.connect(str(db_path))
        cur = conn.cursor()
        cur.execute(
            "SELECT host_key, name, value, encrypted_value, path, is_secure, is_httponly, expires_utc FROM cookies"
        )
        for host_key, name, val, enc_val, path_s, is_sec, is_ho, exp_utc in cur.fetchall():
            dom = str(host_key or "").strip()
            bare = dom.lstrip(".").lower()
            if domains and not any(bare == d or bare.endswith("." + d) for d in domains):
                continue
            cookie_val = str(val or "")
            if not cookie_val and enc_val:
                enc_bytes = bytes(enc_val)
                if enc_bytes.startswith(b"v20"):
                    if v20_key:
                        dec = _decrypt_aes_gcm_cookie_value(enc_bytes, v20_key)
                        if dec is not None:
                            cookie_val = dec
                        else:
                            v20_count += 1
                            continue
                    else:
                        v20_count += 1
                        continue
                elif enc_bytes.startswith((b"v10", b"v11")) and v10_key:
                    dec = _decrypt_aes_gcm_cookie_value(enc_bytes, v10_key)
                    if dec is not None:
                        cookie_val = dec
                    else:
                        continue
                else:
                    unprot = _dpapi_unprotect(enc_bytes)
                    if unprot:
                        cookie_val = unprot.decode("utf-8", errors="replace")
            if not cookie_val:
                continue
            exp_unix = 0
            try:
                exp_int = int(exp_utc or 0)
                if exp_int > 11644473600000000:
                    exp_unix = int((exp_int / 1_000_000) - 11644473600)
            except (TypeError, ValueError):
                exp_unix = 0
            rows_out.append(
                {
                    "domain": dom,
                    "name": str(name or ""),
                    "value": cookie_val,
                    "path": str(path_s or "/"),
                    "secure": bool(is_sec),
                    "http_only": bool(is_ho),
                    "expires": exp_unix,
                }
            )
    except Exception:
        pass
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
    return rows_out, v20_count


def translate_rookie_error(exc: BaseException, browser_id: str = "") -> str:
    """Translate a `rookie_cookies.RookieError` (or subclass) into an actionable Chinese diagnostic message."""
    kind = str(getattr(exc, "kind", "") or "")
    code = str(getattr(exc, "code", "") or "")
    stop_reason = str(getattr(exc, "stop_reason", "") or "")
    text = str(exc)
    lower = text.lower()
    b_label = browser_id or "所选浏览器"

    if kind == "stopped" or stop_reason:
        if stop_reason == "cancelled":
            return "浏览器 Cookie 提取已取消。"
        if stop_reason == "timed_out":
            return f"从 {b_label} 提取 Cookie 超时（可能正等待系统密钥环或浏览器响应），请重试或改用「登录并获取 Cookies…」。"
        return f"从 {b_label} 提取 Cookie 中止 ({stop_reason or code})：{text}"

    if "os error 32" in lower or "share-locked" in lower or "being used by another process" in lower or (
        "locked" in lower and ("database" in lower or "sqlite" in lower or "cookie" in lower)
    ):
        return (
            f"{b_label} 正在运行并锁定了 Cookies 数据库（Windows OS error 32）。"
            f"请打开「偏好设置 -> 网络与认证」点击「登录并获取 Cookies…」导出一次后永久免弹窗使用，或先关闭 {b_label} 后再试。"
        )

    if kind in ("request", "source") or "conflicting_profile" in code or "profile" in code:
        return (
            f"浏览器或配置文件参数无效（{code or 'request_error'}）：请打开「偏好设置 -> 网络与认证」"
            f"刷新或清空「浏览器配置」名称后重试。详情：{text}"
        )

    if (
        "app_bound" in code
        or "appbound" in lower
        or "ielevator" in lower
        or "injection" in lower
        or "decrypt" in code
        or "decrypt" in lower
    ):
        return (
            f"无法从 {b_label} 解密 App-Bound (v20) Cookies（{code or 'decrypt_failed'}）："
            "请打开「偏好设置 -> 网络与认证」点击「登录并获取 Cookies…」一键导出，或改用 Firefox。"
        )

    return f"通过 rookie-cookies 从 {b_label} 读取 Cookies 失败（{code or kind or 'error'}）：{text}"


def _build_extract_kwargs(
    b_id: str,
    prof: Optional[str],
    domains: Optional[List[str]],
    policy: str,
    timeout: float,
    cancel_handle: Any = None,
) -> Dict[str, Any]:
    kwargs: Dict[str, Any] = {
        "browser": b_id,
        "app_bound": policy,
        "timeout": max(1, int(timeout)),
    }
    if prof:
        kwargs["profile"] = prof
    if domains is not None:
        kwargs["domains"] = domains
    if b_id in _GECKO_BROWSERS:
        kwargs["include_session"] = True
    if cancel_handle is not None:
        kwargs["cancellation"] = cancel_handle
    return kwargs


def _persistent_cookie_cache_candidates(base_temp: Path, b_id: str, target_url: str) -> List[Path]:
    """Return persistent cookie cache file paths under `data_root/cookies/` (or `base_temp/cookies_cache` when isolated)."""
    resolved = base_temp.resolve()
    data_cookies_dir = (resolved.parent / "cookies") if resolved.name == "temp" else (resolved / "cookies_cache")
    site = resolve_site(target_url)
    site_key = site.key if site is not None else "general"
    return [
        data_cookies_dir / f"rookie_cache_{b_id}_{site_key}.txt",
        data_cookies_dir / f"{site_key}.txt",
    ]


def _save_to_persistent_cookie_cache(
    netscape_content: str,
    base_temp: Path,
    b_id: str,
    target_url: str,
    on_log: Optional[LogCallback] = None,
) -> None:
    """Save a validated Netscape cookie file into `data_root/cookies/` so subsequent downloads never need to touch the browser."""
    try:
        candidates = _persistent_cookie_cache_candidates(base_temp, b_id, target_url)
        for cache_path in candidates:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(netscape_content, encoding="utf-8")
            try:
                os.chmod(cache_path, 0o600)
            except OSError:
                pass
        if on_log and candidates:
            on_log(f"[Cookies 缓存] 已将提取成功的登录凭据持久化缓存至：{candidates[0].name}（后续下载将直接秒级复用，零弹窗）")
    except Exception:
        pass


# rookie-cookies 在 Windows 上不支持 extract_from_path(browser_id=...)，会抛出
# "Chromium path option 'browser_id' is only supported on Linux/macOS"。
# 探测到一次后记住，之后不再白白重试，直接走 Windows 支持的实时提取路径。
_PATH_BROWSER_ID_UNSUPPORTED = False


def _is_browser_id_platform_error(exc: BaseException) -> bool:
    low = str(exc).lower()
    return "browser_id" in low and "only supported on" in low


def _call_extract_from_path(
    rc: Any,
    snap_db: Path,
    b_id: str,
    local_state: Optional[Path],
    domains: Optional[List[str]],
    app_bound_policy: str,
    on_log: Optional[LogCallback] = None,
    live_extract: Optional[Callable[[], List[Dict[str, Any]]]] = None,
) -> List[Dict[str, Any]]:
    """Call `rookie_cookies.extract_from_path` with platform-aware fallbacks.

    - Linux/macOS: `browser_id=` enables v20 decryption on the snapshot DB.
    - Windows: `browser_id=` is rejected by rookie-cookies, so we remember that and fall back to
      `live_extract` (rookie's own `extract(browser=...)`, which is the Windows-supported v20 path),
      then to the legacy `local_state_path` (v10 only) form.
    """
    global _PATH_BROWSER_ID_UNSUPPORTED
    extract_path_fn = getattr(rc, "extract_from_path", None)

    if callable(extract_path_fn) and not _PATH_BROWSER_ID_UNSUPPORTED:
        try:
            res = list(
                extract_path_fn(
                    str(snap_db), domains=domains, browser_id=b_id, app_bound=app_bound_policy
                )
                or []
            )
            if res:
                return res
        except TypeError:
            try:
                res = list(extract_path_fn(str(snap_db), domains=domains, browser_id=b_id) or [])
                if res:
                    return res
            except Exception as exc2:
                if _is_browser_id_platform_error(exc2):
                    _PATH_BROWSER_ID_UNSUPPORTED = True
                elif on_log:
                    on_log(f"[rookie-cookies 调试] extract_from_path(browser_id={b_id}) 回退重试异常: {exc2}")
        except Exception as exc1:
            if _is_browser_id_platform_error(exc1):
                _PATH_BROWSER_ID_UNSUPPORTED = True
                if on_log:
                    on_log("[rookie-cookies] 当前平台不支持 extract_from_path(browser_id)，改用实时提取路径。")
            elif on_log:
                on_log(f"[rookie-cookies 调试] extract_from_path(browser_id={b_id}, app_bound={app_bound_policy}) 异常: {exc1}")

    # Windows-supported route: let rookie read the live browser profile (handles v20 itself).
    if live_extract is not None:
        try:
            res = list(live_extract() or [])
            if res:
                return res
        except Exception as exc3:
            if on_log:
                on_log(f"[rookie-cookies 调试] 实时提取 {b_id} 失败：{exc3}")

    if callable(extract_path_fn) and local_state is not None and local_state.is_file():
        try:
            res = list(
                extract_path_fn(
                    str(snap_db),
                    domains=domains,
                    local_state_path=str(local_state),
                    app_bound=app_bound_policy,
                )
                or []
            )
            if res:
                return res
        except Exception:
            pass

    return []


def extract_to_netscape_file(
    browser: str,
    profile: str = "",
    target_url: str = "",
    temp_dir: Optional[Path] = None,
    app_bound: str = "disabled",
    timeout: float = 15.0,
    token: Optional[CancelToken] = None,
    on_log: Optional[LogCallback] = None,
    force_refresh: bool = False,
) -> Optional[Path]:
    """Extract cookies for `target_url` from `browser`:
    - Respects `app_bound="disabled"` strictly (never silently upgrades `disabled` to `allow_elevated_fallback` or `injection_only`);
    - Supports `app_bound="smart"` for opt-in one-time v20 decryption with persistent caching;
    - Recovers locked Chromium databases (`OS error 32`) via `_copy_locked_chromium_cookies_db`.
    """
    if token:
        token.check()

    b_id = (browser or "").strip().lower()
    if not b_id:
        return None

    base_temp = temp_dir or Path.cwd() / "temp"
    cookie_dir = base_temp / "cookies"
    cookie_dir.mkdir(parents=True, exist_ok=True)

    # Step 1: Check persistent validated Cookie cache first (when not forcing refresh)
    if not force_refresh and target_url:
        for cache_cand in _persistent_cookie_cache_candidates(base_temp, b_id, target_url):
            if cache_cand.is_file() and cache_cand.stat().st_size > 0:
                rep = inspect_netscape(cache_cand, target_url)
                if rep.ok:
                    out_cached = cookie_dir / f"cookies_session_rookie_{b_id}_{uuid.uuid4().hex[:8]}.txt"
                    shutil.copy2(cache_cand, out_cached)
                    if on_log:
                        on_log(f"[Cookies 缓存] 已直接复用已验证的本地浏览器 Cookies 缓存 ({cache_cand.name})：{rep.summary()}")
                    return out_cached

    rc = _import_rookie()
    if rc is None:
        if on_log:
            on_log("[rookie-cookies] 未检测到内置 rookie_cookies 模块，将回退至 yt-dlp 原生读取。")
        return None

    prof = (profile or "").strip() or None
    policy = (app_bound or "injection_only").strip().lower()
    if policy not in ("disabled", "smart", "injection_only", "allow_elevated_fallback"):
        policy = "injection_only"

    eff_pol = "injection_only" if policy == "smart" else policy

    domains = extraction_domains_for(target_url)

    cancel_handle = None
    stop_watcher = threading.Event()
    watcher_thread: Optional[threading.Thread] = None
    cancel_cls = getattr(rc, "CancellationHandle", None)
    if callable(cancel_cls) and token is not None:
        try:
            cancel_handle = cancel_cls()

            def _watch_cancel() -> None:
                while not stop_watcher.wait(0.1):
                    if _is_token_cancelled(token):
                        try:
                            cancel_handle.cancel()
                        except Exception:
                            pass
                        break

            watcher_thread = threading.Thread(target=_watch_cancel, daemon=True)
            watcher_thread.start()
        except Exception:
            cancel_handle = None

    try:
        rows: List[Dict[str, Any]] = []
        v20_skipped = 0
        extract_fn = getattr(rc, "extract", None)
        read_fn = getattr(rc, "read", None)

        def _live_extract() -> List[Dict[str, Any]]:
            if not callable(extract_fn):
                return []
            return list(extract_fn(**_build_extract_kwargs(b_id, prof, domains, eff_pol, timeout, cancel_handle)) or [])
        snap_db: Optional[Path] = None
        local_state: Optional[Path] = None

        user_data_dir = _chromium_user_data_dir(b_id) if (sys.platform == "win32" and b_id in _CHROMIUM_BROWSERS) else None

        with _suppress_chromium_singleton(user_data_dir):
            if sys.platform == "win32" and b_id in _CHROMIUM_BROWSERS:
                snap_db, local_state = _copy_locked_chromium_cookies_db(b_id, prof, base_temp, on_log=on_log)
                if snap_db is not None and snap_db.is_file():
                    rows, v20_skipped = _decrypt_chromium_sqlite_cookies(snap_db, local_state, domains, browser_id=b_id)
                    if not rows_have_site_auth(rows, target_url) and eff_pol != "disabled":
                        cand_rows = _call_extract_from_path(
                            rc, snap_db, b_id, local_state, domains, eff_pol, on_log=on_log, live_extract=_live_extract
                        )
                        if rows_have_site_auth(cand_rows, target_url) or len(cand_rows) > len(rows):
                            rows = cand_rows
                else:
                    if callable(extract_fn):
                        kwargs = _build_extract_kwargs(b_id, prof, domains, eff_pol, timeout, cancel_handle)
                        try:
                            rows = list(extract_fn(**kwargs) or [])
                        except TypeError:
                            minimal_kw = {"browser": b_id, "app_bound": eff_pol}
                            if prof:
                                minimal_kw["profile"] = prof
                            if domains is not None:
                                minimal_kw["domains"] = domains
                            try:
                                rows = list(extract_fn(**minimal_kw) or [])
                            except TypeError:
                                if eff_pol == "disabled" and b_id in _APP_BOUND_CHROMIUM_BROWSERS:
                                    rows = []
                                else:
                                    rows = list(extract_fn(browser=b_id) or [])
            else:
                try:
                    if callable(extract_fn):
                        kwargs = _build_extract_kwargs(b_id, prof, domains, eff_pol, timeout, cancel_handle)
                        try:
                            rows = list(extract_fn(**kwargs) or [])
                        except TypeError:
                            minimal_kw = {"browser": b_id, "app_bound": eff_pol}
                            if prof:
                                minimal_kw["profile"] = prof
                            if domains is not None:
                                minimal_kw["domains"] = domains
                            try:
                                rows = list(extract_fn(**minimal_kw) or [])
                            except TypeError:
                                if eff_pol == "disabled" and b_id in _APP_BOUND_CHROMIUM_BROWSERS:
                                    rows = []
                                else:
                                    rows = list(extract_fn(browser=b_id) or [])
                    elif callable(read_fn):
                        read_kw = _build_extract_kwargs(b_id, prof, None, eff_pol, timeout, cancel_handle)
                        read_kw.pop("domains", None)
                        snap = read_fn(**read_kw)
                        all_rows = list(snap.as_list() if hasattr(snap, "as_list") else [])
                        if domains:
                            rows = [
                                r
                                for r in all_rows
                                if any(
                                    str(r.get("domain") or "").lstrip(".").lower() == d
                                    or str(r.get("domain") or "").lstrip(".").lower().endswith("." + d)
                                    for d in domains
                                )
                            ]
                        else:
                            rows = all_rows
                    else:
                        if eff_pol == "disabled" and b_id in _APP_BOUND_CHROMIUM_BROWSERS:
                            rows = []
                        else:
                            helper_fn = getattr(rc, b_id, None)
                            if not callable(helper_fn):
                                raise RuntimeError(f"rookie_cookies 不支持浏览器 {b_id}")
                            rows = list(helper_fn(domains=domains) if domains else helper_fn())
                except Exception as primary_exc:
                    err_lower = str(primary_exc).lower()
                    is_locked_err = (
                        "os error 32" in err_lower
                        or "share-locked" in err_lower
                        or "being used by another process" in err_lower
                        or ("locked" in err_lower and ("database" in err_lower or "cookie" in err_lower))
                    )
                    if is_locked_err and b_id in _CHROMIUM_BROWSERS:
                        snap_db, local_state = _copy_locked_chromium_cookies_db(b_id, prof, base_temp, on_log=on_log)
                        if snap_db is not None and snap_db.is_file():
                            rows, v20_skipped = _decrypt_chromium_sqlite_cookies(snap_db, local_state, domains, browser_id=b_id)
                            if not rows_have_site_auth(rows, target_url) and eff_pol != "disabled":
                                cand_rows = _call_extract_from_path(
                            rc, snap_db, b_id, local_state, domains, eff_pol, on_log=on_log, live_extract=_live_extract
                        )
                                if rows_have_site_auth(cand_rows, target_url) or len(cand_rows) > len(rows):
                                    rows = cand_rows
                        else:
                            raise primary_exc
                    else:
                        raise primary_exc

        if not rows:
            if on_log:
                dom_desc = f"（目标域: {', '.join(domains)}）" if domains else ""
                if v20_skipped > 0:
                    on_log(
                        f"[Cookies 提示] {b_id} 中检测到 {v20_skipped} 条 App-Bound (v20) 加密登录凭据未能解密（策略: {policy}）。"
                        "建议打开「偏好设置 -> 网络与认证」点击「登录并获取 Cookies…」一键导出（永久免弹窗复用）或改用 Firefox。"
                    )
                else:
                    on_log(
                        f"[Cookies 警告] rookie-cookies 从 {b_id} (配置: {prof or '默认'}) 未提取到匹配的 Cookie{dom_desc}。"
                        "请确认已在浏览器中登录目标网站。"
                    )
            return None

        to_netscape_fn = getattr(rc, "to_netscape", None)
        if callable(to_netscape_fn):
            try:
                netscape_raw = str(to_netscape_fn(rows))
            except Exception:
                netscape_raw = _records_to_netscape_fallback(rows)
        else:
            netscape_raw = _records_to_netscape_fallback(rows)

        if not netscape_raw.lstrip().startswith("# Netscape HTTP Cookie File"):
            netscape_raw = "# Netscape HTTP Cookie File\n" + netscape_raw

        fixed_lines = ["# Netscape HTTP Cookie File", f"# Extracted from {b_id} via rookie-cookies", ""]
        future_exp = int(time.time()) + 86400 * 30
        valid_count = 0
        for raw_ln in netscape_raw.splitlines():
            s = raw_ln.strip()
            if not s:
                continue
            is_ho = s.startswith("#HttpOnly_")
            if s.startswith("#") and not is_ho:
                continue
            prefix = "#HttpOnly_" if is_ho else ""
            body = s[len("#HttpOnly_") :] if is_ho else s
            cols = body.split("\t")
            if len(cols) < 7:
                continue
            dom = cols[0].strip()
            if not dom.startswith(".") and dom.count(".") >= 1 and not dom.startswith("localhost"):
                dom = "." + dom
            inc_sub = "TRUE" if dom.startswith(".") else "FALSE"
            path_s = cols[2].strip() or "/"
            sec_s = "TRUE" if cols[3].strip().upper() == "TRUE" else "FALSE"
            try:
                exp_i = int(float(cols[4].strip()))
                if exp_i <= 0:
                    exp_i = future_exp
            except (TypeError, ValueError):
                exp_i = future_exp
            name_s = cols[5].strip()
            val_s = "\t".join(cols[6:])
            if not dom or not name_s:
                continue
            fixed_lines.append(f"{prefix}{dom}\t{inc_sub}\t{path_s}\t{sec_s}\t{exp_i}\t{name_s}\t{val_s}")
            valid_count += 1

        if valid_count == 0:
            return None

        final_netscape = "\n".join(fixed_lines) + "\n"
        out_path = cookie_dir / f"cookies_session_rookie_{b_id}_{uuid.uuid4().hex[:8]}.txt"
        out_path.write_text(final_netscape, encoding="utf-8")
        try:
            os.chmod(out_path, 0o600)
        except OSError:
            pass

        if target_url:
            rep = inspect_netscape(out_path, target_url)
            if on_log:
                on_log(
                    f"[rookie-cookies] 已从 {b_id} (配置: {prof or '自动'}, 策略: {policy}) 提取 {valid_count} 条 Cookie | {rep.summary()}"
                )
            if not rep.ok and v20_skipped > 0:
                if on_log:
                    on_log(
                        f"[Cookies 警告] 从 {b_id} 仅读取到匿名 Cookie，关键登录凭据 ({v20_skipped} 条) 受 v20 保护未能解密；"
                        "建议使用设置中的「登录并获取 Cookies…」导出一次。"
                    )
                try:
                    out_path.unlink()
                except OSError:
                    pass
                return None
            if rep.ok:
                _save_to_persistent_cookie_cache(final_netscape, base_temp, b_id, target_url, on_log=on_log)
        elif on_log:
            on_log(
                f"[rookie-cookies] 已从 {b_id} (配置: {prof or '自动'}, 策略: {policy}) 成功提取 {valid_count} 条 Cookie"
            )

        return out_path

    except Exception as exc:
        if _is_token_cancelled(token):
            raise CancelledError("任务已取消") from exc
        if str(getattr(exc, "stop_reason", "")) == "cancelled":
            raise CancelledError("任务已取消") from exc
        msg = translate_rookie_error(exc, browser_id=b_id)
        if on_log:
            on_log(f"[Cookies 警告] {msg}")
        raise DownloadError(msg, detail=str(exc)) from exc
    finally:
        stop_watcher.set()


def diagnose_browser_cookies(
    browser: str,
    profile: str = "",
    target_url: str = "",
    app_bound: str = "disabled",
) -> Tuple[bool, str]:
    """Run an on-demand browser Cookie extraction & static inspection check for the Settings UI."""
    rc = _import_rookie()
    if rc is None:
        return False, "当前环境未安装 rookie-cookies 组件，请运行 pip install rookie-cookies>=0.6.0。"

    logs: List[str] = []
    try:
        out_file = extract_to_netscape_file(
            browser=browser,
            profile=profile,
            target_url=target_url,
            app_bound=app_bound,
            on_log=logs.append,
            force_refresh=True,
        )
    except Exception as exc:
        return False, "\n".join(logs) if logs else str(exc)

    if out_file is None or not out_file.exists():
        return False, "\n".join(logs) or f"未能从 {browser} 提取到任何有效 Cookie。"

    try:
        if target_url:
            rep = inspect_netscape(out_file, target_url)
            return rep.ok, "\n".join(logs + [rep.summary()])
        return True, "\n".join(logs) or "Cookie 提取成功。"
    finally:
        try:
            out_file.unlink()
        except OSError:
            pass
