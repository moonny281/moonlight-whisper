"""Built-in bridge to `rookie-cookies` (https://github.com/teng-lin/rookie-cookies) for native Rust browser Cookie extraction."""

from __future__ import annotations

import os
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
    ("edge", "Microsoft Edge（内置 rookie-cookies 读取）"),
    ("chrome", "Google Chrome（内置 rookie-cookies 读取）"),
    ("brave", "Brave（内置 rookie-cookies 读取）"),
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
    """Determine domain filter list for `rookie_cookies.extract(..., domains=...)`.
    Note: `rookie_cookies.extract` expects bare hostnames (e.g. `['youtube.com', 'google.com']`),
    and matches subdomains automatically.
    """
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
        # Only pass include_session=True on Gecko browsers (Firefox/LibreWolf/Zen)
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
    """Extract cookies for `target_url` from `browser` using `rookie-cookies` and write an isolated Netscape cookiefile.
    Defaults to `app_bound="disabled"` so that `rookie-cookies` NEVER spawns a new Edge/Chrome window on Windows.
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

    # Set up optional cancellation bridge from CancelToken -> rookie_cookies.CancellationHandle
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
        extract_fn = getattr(rc, "extract", None)
        read_fn = getattr(rc, "read", None)

        if callable(extract_fn):
            kwargs = _build_extract_kwargs(b_id, prof, domains, policy, timeout, cancel_handle)
            try:
                rows = list(extract_fn(**kwargs) or [])
            except TypeError:
                # Always preserve app_bound=policy in fallback kwargs so it never defaults to spawning a browser!
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
            # Never call legacy named helpers (rc.edge(), rc.chrome()) when policy == "disabled"
            # because legacy helpers unconditionally use allow_elevated_fallback and spawn a browser window!
            if policy == "disabled" and b_id in _APP_BOUND_CHROMIUM_BROWSERS:
                rows = []
            else:
                helper_fn = getattr(rc, b_id, None)
                if not callable(helper_fn):
                    raise RuntimeError(f"rookie_cookies 不支持浏览器 {b_id}")
                rows = list(helper_fn(domains=domains) if domains else helper_fn())

        if not rows:
            if on_log:
                dom_desc = f"（目标域: {', '.join(domains)}）" if domains else ""
                if policy == "disabled" and b_id in _APP_BOUND_CHROMIUM_BROWSERS:
                    on_log(
                        f"[Cookies 提示] {b_id} 中的 Cookie 采用 App-Bound (v20) 加密；为避免自动弹出新浏览器窗口，"
                        "已跳过进程注入读取。若该视频需要登录，请在设置中点击「登录并获取 Cookies…」导出一次或改用 Firefox。"
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

        # Ensure every non-comment row has a valid positive expiration timestamp so yt-dlp doesn't discard session cookies
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

        base_temp = temp_dir or Path.cwd() / "temp"
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
