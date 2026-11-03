"""站点画像与插件化站点注册表：Cookie 域名绑定、关键登录凭据校验、登录与自动续期入口。"""
from __future__ import annotations

import json
import time
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple


@dataclass(frozen=True)
class SiteProfile:
    key: str
    hosts: Tuple[str, ...]                  # 用于匹配输入 URL 的主机后缀
    cookie_domains: Tuple[str, ...]         # 原始 Cookie 头只允许写入这些域
    key_cookies: Tuple[str, ...]            # 任意命中一个即视为“已包含登录凭据”
    note: str = ""
    label: str = ""                         # 界面展示名称
    login_url: str = ""                     # 账号托管中心登录入口 URL
    refresh_url: str = ""                   # 后台无头静默刷新访问 URL
    export_domains: Tuple[str, ...] = ()    # 托管浏览器导出允许匹配的域名（为空时默认取 cookie_domains）

    @property
    def display_label(self) -> str:
        return self.label or self.key

    @property
    def effective_login_url(self) -> str:
        if self.login_url:
            return self.login_url
        if self.hosts:
            return f"https://www.{self.hosts[0]}/"
        return "about:blank"

    @property
    def effective_refresh_url(self) -> str:
        if self.refresh_url:
            return self.refresh_url
        if self.hosts:
            return f"https://www.{self.hosts[0]}/"
        return self.effective_login_url


SITES: Tuple[SiteProfile, ...] = (
    SiteProfile(
        "youtube",
        ("youtube.com", "youtu.be", "youtube-nocookie.com"),
        (".youtube.com",),
        ("__Secure-3PSID", "SAPISID", "LOGIN_INFO", "__Secure-1PSID", "SID", "__Secure-3PAPISID"),
        "YouTube 需包含 __Secure-3PSID / SAPISID / LOGIN_INFO 等登录凭据 Cookie",
        label="YouTube / Google (youtube.com)",
        login_url="https://www.youtube.com/",
        refresh_url="https://www.youtube.com/",
        export_domains=(".youtube.com", ".google.com"),
    ),
    SiteProfile(
        "bilibili",
        ("bilibili.com", "b23.tv"),
        (".bilibili.com",),
        ("SESSDATA",),
        label="哔哩哔哩 (bilibili.com)",
        login_url="https://passport.bilibili.com/login",
        refresh_url="https://www.bilibili.com/",
    ),
    SiteProfile(
        "douyin",
        ("douyin.com", "iesdouyin.com"),
        (".douyin.com",),
        ("sessionid", "sessionid_ss", "sid_tt", "LOGIN_STATUS"),
        label="抖音 (douyin.com)",
        login_url="https://www.douyin.com/",
        refresh_url="https://www.douyin.com/",
    ),
    SiteProfile(
        "tiktok",
        ("tiktok.com",),
        (".tiktok.com",),
        ("sessionid", "sessionid_ss", "sid_tt"),
        label="TikTok (tiktok.com)",
        login_url="https://www.tiktok.com/login",
        refresh_url="https://www.tiktok.com/",
    ),
    SiteProfile(
        "x",
        ("x.com", "twitter.com"),
        (".x.com", ".twitter.com"),
        ("auth_token",),
        label="X / Twitter (x.com)",
        login_url="https://x.com/i/flow/login",
        refresh_url="https://x.com/home",
    ),
    SiteProfile(
        "instagram",
        ("instagram.com",),
        (".instagram.com",),
        ("sessionid",),
        label="Instagram (instagram.com)",
        login_url="https://www.instagram.com/accounts/login/",
        refresh_url="https://www.instagram.com/",
    ),
    SiteProfile(
        "xiaohongshu",
        ("xiaohongshu.com", "xhslink.com"),
        (".xiaohongshu.com",),
        ("web_session",),
        label="小红书 (xiaohongshu.com)",
        login_url="https://www.xiaohongshu.com/",
        refresh_url="https://www.xiaohongshu.com/",
    ),
)

# 常见多段公共后缀（兜底启发式；不引入 tldextract 依赖）
_MULTI_TLD = {"co.uk", "com.cn", "com.hk", "com.tw", "com.jp", "com.au", "co.kr", "com.br"}


def load_all_sites(custom_sites_file: Optional[Path] = None) -> Tuple[SiteProfile, ...]:
    """加载内置站点画像及可选 `plugins/sites.json` 用户自定义扩展站点。"""
    by_key: Dict[str, SiteProfile] = {s.key: s for s in SITES}
    if custom_sites_file is not None and custom_sites_file.is_file():
        try:
            raw = json.loads(custom_sites_file.read_text(encoding="utf-8"))
            items = raw if isinstance(raw, list) else (raw.get("sites") if isinstance(raw, dict) else [])
            if isinstance(items, list):
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    key = str(item.get("key") or "").strip().lower()
                    hosts = tuple(str(h).strip().lower().lstrip(".") for h in (item.get("hosts") or []) if str(h).strip())
                    if not key or not hosts:
                        continue
                    c_doms = tuple(
                        ("." + str(d).strip().lower().lstrip("."))
                        for d in (item.get("cookie_domains") or hosts)
                        if str(d).strip()
                    )
                    e_doms = tuple(
                        ("." + str(d).strip().lower().lstrip("."))
                        for d in (item.get("export_domains") or ())
                        if str(d).strip()
                    )
                    k_cookies = tuple(str(k).strip() for k in (item.get("key_cookies") or ()) if str(k).strip())
                    by_key[key] = SiteProfile(
                        key=key,
                        hosts=hosts,
                        cookie_domains=c_doms,
                        key_cookies=k_cookies,
                        note=str(item.get("note") or ""),
                        label=str(item.get("label") or key),
                        login_url=str(item.get("login_url") or ""),
                        refresh_url=str(item.get("refresh_url") or ""),
                        export_domains=e_doms,
                    )
        except Exception:
            pass
    return tuple(by_key.values())


def get_site_by_key(site_key: str, custom_sites_file: Optional[Path] = None) -> Optional[SiteProfile]:
    target = (site_key or "").strip().lower()
    if not target:
        return None
    for s in load_all_sites(custom_sites_file):
        if s.key == target:
            return s
    return None


def _host(url: str) -> str:
    try:
        return (urllib.parse.urlparse(url).hostname or "").lower()
    except ValueError:
        return ""


def resolve_site(url: str, custom_sites_file: Optional[Path] = None) -> Optional[SiteProfile]:
    host = _host(url)
    if not host:
        return None
    for site in load_all_sites(custom_sites_file):
        if any(host == h or host.endswith("." + h) for h in site.hosts):
            return site
    return None


def export_domains_for(site: Optional[SiteProfile]) -> Tuple[str, ...]:
    """托管浏览器导出与登录凭据体检允许匹配的域名集合（如 YouTube 同时包含 .youtube.com 与 .google.com）。"""
    if site is None:
        return ()
    if site.export_domains:
        return site.export_domains
    if site.key == "youtube":
        return (".youtube.com", ".google.com")
    return site.cookie_domains


def cookie_domains_for(url: str, custom_sites_file: Optional[Path] = None) -> List[str]:
    """原始 Cookie 头应绑定的域：已知站点取其画像域，未知站点取可注册域；绝不再写入无关站点。"""
    site = resolve_site(url, custom_sites_file=custom_sites_file)
    if site:
        return list(site.cookie_domains)
    host = _host(url)
    if not host:
        return []
    parts = host.split(".")
    if len(parts) >= 3 and ".".join(parts[-2:]) in _MULTI_TLD:
        return ["." + ".".join(parts[-3:])]
    return ["." + ".".join(parts[-2:])] if len(parts) >= 2 else ["." + host]


def rows_have_site_auth(
    rows: Iterable[Dict[str, Any]],
    target_url: str,
    custom_sites_file: Optional[Path] = None,
) -> bool:
    """检查提取出的 Cookie 字典列表中是否真正包含目标站点的关键登录凭据（避免仅提取到匿名 PREF/YSC 时误判成功）。"""
    site = resolve_site(target_url, custom_sites_file=custom_sites_file)
    rows_list = [r for r in rows if isinstance(r, dict)]
    if not rows_list:
        return False
    if site is None or not site.key_cookies:
        return True
    now = int(time.time())
    allowed_doms = tuple(d.lstrip(".").lower() for d in export_domains_for(site))
    names_found = set()
    for r in rows_list:
        dom = str(r.get("domain") or "").lstrip(".").lower()
        if allowed_doms and not any(dom == d or dom.endswith("." + d) for d in allowed_doms):
            continue
        exp = r.get("expires")
        try:
            exp_i = int(float(exp)) if exp is not None else 0
            if 0 < exp_i < now:
                continue
        except (TypeError, ValueError):
            pass
        name = str(r.get("name") or "").strip()
        val = str(r.get("value") or "").strip()
        if name and val:
            names_found.add(name)
    if site.key == "x" and "auth_token" in names_found and "ct0" not in names_found:
        return False
    return any(k in names_found for k in site.key_cookies)


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


def inspect_netscape(
    path: Path,
    target_url: str,
    custom_sites_file: Optional[Path] = None,
) -> CookieReport:
    """对（已规范化的）Netscape cookies 文件做静态体检，不发网络请求。"""
    rep = CookieReport()
    if not path.is_file():
        return rep
    site = resolve_site(target_url, custom_sites_file=custom_sites_file)
    now = int(time.time())
    domains = tuple(
        d.lstrip(".")
        for d in (
            export_domains_for(site)
            if site
            else cookie_domains_for(target_url, custom_sites_file=custom_sites_file)
        )
    )
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
            rep.missing_hint = f"但缺少登录凭据 Cookie（期望之一：{', '.join(site.key_cookies[:3])}），可能未登录或仅提取到匿名 Cookie"
        if site.key == "x" and "auth_token" in names_seen and "ct0" not in names_seen:
            rep.missing_hint = "缺少 ct0"
    rep.ok = rep.matched > 0 and not rep.missing_hint and rep.matched > rep.expired
    return rep


def netscape_to_header(
    path: Path,
    target_url: str,
    custom_sites_file: Optional[Path] = None,
) -> str:
    """把 Netscape 文件中属于目标站点的有效 Cookie 拼成 `k=v; k2=v2`，用于登录态探测。"""
    site = resolve_site(target_url, custom_sites_file=custom_sites_file)
    domains = tuple(
        d.lstrip(".")
        for d in (
            export_domains_for(site)
            if site
            else cookie_domains_for(target_url, custom_sites_file=custom_sites_file)
        )
    )
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
