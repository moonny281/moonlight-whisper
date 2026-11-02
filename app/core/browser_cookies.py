"""通过 Chrome DevTools Protocol 从“程序专用浏览器配置目录”读取 Cookies。

要点：
- 使用独立 --user-data-dir（Chrome 136+ 对默认目录会忽略远程调试开关）；
- Cookies 由浏览器自己解密后经 CDP 返回，绕开 App-Bound Encryption；
- 只导出目标站点域名的 Cookies；调试端口仅绑定回环地址并使用随机端口。
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


class ManagedBrowser:
    """启动并控制一个使用独立配置目录的 Edge/Chrome 进程。"""

    def __init__(self, exe: Path, profile_dir: Path, headless: bool = False) -> None:
        self.exe, self.profile_dir, self.headless = exe, profile_dir, headless
        self.port = _free_port()
        self._proc: Optional[subprocess.Popen] = None

    def start(self, start_url: str = "about:blank", timeout: float = 15.0) -> None:
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        args = [
            str(self.exe),
            f"--remote-debugging-port={self.port}",
            f"--user-data-dir={self.profile_dir}",
            "--no-first-run",
            "--no-default-browser-check",
        ]
        if self.headless:
            args += ["--headless=new", "--disable-gpu"]
        args.append(start_url)
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if self.headless else 0
        self._proc = subprocess.Popen(
            args,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=flags,
        )
        deadline = time.time() + timeout
        sess = _local_session()
        while time.time() < deadline:
            if self._proc.poll() is not None:
                raise BrowserCookieError("浏览器进程已退出（该配置目录可能正被另一个实例占用）")
            try:
                r = sess.get(f"http://127.0.0.1:{self.port}/json/version", timeout=1.5)
                if r.ok:
                    return
            except requests.RequestException:
                time.sleep(0.3)
        raise BrowserCookieError("等待浏览器调试端口超时")

    def get_cookies(self) -> List[Dict[str, Any]]:
        sess = _local_session()
        ver = sess.get(f"http://127.0.0.1:{self.port}/json/version", timeout=5).json()
        ws_url = ver.get("webSocketDebuggerUrl")
        if not ws_url:
            raise BrowserCookieError("浏览器未返回 webSocketDebuggerUrl")
        ws = _ws_connect(ws_url)
        try:
            ws.send(json.dumps({"id": 1, "method": "Storage.getCookies"}))
            deadline = time.time() + 15
            while time.time() < deadline:
                msg = json.loads(ws.recv(timeout=15))
                if msg.get("id") == 1:
                    if "error" in msg:
                        raise BrowserCookieError(f"CDP 错误：{msg['error']}")
                    return list(msg.get("result", {}).get("cookies", []))
        finally:
            ws.close()
        raise BrowserCookieError("读取 Cookies 超时")

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
    """CDP 的 cookie 列表 -> Netscape 文本；仅保留 allowed_domains（如 ['.bilibili.com']）。"""
    allow = tuple(d.lstrip(".").lower() for d in allowed_domains)
    session_exp = int(time.time()) + 86400 * 30
    lines = ["# Netscape HTTP Cookie File", "# Exported from managed browser profile by Moonlight Whisper", ""]
    count = 0
    for c in cookies:
        dom = str(c.get("domain") or "")
        bare = dom.lstrip(".").lower()
        if allow and not any(bare == a or bare.endswith("." + a) for a in allow):
            continue
        name, value = str(c.get("name") or ""), str(c.get("value") or "")
        if not name:
            continue
        exp = c.get("expires")
        exp_i = int(exp) if isinstance(exp, (int, float)) and exp > 0 else session_exp
        prefix = "#HttpOnly_" if c.get("httpOnly") else ""
        lines.append("\t".join([
            f"{prefix}{dom}",
            "TRUE" if dom.startswith(".") else "FALSE",
            str(c.get("path") or "/"),
            "TRUE" if c.get("secure") else "FALSE",
            str(exp_i),
            name,
            value,
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


def refresh_managed_cookies(site_key: str, paths, browser_kind: str = "edge") -> Optional[Path]:
    """每次任务前静默刷新 cookies；失败（例如登录窗口仍开着导致配置目录被占用）不应阻断任务。"""
    from app.core.site_cookies import SITES

    site = next((s for s in SITES if s.key == site_key), None)
    exe = find_browser(browser_kind) or find_browser("chrome")
    if site is None or exe is None:
        return None
    out = paths.data_root / "cookies" / f"{site_key}.txt"
    browser = ManagedBrowser(exe, paths.data_root / "browser_profile", headless=True)
    try:
        browser.start()
        export_site_cookies(browser, out, site.cookie_domains)
        return out
    except Exception:
        return out if out.exists() else None     # 退回上一次导出的文件
    finally:
        browser.close()
