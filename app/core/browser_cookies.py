"""网站账号托管与自动续期引擎（Account Vault & CDP Silent Auto-Refresh）。

架构设计：
1. 独立持久化的账号托管库 (`data_root/account_vault/browser_profile`)：
   - 与用户日常使用的 Edge/Chrome 完全物理隔离，绝不读取或干扰日常浏览器；
   - 用户只需在「网站账号托管中心」登录一次各大视频站点（YouTube / B站 / 抖音 / TikTok / X / Instagram / 小红书等），登录态长久保存在托管库中。
2. 零标签页堆积与零单实例冲突保障：
   - 每次启动托管浏览器前，主动清理占用该托管目录的残留进程（`_kill_stale_profile_owner`）；
   - 自动清空 Chromium 会话历史标签页缓存（`Default/Sessions/*`）并将 `Preferences` 的 `exit_type` 设为 `Normal`，保留 100% 登录态与 Cookie 的同时，彻底杜绝历史标签页恢复与堆积。
3. 失效自动静默提取（`refresh_hosted_site_cookies`）：
   - 当 `data_root/cookies/<site>.txt` 过期、缺失或下载中途被站点要求重新验证时，后台以 `--headless=new` 无头模式静默拉起托管库，访问目标站点完成令牌轮换（如 YouTube `__Secure-1PSIDTS`），经本地 CDP (`Storage.getCookies` + `Network.getAllCookies`) 提取明文 Cookie 落盘后立即关闭，全程 0 弹窗、100% 绕开 Windows App-Bound (v20) 限制。
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

import requests

from app.core.site_cookies import (
    CookieReport,
    SiteProfile,
    export_domains_for,
    get_site_by_key,
    inspect_netscape,
    load_all_sites,
    resolve_site,
    rows_have_site_auth,
)
from app.paths import PATHS, AppPaths

try:  # websockets>=11 提供同步客户端
    from websockets.sync.client import connect as _ws_connect_raw
except Exception:  # pragma: no cover
    _ws_connect_raw = None

_VAULT_LOCK = threading.RLock()


class BrowserCookieError(RuntimeError):
    pass


def get_vault_profile_dir(paths: AppPaths = PATHS) -> Path:
    """返回独立持久化的账号托管浏览器 Profile 目录。"""
    return paths.data_root / "account_vault" / "browser_profile"


def get_site_cookie_path(site_key: str, paths: AppPaths = PATHS) -> Path:
    """返回指定托管站点的标准 Netscape Cookies 文件路径 (`data_root/cookies/<site_key>.txt`)。"""
    clean_key = (site_key or "").strip().lower()
    return paths.data_root / "cookies" / f"{clean_key}.txt"


def get_custom_sites_file(paths: AppPaths = PATHS) -> Path:
    """返回可选的用户自定义扩展站点配置文件路径 (`data_root/plugins/sites.json`)。"""
    return paths.plugins_dir / "sites.json"


def has_vault_profile(paths: AppPaths = PATHS) -> bool:
    """判断本机是否已初始化过账号托管浏览器配置目录。"""
    vault_dir = get_vault_profile_dir(paths)
    if not vault_dir.is_dir():
        return False
    return (vault_dir / "Local State").exists() or (vault_dir / "Default").exists()


def find_browser(kind: str = "edge") -> Optional[Path]:
    """定位 Edge / Chrome 可执行文件。"""
    env = os.environ
    pf, pf86, local = env.get("ProgramFiles", ""), env.get("ProgramFiles(x86)", ""), env.get("LOCALAPPDATA", "")
    table = {
        "edge": [
            rf"{pf86}\Microsoft\Edge\Application\msedge.exe",
            rf"{pf}\Microsoft\Edge\Application\msedge.exe",
            rf"{local}\Microsoft\Edge\Application\msedge.exe",
        ],
        "chrome": [
            rf"{pf}\Google\Chrome\Application\chrome.exe",
            rf"{pf86}\Google\Chrome\Application\chrome.exe",
            rf"{local}\Google\Chrome\Application\chrome.exe",
        ],
    }
    for cand in table.get(kind, []):
        if cand and Path(cand).is_file():
            return Path(cand)
    if sys.platform == "win32":
        try:
            import winreg

            exe = "msedge.exe" if kind == "edge" else "chrome.exe"
            for root in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
                try:
                    with winreg.OpenKey(root, rf"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\{exe}") as k:
                        p = Path(winreg.QueryValueEx(k, None)[0])
                        if p.is_file():
                            return p
                except OSError:
                    continue
        except ImportError:
            pass
    else:
        names = ("microsoft-edge", "msedge") if kind == "edge" else ("google-chrome", "chromium", "chromium-browser")
        for name in names:
            found = shutil.which(name)
            if found:
                return Path(found).resolve()
    return None


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _local_session() -> requests.Session:
    s = requests.Session()
    s.trust_env = False            # 不让系统/环境代理拦截 127.0.0.1
    return s


def _ws_connect(url: str):
    if _ws_connect_raw is None:
        raise BrowserCookieError("缺少 websockets>=11，无法与浏览器通信")
    try:
        return _ws_connect_raw(url, max_size=None, proxy=None)   # websockets>=15：显式禁用代理
    except TypeError:
        return _ws_connect_raw(url, max_size=None)               # 旧版本无 proxy 形参


def _cdp_call(ws_url: str, method: str, call_id: int = 1, timeout: float = 10.0) -> Dict[str, Any]:
    ws = _ws_connect(ws_url)
    try:
        ws.send(json.dumps({"id": call_id, "method": method}))
        deadline = time.time() + timeout
        while time.time() < deadline:
            msg = json.loads(ws.recv(timeout=timeout))
            if msg.get("id") == call_id:
                if "error" in msg:
                    raise BrowserCookieError(f"CDP 错误 ({method})：{msg['error']}")
                return dict(msg.get("result") or {})
    finally:
        ws.close()
    raise BrowserCookieError(f"CDP 调用 {method} 超时")


def _kill_stale_profile_owner(profile_dir: Path) -> None:
    """按命令行是否包含该独立托管 profile 路径来清理残留浏览器进程（绝不影响用户日常浏览器）。"""
    if sys.platform != "win32":
        return
    needle = str(profile_dir).strip().lower()
    if not needle:
        return
    try:
        import psutil  # type: ignore

        for p in psutil.process_iter(["name", "cmdline"]):
            try:
                cmdline = " ".join(p.info.get("cmdline") or []).lower()
                if needle in cmdline:
                    p.terminate()
            except Exception:
                continue
        return
    except ImportError:
        pass
    except Exception:
        return

    try:
        escaped = str(profile_dir).replace("'", "''").replace("\\", "\\\\")
        ps_cmd = (
            "Get-CimInstance Win32_Process | "
            f"Where-Object {{ $_.CommandLine -match '{escaped}' }} | "
            "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
        )
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps_cmd],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=4,
            creationflags=flags,
            check=False,
        )
    except Exception:
        pass


def _prepare_clean_profile_session(profile_dir: Path) -> None:
    """在保留托管账号 Cookies 与 LocalStorage 的前提下，清除单实例锁与历史打开标签页，彻底杜绝标签页恢复堆积。"""
    if not profile_dir.exists():
        return
    for lock_name in ("SingletonLock", "SingletonCookie", "SingletonSocket", "lockfile"):
        lock_p = profile_dir / lock_name
        try:
            if lock_p.exists() or lock_p.is_symlink():
                lock_p.unlink()
        except OSError:
            pass

    default_dir = profile_dir / "Default"
    if not default_dir.is_dir():
        return

    sessions_dir = default_dir / "Sessions"
    if sessions_dir.is_dir():
        shutil.rmtree(sessions_dir, ignore_errors=True)

    for legacy_tab_file in ("Current Session", "Current Tabs", "Last Session", "Last Tabs"):
        p = default_dir / legacy_tab_file
        try:
            if p.is_file():
                p.unlink()
        except OSError:
            pass

    prefs_file = default_dir / "Preferences"
    if prefs_file.is_file():
        try:
            prefs = json.loads(prefs_file.read_text(encoding="utf-8", errors="replace"))
            if isinstance(prefs, dict):
                prof_sec = prefs.setdefault("profile", {})
                if isinstance(prof_sec, dict):
                    prof_sec["exit_type"] = "Normal"
                    prof_sec["exited_cleanly"] = True
                prefs_file.write_text(json.dumps(prefs, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass


class ManagedBrowser:
    """启动并控制使用独立托管配置目录的 Edge/Chrome 进程（支持可见登录窗口与后台无头静默刷新）。"""

    def __init__(
        self,
        exe: Path,
        profile_dir: Path,
        headless: bool = False,
        proxy: str = "",
        app_window: bool = False,
        window_size: Tuple[int, int] = (480, 780),
        window_pos: Optional[Tuple[int, int]] = None,
    ) -> None:
        self.exe, self.profile_dir, self.headless = exe, profile_dir, headless
        self.proxy = (proxy or "").strip()
        # app_window=True：以 `--app=URL` 单窗口小窗模式启动（无标签栏/地址栏，不会产生多余标签页）
        self.app_window = bool(app_window)
        self.window_size = window_size
        self.window_pos = window_pos
        self.port = _free_port()
        self._proc: Optional[subprocess.Popen] = None

    def start(self, start_url: str = "about:blank", timeout: float = 18.0) -> None:
        _kill_stale_profile_owner(self.profile_dir)
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        _prepare_clean_profile_session(self.profile_dir)
        args = [
            str(self.exe),
            f"--remote-debugging-port={self.port}",
            f"--remote-allow-origins=http://127.0.0.1:{self.port}",
            f"--user-data-dir={self.profile_dir}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-session-crashed-bubble",
            "--hide-crash-restore-bubble",
            "--disable-blink-features=AutomationControlled",
            # 独立托管目录不参与任何账号同步：关闭同步、跳过首次运行向导，避免 Edge 弹出登录/同步窗口与多余标签页
            "--disable-sync",
            "--disable-default-apps",
            "--disable-features=msImplicitSignin,msEdgeOnRampFRE",
        ]
        if self.proxy:
            args.append(f"--proxy-server={self.proxy}")
        startupinfo = None
        if self.headless:
            args += [
                "--headless=new",
                "--disable-gpu",
                "--no-startup-window",
                "--silent-launch",
                "--window-position=-32000,-32000",
                "--window-size=1,1",
            ]
            if sys.platform == "win32" and hasattr(subprocess, "STARTUPINFO"):
                startupinfo = subprocess.STARTUPINFO()
                startupinfo.dwFlags |= getattr(subprocess, "STARTF_USESHOWWINDOW", 1)
                startupinfo.wShowWindow = getattr(subprocess, "SW_HIDE", 0)
        if self.app_window and not self.headless:
            args.append(f"--app={start_url}")
            args.append(f"--window-size={int(self.window_size[0])},{int(self.window_size[1])}")
            if self.window_pos is not None:
                args.append(f"--window-position={int(self.window_pos[0])},{int(self.window_pos[1])}")
        else:
            args.append(start_url)
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000) if (self.headless and sys.platform == "win32") else 0
        self._proc = subprocess.Popen(
            args,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=flags,
            startupinfo=startupinfo,
        )
        deadline = time.time() + timeout
        sess = _local_session()
        while time.time() < deadline:
            if self._proc.poll() is not None:
                raise BrowserCookieError("托管浏览器进程已退出，请重试打开登录窗口")
            try:
                r = sess.get(f"http://127.0.0.1:{self.port}/json/version", timeout=1.5)
                if r.ok:
                    return
            except requests.RequestException:
                time.sleep(0.3)
        raise BrowserCookieError("等待托管浏览器调试端口超时")

    def alive(self) -> bool:
        """托管浏览器进程是否仍在运行（用户手动关掉登录小窗后为 False）。"""
        return self._proc is not None and self._proc.poll() is None

    def page_urls(self) -> List[str]:
        """当前所有页面（标签/小窗）的 URL，用于判断用户是否已回到目标站点。"""
        try:
            targets = _local_session().get(f"http://127.0.0.1:{self.port}/json/list", timeout=3).json()
        except Exception:
            return []
        return [str(t.get("url") or "") for t in targets if isinstance(t, dict) and t.get("type") == "page"]

    def get_cookies(self) -> List[Dict[str, Any]]:
        """同时从 Browser Target (`Storage.getCookies`) 与 Page Target (`Network.getAllCookies` / `Storage.getCookies`)
        收集并合并全部 Cookie，确保 YouTube/Google 等跨站登录凭据 100% 完整导出。
        """
        sess = _local_session()
        merged: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
        last_err: Optional[Exception] = None

        # 1. Try Browser-level WebSocket (`/json/version` -> `Storage.getCookies`)
        try:
            ver = sess.get(f"http://127.0.0.1:{self.port}/json/version", timeout=5).json()
            ws_url = ver.get("webSocketDebuggerUrl")
            if ws_url:
                res = _cdp_call(ws_url, "Storage.getCookies", call_id=1)
                for c in res.get("cookies") or []:
                    if isinstance(c, dict) and c.get("name"):
                        key = (str(c.get("domain") or ""), str(c.get("path") or "/"), str(c.get("name") or ""))
                        merged[key] = c
        except Exception as exc:
            last_err = exc

        # 2. Also query active Page targets (`/json/list` -> `Network.getAllCookies` & `Storage.getCookies`)
        try:
            targets = sess.get(f"http://127.0.0.1:{self.port}/json/list", timeout=5).json()
            if isinstance(targets, list):
                for t in targets:
                    if not isinstance(t, dict):
                        continue
                    page_ws = t.get("webSocketDebuggerUrl")
                    if not page_ws:
                        continue
                    for idx, method in enumerate(("Network.getAllCookies", "Storage.getCookies"), start=10):
                        try:
                            res = _cdp_call(page_ws, method, call_id=idx, timeout=6.0)
                            for c in res.get("cookies") or []:
                                if isinstance(c, dict) and c.get("name"):
                                    key = (
                                        str(c.get("domain") or ""),
                                        str(c.get("path") or "/"),
                                        str(c.get("name") or ""),
                                    )
                                    merged[key] = c
                        except Exception as exc:
                            last_err = exc
        except Exception as exc:
            last_err = exc

        if merged:
            return list(merged.values())
        if last_err is not None:
            raise BrowserCookieError(f"读取 Cookies 失败：{last_err}") from last_err
        raise BrowserCookieError("未从托管浏览器调试端口获取到任何 Cookies")

    def close(self) -> None:
        if self._proc and self._proc.poll() is None:
            try:
                # 先尝试通过 CDP 优雅关闭，使新签发的 Cookie 完整落盘至托管 Profile
                sess = _local_session()
                ver = sess.get(f"http://127.0.0.1:{self.port}/json/version", timeout=2).json()
                ws = _ws_connect(ver["webSocketDebuggerUrl"])
                try:
                    ws.send(json.dumps({"id": 2, "method": "Browser.close"}))
                finally:
                    ws.close()
                self._proc.wait(timeout=8)
            except Exception:
                try:
                    self._proc.terminate()
                    self._proc.wait(timeout=3)
                except Exception:
                    pass
        self._proc = None
        _kill_stale_profile_owner(self.profile_dir)
        _prepare_clean_profile_session(self.profile_dir)


def cdp_cookies_to_netscape(cookies: Iterable[Dict[str, Any]], allowed_domains: Iterable[str]) -> Tuple[str, int]:
    """CDP 的 cookie 列表 -> Netscape 文本；仅保留 allowed_domains（保留 host-only 与 domain cookie 原始语义）。"""
    allow = tuple(d.lstrip(".").lower() for d in allowed_domains)
    session_exp = int(time.time()) + 86400 * 30
    lines = ["# Netscape HTTP Cookie File", "# Exported from managed browser profile by Moonlight Whisper", ""]
    count = 0
    for c in cookies:
        dom = str(c.get("domain") or "").strip()
        bare = dom.lstrip(".").lower()
        if allow and not any(bare == a or bare.endswith("." + a) for a in allow):
            continue
        name, value = str(c.get("name") or "").strip(), str(c.get("value") or "")
        if not dom or not name:
            continue
        exp = c.get("expires")
        exp_i = int(exp) if isinstance(exp, (int, float)) and exp > 0 else session_exp
        prefix = "#HttpOnly_" if c.get("httpOnly") else ""
        safe_val = value.replace("\t", "%09").replace("\r", "%0D").replace("\n", "%0A")
        lines.append("\t".join([
            f"{prefix}{dom}",
            "TRUE" if dom.startswith(".") else "FALSE",
            str(c.get("path") or "/"),
            "TRUE" if c.get("secure") else "FALSE",
            str(exp_i),
            name,
            safe_val,
        ]))
        count += 1
    return "\n".join(lines) + "\n", count


def _write_netscape_atomic(out_file: Path, text: str) -> None:
    out_file.parent.mkdir(parents=True, exist_ok=True)
    tmp_file = out_file.with_suffix(out_file.suffix + ".tmp")
    tmp_file.write_text(text, encoding="utf-8")
    try:
        os.chmod(tmp_file, 0o600)
    except OSError:
        pass
    tmp_file.replace(out_file)


def export_site_cookies(browser: ManagedBrowser, out_file: Path, allowed_domains: Iterable[str]) -> int:
    text, count = cdp_cookies_to_netscape(browser.get_cookies(), allowed_domains)
    _write_netscape_atomic(out_file, text)
    return count


def export_logged_in_sites_from_browser(
    browser: ManagedBrowser,
    paths: AppPaths = PATHS,
    primary_site_key: str = "",
) -> Dict[str, CookieReport]:
    """从当前打开的托管浏览器一次性读取全部 Cookies，自动为当前选中站点及所有已登录站点导出 `<site>.txt`。"""
    all_cookies = browser.get_cookies()
    custom_file = get_custom_sites_file(paths)
    reports: Dict[str, CookieReport] = {}
    for site in load_all_sites(custom_file):
        check_url = site.effective_refresh_url
        is_primary = site.key == (primary_site_key or "").strip().lower()
        has_auth = rows_have_site_auth(all_cookies, check_url, custom_sites_file=custom_file)
        if not is_primary and not has_auth:
            continue
        out_path = get_site_cookie_path(site.key, paths)
        text, count = cdp_cookies_to_netscape(all_cookies, export_domains_for(site))
        if count > 0 or is_primary:
            _write_netscape_atomic(out_path, text)
            reports[site.key] = inspect_netscape(out_path, check_url, custom_sites_file=custom_file)
    return reports


def list_hosted_sites_status(paths: AppPaths = PATHS) -> List[Tuple[SiteProfile, Path, CookieReport]]:
    """列出所有已注册站点的本地托管 Cookie 状态。"""
    custom_file = get_custom_sites_file(paths)
    results: List[Tuple[SiteProfile, Path, CookieReport]] = []
    for site in load_all_sites(custom_file):
        c_path = get_site_cookie_path(site.key, paths)
        rep = inspect_netscape(c_path, site.effective_refresh_url, custom_sites_file=custom_file)
        results.append((site, c_path, rep))
    return results


def clear_hosted_site_cookies(site_key: str, paths: AppPaths = PATHS) -> bool:
    """删除指定站点的已导出 Cookie 文件。"""
    p = get_site_cookie_path(site_key, paths)
    if p.is_file():
        try:
            p.unlink()
            return True
        except OSError:
            return False
    return False


def refresh_hosted_site_cookies(
    site_or_url: str | SiteProfile,
    paths: AppPaths = PATHS,
    proxy: str = "",
    timeout: float = 18.0,
    settle_sec: float = 2.5,
    on_log: Optional[Callable[[str], None]] = None,
) -> Optional[Path]:
    """当托管站点的 Cookie 过期或触发验证时，在后台以无头模式 (`headless=True`) 静默启动托管浏览器配置目录，
    访问目标站点完成令牌刷新并重新导出 `data_root/cookies/<site>.txt`。
    """
    custom_file = get_custom_sites_file(paths)
    if isinstance(site_or_url, SiteProfile):
        site: Optional[SiteProfile] = site_or_url
    else:
        raw = str(site_or_url or "").strip()
        site = get_site_by_key(raw, custom_file) or resolve_site(raw, custom_file)
    if site is None:
        return None

    if not has_vault_profile(paths):
        # 若未创建独立 vault_profile，尝试直接从用户当前现有浏览器静默提取最新 Cookie 更新托管文件
        try:
            from app.core import rookie_bridge

            for cand_b in ("edge", "chrome", "firefox"):
                extracted = rookie_bridge.extract_to_netscape_file(
                    browser=cand_b,
                    target_url=site.effective_refresh_url,
                    temp_dir=paths.temp_dir,
                    app_bound="smart",
                    force_refresh=True,
                    on_log=on_log,
                )
                if extracted is not None and extracted.is_file():
                    out_file = get_site_cookie_path(site.key, paths)
                    out_file.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(extracted, out_file)
                    rep = inspect_netscape(out_file, site.effective_refresh_url, custom_sites_file=custom_file)
                    if rep.ok:
                        return out_file
        except Exception:
            pass
        return None

    exe = find_browser("edge") or find_browser("chrome")
    if exe is None:
        if on_log:
            on_log("[账号托管] 未检测到 Edge 或 Chrome，无法执行后台静默刷新。")
        return None

    vault_profile = get_vault_profile_dir(paths)
    refresh_url = site.effective_refresh_url
    out_file = get_site_cookie_path(site.key, paths)

    with _VAULT_LOCK:
        if on_log:
            on_log(f"[账号托管] 正在后台静默启动托管浏览器刷新 {site.display_label} 的登录凭据...")
        browser = ManagedBrowser(exe, vault_profile, headless=True, proxy=proxy)
        try:
            browser.start(refresh_url, timeout=timeout)
            deadline = time.time() + max(0.5, settle_sec)
            latest_cookies: List[Dict[str, Any]] = []
            while time.time() < deadline:
                time.sleep(0.5)
                try:
                    latest_cookies = browser.get_cookies()
                except Exception:
                    continue
                if rows_have_site_auth(latest_cookies, refresh_url, custom_sites_file=custom_file):
                    # 额外等待一小段时间让页面脚本完成令牌轮换（如 __Secure-1PSIDTS）
                    time.sleep(min(0.8, max(0.0, deadline - time.time())))
                    try:
                        latest_cookies = browser.get_cookies()
                    except Exception:
                        pass
                    break

            if not latest_cookies:
                latest_cookies = browser.get_cookies()

            text, count = cdp_cookies_to_netscape(latest_cookies, export_domains_for(site))
            if count <= 0:
                if on_log:
                    on_log(f"[账号托管] 托管浏览器中未找到 {site.display_label} 的 Cookie，请先在「网站账号托管中心」登录一次。")
                return None

            _write_netscape_atomic(out_file, text)
            rep = inspect_netscape(out_file, refresh_url, custom_sites_file=custom_file)
            if rep.ok:
                if on_log:
                    on_log(f"[账号托管] 已成功静默提取并刷新 {site.display_label} 的最新凭据（{rep.summary()}）")
                return out_file

            if on_log:
                on_log(f"[账号托管] 托管库中的 {site.display_label} 登录态已失效（{rep.summary()}），请打开「网站账号托管中心」重新登录。")
            return None
        except Exception as exc:
            if on_log:
                on_log(f"[账号托管] 后台静默刷新 {site.display_label} 凭据未成功：{exc}")
            return None
        finally:
            browser.close()


def open_url_in_existing_browser(url: str, preferred_browser: str = "") -> bool:
    """直接在用户当前已有的浏览器中打开一个新标签页，绝不新建 --user-data-dir 配置目录、绝不触发多窗口同步弹窗。"""
    import webbrowser

    target_url = (url or "").strip() or "about:blank"
    kind = (preferred_browser or "").strip().lower()
    if kind in ("edge", "chrome"):
        exe = find_browser(kind)
        if exe is not None and exe.is_file():
            try:
                flags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000) if sys.platform == "win32" else 0
                subprocess.Popen(
                    [str(exe), target_url],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=flags,
                )
                return True
            except Exception:
                pass
    try:
        return bool(webbrowser.open(target_url, new=2, autoraise=True))
    except Exception:
        return False
