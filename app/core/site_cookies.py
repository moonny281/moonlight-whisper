"""站点画像：Cookie 域名绑定、关键 Cookie 检查、登录态探测。"""
from __future__ import annotations

import time
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple


@dataclass(frozen=True)
class SiteProfile:
    key: str
    hosts: Tuple[str, ...]            # 用于匹配输入 URL 的主机后缀
    cookie_domains: Tuple[str, ...]   # 原始 Cookie 头只允许写入这些域
    key_cookies: Tuple[str, ...]      # 任意命中一个即视为“看起来已登录”
    note: str = ""


SITES: Tuple[SiteProfile, ...] = (
    SiteProfile(
        "youtube",
        ("youtube.com", "youtu.be", "youtube-nocookie.com"),
        (".youtube.com",),
        ("__Secure-3PSID", "SAPISID", "LOGIN_INFO"),
        "YouTube 需在无痕窗口导出，并立即关闭该窗口，否则 Cookie 会被轮换失效",
    ),
    SiteProfile(
        "bilibili",
        ("bilibili.com", "b23.tv"),
        (".bilibili.com",),
        ("SESSDATA",),
    ),
    SiteProfile(
        "tiktok",
        ("tiktok.com",),
        (".tiktok.com",),
        ("sessionid", "sessionid_ss"),
    ),
    SiteProfile(
        "x",
        ("x.com", "twitter.com"),
        (".x.com", ".twitter.com"),
        ("auth_token",),
    ),
    SiteProfile(
        "instagram",
        ("instagram.com",),
        (".instagram.com",),
        ("sessionid",),
    ),
)

# 常见多段公共后缀（兜底启发式；不引入 tldextract 依赖）
_MULTI_TLD = {"co.uk", "com.cn", "com.hk", "com.tw", "com.jp", "com.au", "co.kr", "com.br"}


def _host(url: str) -> str:
    try:
        return (urllib.parse.urlparse(url).hostname or "").lower()
    except ValueError:
        return ""


def resolve_site(url: str) -> Optional[SiteProfile]:
    host = _host(url)
    for site in SITES:
        if any(host == h or host.endswith("." + h) for h in site.hosts):
            return site
    return None


def cookie_domains_for(url: str) -> List[str]:
    """原始 Cookie 头应绑定的域：已知站点取其画像域，未知站点取可注册域；绝不再写入无关站点。"""
    site = resolve_site(url)
    if site:
        return list(site.cookie_domains)
    host = _host(url)
    if not host:
        return []
    parts = host.split(".")
    if len(parts) >= 3 and ".".join(parts[-2:]) in _MULTI_TLD:
        return ["." + ".".join(parts[-3:])]
    return ["." + ".".join(parts[-2:])] if len(parts) >= 2 else ["." + host]


@dataclass
class CookieReport:
    total: int = 0
    matched: int = 0                 # 与目标站点域名匹配的条数
    expired: int = 0
    found_keys: List[str] = field(default_factory=list)
    missing_hint: str = ""
    ok: bool = False

    def summary(self) -> str:
        if self.total == 0:
            return "Cookies 文件中没有可解析的记录"
        if self.matched == 0:
            return f"共 {self.total} 条，但没有任何一条属于目标站点域名（导出了错误网站的 Cookies？）"
        if self.missing_hint:
            return f"共 {self.total} 条（匹配 {self.matched} 条），{self.missing_hint}"
        return f"共 {self.total} 条（匹配 {self.matched} 条），已包含登录凭据：{', '.join(self.found_keys)}"


def inspect_netscape(path: Path, target_url: str) -> CookieReport:
    """对（已规范化的）Netscape cookies 文件做静态体检，不发网络请求。"""
    rep = CookieReport()
    site = resolve_site(target_url)
    now = int(time.time())
    domains = tuple(d.lstrip(".") for d in (site.cookie_domains if site else cookie_domains_for(target_url)))
    names_seen: Dict[str, int] = {}
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if line.startswith("#HttpOnly_"):
            line = line[len("#HttpOnly_"):]
        elif not line or line.startswith("#"):
            continue
        cols = line.split("\t")
        if len(cols) < 7:
            continue
        dom, _, _, _, exp, name = cols[0].lstrip("."), cols[1], cols[2], cols[3], cols[4], cols[5]
        rep.total += 1
        if not any(dom == d or dom.endswith("." + d) for d in domains):
            continue
        rep.matched += 1
        try:
            if 0 < int(float(exp)) < now:
                rep.expired += 1
                continue
        except ValueError:
            pass
        names_seen[name] = names_seen.get(name, 0) + 1
    if site:
        rep.found_keys = [k for k in site.key_cookies if k in names_seen]
        if not rep.found_keys and rep.matched:
            rep.missing_hint = f"但缺少登录凭据 Cookie（期望之一：{', '.join(site.key_cookies)}），可能未登录或已过期"
        if site.key == "x" and "auth_token" in names_seen and "ct0" not in names_seen:
            rep.missing_hint = "缺少 ct0"
    rep.ok = rep.matched > 0 and not rep.missing_hint
    return rep


def netscape_to_header(path: Path, target_url: str) -> str:
    """把 Netscape 文件中属于目标站点的有效 Cookie 拼成 `k=v; k2=v2`，用于登录态探测。"""
    site = resolve_site(target_url)
    domains = tuple(d.lstrip(".") for d in (site.cookie_domains if site else cookie_domains_for(target_url)))
    now = int(time.time())
    pairs: List[str] = []
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if line.startswith("#HttpOnly_"):
            line = line[len("#HttpOnly_"):]
        elif not line or line.startswith("#"):
            continue
        cols = line.split("\t")
        if len(cols) < 7:
            continue
        dom = cols[0].lstrip(".")
        if not any(dom == d or dom.endswith("." + d) for d in domains):
            continue
        try:
            if 0 < int(float(cols[4])) < now:
                continue
        except ValueError:
            pass
        pairs.append(f"{cols[5]}={cols[6]}")
    return "; ".join(pairs)


def probe_bilibili_login(cookie_header: str, proxy: str = "", timeout: float = 10.0) -> Optional[bool]:
    """联网探测 B 站登录态：True=已登录，False=未登录/已失效，None=探测失败（网络等）。"""
    import requests  # 延迟导入，保持本模块可无网络依赖地做静态体检

    try:
        resp = requests.get(
            "https://api.bilibili.com/x/web-interface/nav",
            headers={"Cookie": cookie_header, "User-Agent": "Mozilla/5.0", "Referer": "https://www.bilibili.com/"},
            proxies={"http": proxy, "https": proxy} if proxy else None,
            timeout=timeout,
        )
        return bool((resp.json().get("data") or {}).get("isLogin"))
    except Exception:
        return None
