"""Built-in bridge to `rookie-cookies` (https://github.com/teng-lin/rookie-cookies) with non-disruptive Chromium locked-DB snapshot & pure-Python v10 DPAPI fallback."""

from __future__ import annotations

import base64
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
from app.core.site_cookies import SITES, cookie_domains_for, inspect_netscape, resolve_site

LogCallback = Callable[[str], None]

# Default to "disabled" so downloading NEVER spawns a new Edge/Chrome browser window on Windows!
APP_BOUND_POLICIES: Tuple[Tuple[str, str], ...] = (
    ("disabled", "静默安全读取 (disabled，默认推荐：绝不弹出/启动新浏览器窗口)"),
    ("injection_only", "无特权进程注入解密 v20 (injection_only，注意：Windows 下会拉起新浏览器窗口)"),
    ("allow_elevated_fallback", "允许管理员提权解密 v20 (allow_elevated_fallback，注意：可能拉起浏览器窗口)"),
)

DEFAULT_BROWSER_CATALOG: Tuple[Tuple[str, str], ...] = (
    ("firefox", "Firefox（推荐：明文库 + 会话恢复，无需启动浏览器进程）"),
    ("edge", "Microsoft Edge（内置 rookie-cookies 读取 + 锁库快照解锁）"),
    ("chrome", "Google Chrome（内置 rookie-cookies 读取 + 锁库快照解锁）"),
    ("brave", "Brave（内置 rookie-cookies 读取 + 锁库快照解锁）"),
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
        dst.write_bytes(b"".join(chunks))
        return dst.is_file() and dst.stat().st_size > 0
    except Exception:
        return False
    finally:
        kernel32.CloseHandle(handle)


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

    # Attempt 1: Try shared-mode copy (`FILE_SHARE_READ | WRITE | DELETE`)
    if _win32_copy_shared_file(src_db, dst_db):
        for suffix in ("-wal", "-journal"):
            side = src_db.with_name(src_db.name + suffix)
            if side.is_file():
                _win32_copy_shared_file(side, dst_db.with_name(dst_db.name + suffix))
        return dst_db, local_state

    if sys.platform != "win32":
        return None, None

    # Attempt 2: Briefly restart ONLY the headless `network.mojom.NetworkService` utility subprocess.
    # In Chromium's architecture, NetworkService is a headless background socket worker (`--type=utility`).
    # Terminating ONLY this background worker releases the exclusive file lock on `Network/Cookies` immediately,
    # while all open browser windows & tabs remain completely untouched and zero new windows are created!
    exe_map = {"edge": "msedge.exe", "chrome": "chrome.exe", "brave": "brave.exe"}
    exe_name = exe_map.get(browser_id.lower())
    if exe_name:
        try:
            ps_script = (
                f"Get-CimInstance Win32_Process -Filter \"Name='{exe_name}'\" | "
                "Where-Object { $_.CommandLine -like '*--utility-sub-type=network.mojom.NetworkService*' } | "
                "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
            )
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
            subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps_script],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=4,
                creationflags=flags,
                check=False,
            )
            # Poll rapidly for up to 400ms while the file lock is released
            for _ in range(20):
                if _win32_copy_shared_file(src_db, dst_db):
                    for suffix in ("-wal", "-journal"):
                        side = src_db.with_name(src_db.name + suffix)
                        if side.is_file():
                            _win32_copy_shared_file(side, dst_db.with_name(dst_db.name + suffix))
                    if on_log:
                        on_log(f"[Cookies] 已通过后台无感快照解锁读取 {browser_id} 的 Cookies 数据库")
                    return dst_db, local_state
                time.sleep(0.02)
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


def _decrypt_chromium_sqlite_cookies(
    db_path: Path,
    local_state_path: Optional[Path],
    domains: Optional[List[str]],
) -> Tuple[List[Dict[str, Any]], int]:
    """Read an unlocked Chromium `Cookies` SQLite file and decrypt plaintext + `v10` DPAPI cookies in pure Python.
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
                    v20_count += 1
                    continue
                if enc_bytes.startswith((b"v10", b"v11")) and v10_key:
                    try:
                        from Cryptodome.Cipher import AES  # type: ignore

                        nonce = enc_bytes[3:15]
                        ciphertext = enc_bytes[15:-16]
                        tag = enc_bytes[-16:]
                        cipher = AES.new(v10_key, AES.MODE_GCM, nonce=nonce)
                        plain = cipher.decrypt_and_verify(ciphertext, tag)
                        # Chrome/Edge 130+ prepends a 32-byte SHA-256 domain digest before the UTF-8 cookie value
                        try:
                            cookie_val = plain.decode("utf-8")
                        except UnicodeDecodeError:
                            if len(plain) > 32:
                                cookie_val = plain[32:].decode("utf-8", errors="replace")
                    except Exception:
                        continue
                else:
                    unprot = _dpapi_unprotect(enc_bytes)
                    if unprot:
                        cookie_val = unprot.decode("utf-8", errors="replace")
            if not cookie_val:
                continue
            # Convert Chromium WebKit timestamp (microseconds since 1601-01-01) to Unix epoch seconds
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
            f"{b_label} 正在运行并独占锁定了 Cookies 数据库（Windows OS error 32）。"
            f"请先关闭 {b_label} 后再试，或在设置中点击「登录并获取 Cookies…」导出一次后永久免弹窗使用，也可改用 Firefox。"
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
            "为防止自动弹出新浏览器窗口，默认已禁用进程注入。建议使用设置中的「登录并获取 Cookies…」导出一次或改用 Firefox。"
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


def extract_to_netscape_file(
    browser: str,
    profile: str = "",
    target_url: str = "",
    temp_dir: Optional[Path] = None,
    app_bound: str = "disabled",
    timeout: float = 15.0,
    token: Optional[CancelToken] = None,
    on_log: Optional[LogCallback] = None,
) -> Optional[Path]:
    """Extract cookies for `target_url` from `browser` using `rookie-cookies` (with locked-DB snapshot fallback)
    and write an isolated Netscape cookiefile. Defaults to `app_bound="disabled"` so it NEVER spawns a browser window.
    """
    if token:
        token.check()

    rc = _import_rookie()
    if rc is None:
        if on_log:
            on_log("[rookie-cookies] 未检测到内置 rookie_cookies 模块，将回退至 yt-dlp 原生读取。")
        return None

    b_id = (browser or "").strip().lower()
    if not b_id:
        return None

    prof = (profile or "").strip() or None
    policy = (app_bound or "disabled").strip().lower()
    if policy not in ("injection_only", "allow_elevated_fallback", "disabled"):
        policy = "disabled"

    domains = extraction_domains_for(target_url)
    base_temp = temp_dir or Path.cwd() / "temp"

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

        try:
            if callable(extract_fn):
                kwargs = _build_extract_kwargs(b_id, prof, domains, policy, timeout, cancel_handle)
                try:
                    rows = list(extract_fn(**kwargs) or [])
                except TypeError:
                    minimal_kw: Dict[str, Any] = {"browser": b_id, "app_bound": policy}
                    if prof:
                        minimal_kw["profile"] = prof
                    if domains is not None:
                        minimal_kw["domains"] = domains
                    try:
                        rows = list(extract_fn(**minimal_kw) or [])
                    except TypeError:
                        if policy == "disabled" and b_id in _APP_BOUND_CHROMIUM_BROWSERS:
                            rows = []
                        else:
                            rows = list(extract_fn(browser=b_id) or [])
            elif callable(read_fn):
                read_kw = _build_extract_kwargs(b_id, prof, None, policy, timeout, cancel_handle)
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
                if policy == "disabled" and b_id in _APP_BOUND_CHROMIUM_BROWSERS:
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
            if is_locked_err and b_id in ("edge", "chrome", "brave", "chromium", "vivaldi", "opera", "opera_gx"):
                snap_db, local_state = _copy_locked_chromium_cookies_db(b_id, prof, base_temp, on_log=on_log)
                if snap_db is not None and snap_db.is_file():
                    extract_path_fn = getattr(rc, "extract_from_path", None)
                    if callable(extract_path_fn):
                        try:
                            path_kw: Dict[str, Any] = {"domains": domains, "app_bound": policy}
                            if local_state is not None:
                                path_kw["local_state_path"] = str(local_state)
                            else:
                                path_kw["browser_id"] = b_id
                            rows = list(extract_path_fn(str(snap_db), **path_kw) or [])
                        except Exception:
                            rows = []
                    if not rows:
                        rows, v20_skipped = _decrypt_chromium_sqlite_cookies(snap_db, local_state, domains)
                else:
                    raise primary_exc
            else:
                raise primary_exc

        # If extract() succeeded without throwing OS error 32, but returned 0 rows on Chromium with policy=="disabled",
        # also check if our SQLite v10 reader can recover any v10/plaintext cookies from the database
        if not rows and b_id in _APP_BOUND_CHROMIUM_BROWSERS:
            snap_db, local_state = _copy_locked_chromium_cookies_db(b_id, prof, base_temp, on_log=None)
            if snap_db is not None and snap_db.is_file():
                rows, v20_skipped = _decrypt_chromium_sqlite_cookies(snap_db, local_state, domains)

        if not rows:
            if on_log:
                dom_desc = f"（目标域: {', '.join(domains)}）" if domains else ""
                if (policy == "disabled" and b_id in _APP_BOUND_CHROMIUM_BROWSERS) or v20_skipped > 0:
                    on_log(
                        f"[Cookies 提示] {b_id} 中的 Cookie 采用 App-Bound (v20) 加密；为避免自动弹出新浏览器窗口，"
                        "已跳过进程注入读取。正在自动启用免 Cookie 通道与备用解析接口（若需登录态，可在设置中点击「登录并获取 Cookies…」导出一次或改用 Firefox）。"
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

        cookie_dir = base_temp / "cookies"
        cookie_dir.mkdir(parents=True, exist_ok=True)
        out_path = cookie_dir / f"cookies_session_rookie_{b_id}_{uuid.uuid4().hex[:8]}.txt"
        out_path.write_text("\n".join(fixed_lines) + "\n", encoding="utf-8")
        try:
            os.chmod(out_path, 0o600)
        except OSError:
            pass

        if on_log:
            on_log(
                f"[rookie-cookies] 已从 {b_id} (配置: {prof or '自动'}, 策略: {policy}) 成功提取 {valid_count} 条 Cookie"
            )
            if target_url:
                rep = inspect_netscape(out_path, target_url)
                on_log(f"[Cookies 体检] {rep.summary()}")

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
        return (
            False,
            "当前环境未安装 rookie-cookies 组件（请运行 `pip install rookie-cookies>=0.6.0` 或使用打包版）。",
        )
    import tempfile

    logs: List[str] = []
    with tempfile.TemporaryDirectory(prefix="rookie_diag_") as tmp:
        try:
            out = extract_to_netscape_file(
                browser=browser,
                profile=profile,
                target_url=target_url,
                temp_dir=Path(tmp),
                app_bound=app_bound,
                timeout=15.0,
                on_log=logs.append,
            )
        except DownloadError as exc:
            return False, exc.user_message
        except Exception as exc:
            return False, translate_rookie_error(exc, browser_id=browser)

        if out is None or not out.exists():
            return False, "\n".join(logs) or f"未能从 {browser} 提取到匹配的 Cookie。"

        if target_url.strip():
            rep = inspect_netscape(out, target_url.strip())
            return rep.ok, "\n".join(logs)
        return True, "\n".join(logs)
