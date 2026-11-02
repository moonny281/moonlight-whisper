"""通过 Chrome DevTools Protocol 从“程序专用浏览器配置目录”读取 Cookies。

要点：
- 使用独立 --user-data-dir（Chrome 136+ 对默认目录会忽略远程调试开关）；
- 注入 --disable-blink-features=AutomationControlled，防止 Google / YouTube 登录拦截“此浏览器或应用可能不安全”；
- Cookies 由浏览器自己解密后经 CDP (Storage.getCookies + Network.getAllCookies) 返回，100% 绕开 App-Bound Encryption (v20)；
- 只导出目标站点关联域名的 Cookies（如 YouTube 同时包含 .youtube.com 与 .google.com）；调试端口仅绑定 127.0.0.1 随机端口与本地 Origin。
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import requests

try:  # websockets>=11 提供同步客户端
    from websockets.sync.client import connect as _ws_connect_raw
except Exception:  # pragma: no cover
    _ws_connect_raw = None


class BrowserCookieError(RuntimeError):
    pass


def find_browser(kind: str = "edge") -> Optional[Path]:
    """定位 Edge / Chrome 可执行文件（Windows）。"""
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


class ManagedBrowser:
    """启动并控制一个使用独立配置目录的 Edge/Chrome 进程。"""

    def __init__(self, exe: Path, profile_dir: Path, headless: bool = False, proxy: str = "") -> None:
        self.exe, self.profile_dir, self.headless = exe, profile_dir, headless
        self.proxy = (proxy or "").strip()
        self.port = _free_port()
        self._proc: Optional[subprocess.Popen] = None

    def start(self, start_url: str = "about:blank", timeout: float = 18.0) -> None:
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        args = [
            str(self.exe),
            f"--remote-debugging-port={self.port}",
            f"--remote-allow-origins=http://127.0.0.1:{self.port}",
            f"--user-data-dir={self.profile_dir}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-blink-features=AutomationControlled",
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
                raise BrowserCookieError("浏览器进程已退出（该专用配置目录可能正被另一个窗口占用，请先关闭后重试）")
            try:
                r = sess.get(f"http://127.0.0.1:{self.port}/json/version", timeout=1.5)
                if r.ok:
                    return
            except requests.RequestException:
                time.sleep(0.3)
        raise BrowserCookieError("等待浏览器调试端口超时")

    def get_cookies(self) -> List[Dict[str, Any]]:
        """同时从 Browser Target (`Storage.getCookies`) 与 Page Target (`Network.getAllCookies` / `Storage.getCookies`)
        收集并合并全部 Cookie，确保 YouTube/Google 跨站登录凭据 100% 完整导出。
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
        raise BrowserCookieError("未从浏览器调试端口获取到任何 Cookies")

    def close(self) -> None:
        if self._proc and self._proc.poll() is None:
            try:
                # 先尝试通过 CDP 优雅关闭，使 Cookie 落盘
                sess = _local_session()
                ver = sess.get(f"http://127.0.0.1:{self.port}/json/version", timeout=2).json()
                ws = _ws_connect(ver["webSocketDebuggerUrl"])
                try:
                    ws.send(json.dumps({"id": 2, "method": "Browser.close"}))
                finally:
                    ws.close()
                self._proc.wait(timeout=8)
            except Exception:
                self._proc.terminate()
        self._proc = None


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


def export_site_cookies(browser: ManagedBrowser, out_file: Path, allowed_domains: Iterable[str]) -> int:
    text, count = cdp_cookies_to_netscape(browser.get_cookies(), allowed_domains)
    out_file.parent.mkdir(parents=True, exist_ok=True)
    out_file.write_text(text, encoding="utf-8")
    try:
        os.chmod(out_file, 0o600)
    except OSError:
        pass
    return count


def refresh_managed_cookies(site_key: str, paths, browser_kind: str = "edge", spawn_browser: bool = False) -> Optional[Path]:
    """返回已导出的站点 Cookies 文件路径；默认绝不在后台启动新的 Edge/Chrome 浏览器进程（避免每次下载弹出新 Edge 窗口）。"""
    from app.core.site_cookies import SITES, export_domains_for

    site = next((s for s in SITES if s.key == site_key), None)
    if site is None:
        return None
    out = paths.data_root / "cookies" / f"{site_key}.txt"
    if not spawn_browser:
        return out if out.exists() else None
    exe = find_browser(browser_kind) or find_browser("chrome")
    if exe is None:
        return out if out.exists() else None
    browser = ManagedBrowser(exe, paths.data_root / "browser_profile", headless=True)
    try:
        browser.start()
        export_site_cookies(browser, out, export_domains_for(site))
        return out
    except Exception:
        return out if out.exists() else None
    finally:
        browser.close()
