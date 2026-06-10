from __future__ import annotations

import html
import hashlib
import json
import os
import re
import tempfile
import time
from dataclasses import dataclass, asdict, field
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse
from xml.etree import ElementTree

import requests
from bs4 import BeautifulSoup

from knowledge_base import WECHAT_CANDIDATE_SOURCE_TYPE, add_document, filename_safe


SOGOU_WECHAT_URL = "https://weixin.sogou.com/weixin"
WECHAT_CACHE_DIR = Path(os.getenv("INDUSTRYSCOPE_WECHAT_CACHE_DIR", "data/wechat_cache"))
SEARCH_CACHE_DIR = WECHAT_CACHE_DIR / "search"
ARTICLE_CACHE_DIR = WECHAT_CACHE_DIR / "article"
WECHAT_COLLECTOR_BOOKMARKLET = '''javascript:(async()=>{const T=s=>(s||"").replace(/\\s+/g," ").trim(),Q=s=>document.querySelector(s),C=Q("#js_content")||Q(".rich_media_content")||document.body,I=[...C.querySelectorAll("img")].map((m,i)=>({index:i+1,src:m.getAttribute("data-src")||m.currentSrc||m.src||"",alt:T(m.alt||m.getAttribute("data-type")||""),nearby:T((m.closest("p,section,figure")||m.parentElement||{}).innerText||"").slice(0,240)})).filter(x=>x.src),P={source:"IndustryScopeWeChatClip",version:1,captured_at:new Date().toISOString(),title:T((Q("#activity-name")||Q("h1")||{}).innerText),url:location.href,account:T((Q("#js_name")||Q(".profile_nickname")||{}).innerText),published_at:T((Q("#publish_time")||{}).innerText),content:(C.innerText||"").trim(),images:I};const S=JSON.stringify(P);try{await navigator.clipboard.writeText(S)}catch(e){const a=document.createElement("textarea");a.value=S;document.body.appendChild(a);a.select();document.execCommand("copy");a.remove()}alert("已复制到剪贴板：回到 IndustryScope 粘贴并入库。图片链接 "+I.length+" 个。");try{window.opener&&window.opener.focus();setTimeout(()=>window.close(),300)}catch(e){}})()'''
DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,text/plain;q=0.8,*/*;q=0.7",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Referer": "https://weixin.sogou.com/",
}
HEADER_ALLOWLIST = {
    "accept": "Accept",
    "accept-language": "Accept-Language",
    "cookie": "Cookie",
    "referer": "Referer",
    "user-agent": "User-Agent",
}


@dataclass
class WechatSearchResult:
    title: str
    search_url: str
    url: str
    account: str
    published_at: str
    published_ts: int
    snippet: str
    rank: int
    status: str = "候选"
    error: str = ""
    relevance: int = 0


@dataclass
class WechatArticle:
    title: str
    url: str
    account: str
    published_at: str
    content: str
    html_title: str = ""
    images: list[dict[str, str]] = field(default_factory=list)


def normalize_space(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def repair_mojibake(value: str) -> str:
    """Repair common UTF-8-as-Latin-1 mojibake seen in Sogou snippets."""
    text = value or ""
    if not re.search(r"[ÃÂåæçèéä]", text):
        return text
    try:
        repaired = bytes((ord(char) & 0xFF for char in text)).decode("utf-8", errors="replace")
        return repaired if re.search(r"[\u4e00-\u9fff]", repaired) else text
    except Exception:
        return text


def clean_text(text: str) -> str:
    text = repair_mojibake(html.unescape(text or ""))
    text = re.sub(r"<!--red_beg-->|<!--red_end-->", "", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = text.replace("\xa0", " ")
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def normalize_sogou_url(url: str) -> str:
    decoded = clean_text(url).replace("&amp;", "&")
    if decoded.startswith("//"):
        return f"https:{decoded}"
    if decoded.startswith("/"):
        return urljoin("https://weixin.sogou.com", decoded)
    return decoded


def configured_wechat_feeds() -> list[str]:
    raw = os.getenv("WECHAT_RSS_FEEDS", "")
    parts = re.split(r"[,\n]", raw)
    return [part.strip() for part in parts if re.match(r"^https?://", part.strip(), re.I)]


def split_search_terms(value: str) -> list[str]:
    text = value or ""
    latin_terms = re.findall(r"[a-z0-9][a-z0-9.+-]{1,}", text.lower())
    cjk_terms: list[str] = []
    for term in re.findall(r"[\u4e00-\u9fff]{2,}", text):
        cjk_terms.extend(expand_cjk_term(term))
    seen: set[str] = set()
    terms: list[str] = []
    for term in latin_terms + cjk_terms:
        if term and term not in seen and not is_generic_term(term):
            seen.add(term)
            terms.append(term)
    return terms


def expand_cjk_term(term: str) -> list[str]:
    cleaned = re.sub(r"行业|产业|领域|赛道|最新|新闻|要闻|公司|企业", "", term or "")
    if not cleaned:
        return []
    if len(cleaned) <= 4:
        return [cleaned]
    parts = [cleaned]
    parts.extend(cleaned[idx:idx + 2] for idx in range(0, len(cleaned) - 1))
    return parts


def is_generic_term(term: str) -> bool:
    return term.lower() in {
        "行业", "产业", "领域", "赛道", "最新", "新闻", "要闻", "公司", "企业",
        "market", "industry", "latest", "news", "technology", "policy", "update", "company",
    }


def build_wechat_search_profile(keyword: str) -> dict[str, Any]:
    raw = normalize_space(keyword)
    canonical = re.sub(r"行业|产业|领域|赛道|公司|企业|最新|新闻|要闻", "", raw).strip() or raw
    aliases = {canonical, raw}
    exact_terms: set[str] = set()
    text = f"{raw} {canonical}"

    if re.search(r"脑机|bci|brain.?computer|neuralink|侵入式|非侵入式", text, re.I):
        aliases.update([
            "脑机接口",
            "脑机接口行业",
            "BCI",
            "brain-computer interface",
            "brain computer interface",
            "neural interface",
            "neurotechnology",
            "Neuralink",
        ])
    if re.search(r"澜昆微|lankun", text, re.I):
        for term in ["澜昆微", "澜昆微电子", "上海澜昆微电子", "上海澜昆微电子科技有限公司", "Lankun Micro"]:
            aliases.add(term)
            exact_terms.add(term)

    required_terms: list[str] = []
    for alias in aliases:
        required_terms.extend(split_search_terms(alias))
    return {
        "canonical": canonical,
        "aliases": [item for item in dict.fromkeys(aliases) if item],
        "exact_terms": [item for item in dict.fromkeys(exact_terms) if item],
        "required_terms": [item for item in dict.fromkeys(required_terms) if item],
    }


def build_sogou_weixin_queries(keyword: str) -> list[str]:
    profile = build_wechat_search_profile(keyword)
    queries = [item for item in [*profile["exact_terms"], profile["canonical"], *profile["aliases"]] if re.search(r"[\u4e00-\u9fff]", item)]
    return list(dict.fromkeys(queries or [keyword]))[:3]


def wechat_relevance_score(title: str, snippet: str, account: str, keyword: str) -> int:
    profile = build_wechat_search_profile(keyword)
    text = normalize_space(f"{title} {snippet} {account}").lower()
    title_text = normalize_space(title).lower()
    account_text = normalize_space(account).lower()
    snippet_text = normalize_space(snippet).lower()
    score = 42
    exact_snippet_only = False

    if profile["exact_terms"]:
        exact_title_hit = any(term.lower() in title_text or term.lower() in account_text for term in profile["exact_terms"])
        exact_snippet_hit = any(term.lower() in snippet_text for term in profile["exact_terms"])
        if not exact_title_hit and not exact_snippet_hit:
            return -100
        if exact_title_hit:
            score += 34
        else:
            # Company-only mentions buried in snippets are useful clues, but often weak listicle mentions.
            exact_snippet_only = True
            score -= 18

    alias_hit = any(len(alias) > 1 and alias.lower() in text for alias in profile["aliases"])
    alias_title_hit = any(len(alias) > 1 and alias.lower() in title_text for alias in profile["aliases"])
    required_hits = [term for term in profile["required_terms"] if term.lower() in text]
    if alias_title_hit:
        score += 24
    elif alias_hit:
        score += 24
    elif len(required_hits) >= min(2, max(1, len(profile["required_terms"]))):
        score += 12
    else:
        return -30

    for term in profile["required_terms"]:
        term_l = term.lower()
        if term_l in title_text:
            score += 12
        elif term_l in text:
            score += 5

    if re.search(r"调研纪要|专家纪要|专家访谈|电话会|交流纪要|路演纪要|产业链调研|机构调研|投资者交流|纪要全文|研报翻译|海外研报|摘译|投行|券商", text, re.I):
        score += 18
    if re.search(r"goldman|高盛|morgan stanley|摩根士丹利|j\.?p\.? morgan|摩根大通|bernstein|伯恩斯坦|semianalysis|semi analysis|yole|omdia|gartner|idc|trendforce|techinsights", text, re.I):
        score += 14
    if re.search(r"涨停|牛股|妖股|翻倍|财富密码|封神|炸裂|仅用\d+天|全民.*时代|到底在急什么|大消息|重磅利好|付费|课程|训练营|社群|招商|广告", text, re.I):
        score -= 28
    if re.search(r"低估|收藏|名单|概念|行情|赶紧|太狠|硬核公司|附名单|龙头", title_text, re.I):
        score -= 34
    if len(title_text) > 70:
        score -= 6
    if exact_snippet_only:
        cap = 45 if re.search(r"调研|纪要|访谈|电话会|投融资|融资|发布|首发|客户|验证|量产", text, re.I) else 35
        score = min(score, cap)
    return score


def is_relevant_wechat_result(title: str, snippet: str, account: str, keyword: str) -> bool:
    return wechat_relevance_score(title, snippet, account, keyword) >= 20


def wechat_relevance_band(score: int) -> int:
    if score >= 60:
        return 3
    if score >= 36:
        return 2
    if score >= 20:
        return 1
    return 0


def ensure_wechat_cache_dirs() -> None:
    SEARCH_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    ARTICLE_CACHE_DIR.mkdir(parents=True, exist_ok=True)


def cache_key(value: str) -> str:
    return hashlib.sha256((value or "").encode("utf-8")).hexdigest()[:24]


def cache_path(cache_dir: Path, key: str) -> Path:
    ensure_wechat_cache_dirs()
    return cache_dir / f"{key}.json"


def read_cache(path: Path, ttl_days: int) -> Any | None:
    if ttl_days <= 0 or not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        cached_at = datetime.fromisoformat(data.get("_cached_at", ""))
        if datetime.now() - cached_at > timedelta(days=ttl_days):
            return None
        return data.get("payload")
    except Exception:
        return None


def write_cache(path: Path, payload: Any) -> None:
    ensure_wechat_cache_dirs()
    data = {"_cached_at": datetime.now().isoformat(timespec="seconds"), "payload": payload}
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def request_with_backoff(
    session: requests.Session,
    url: str,
    *,
    params: dict[str, str] | None = None,
    timeout: int = 12,
    allow_redirects: bool = True,
    max_retries: int = 3,
    base_delay: float = 2.0,
) -> requests.Response:
    last_exc: Exception | None = None
    for attempt in range(max(1, max_retries)):
        try:
            response = session.get(url, params=params, timeout=timeout, allow_redirects=allow_redirects)
            if response.status_code not in {429, 500, 502, 503, 504}:
                return response
            last_exc = RuntimeError(f"HTTP {response.status_code}")
        except requests.RequestException as exc:
            last_exc = exc
        if attempt < max_retries - 1:
            time.sleep(min(20.0, base_delay * (2 ** attempt)))
    if last_exc:
        raise last_exc
    raise RuntimeError("请求失败。")


def timestamp_to_date(value: str) -> tuple[str, int]:
    try:
        ts = int(value)
    except (TypeError, ValueError):
        return "", 0
    if ts <= 0:
        return "", 0
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d"), ts


def extract_sogou_date(li: Any) -> tuple[str, int]:
    text = str(li)
    match = re.search(r"timeConvert\('(\d{9,11})'\)", text)
    if match:
        return timestamp_to_date(match.group(1))
    match = re.search(r"\b(20\d{2}[-/.年]\d{1,2}[-/.月]\d{1,2})", li.get_text(" ", strip=True))
    if match:
        date = match.group(1).replace("年", "-").replace("月", "-").replace("/", "-").replace(".", "-").strip("-")
        try:
            parsed = datetime.strptime(date, "%Y-%m-%d")
            return parsed.strftime("%Y-%m-%d"), int(parsed.timestamp())
        except ValueError:
            return date, 0
    return "", 0


def extract_sogou_date_from_block(block: str) -> tuple[str, int]:
    match = re.search(r"timeConvert\(['\"]?(\d{9,11})['\"]?\)", block or "", re.I)
    if match:
        return timestamp_to_date(match.group(1))
    clean_block = clean_text(block)
    match = re.search(r"\b(20\d{2}[-/.年]\d{1,2}[-/.月]\d{1,2})", clean_block)
    if match:
        date = match.group(1).replace("年", "-").replace("月", "-").replace("/", "-").replace(".", "-").strip("-")
        try:
            parsed = datetime.strptime(date, "%Y-%m-%d")
            return parsed.strftime("%Y-%m-%d"), int(parsed.timestamp())
        except ValueError:
            return date, 0
    return "", 0


def parse_sogou_weixin(html_text: str, keyword: str, start_rank: int = 0) -> list[WechatSearchResult]:
    blocks = re.findall(r"<li[^>]+id=[\"']sogou_vr_11002601_box_\d+[\"'][\s\S]*?</li>", html_text or "", re.I)
    if not blocks:
        soup = BeautifulSoup(html_text or "", "html.parser")
        blocks = [str(item) for item in soup.select("ul.news-list li")]

    results: list[WechatSearchResult] = []
    for index, block in enumerate(blocks, start=1):
        title_match = re.search(
            r"<a[^>]+id=[\"']sogou_vr_11002601_title_\d+[\"'][^>]*href=[\"']([^\"']+)[\"'][^>]*>([\s\S]*?)</a>",
            block,
            re.I,
        )
        fallback_title_match = re.search(r"<h3>\s*<a[^>]*href=[\"']([^\"']+)[\"'][^>]*>([\s\S]*?)</a>", block, re.I)
        match = title_match or fallback_title_match
        if not match:
            continue
        raw_href, raw_title = match.group(1), match.group(2)
        title = normalize_space(clean_text(raw_title))
        if not title or not raw_href:
            continue

        raw_summary = ""
        summary_match = re.search(r"<p[^>]+class=[\"']txt-info[\"'][^>]*>([\s\S]*?)</p>", block, re.I)
        if summary_match:
            raw_summary = summary_match.group(1)
        account_match = re.search(r"<span[^>]+class=[\"']all-time-y2[\"'][^>]*>([\s\S]*?)</span>", block, re.I)
        account = normalize_space(clean_text(account_match.group(1) if account_match else ""))
        published_at, published_ts = extract_sogou_date_from_block(block)
        snippet = normalize_space(clean_text(raw_summary))
        relevance = wechat_relevance_score(title, snippet, account, keyword)
        if relevance >= 60:
            status = "微信强相关候选"
        elif relevance >= 36:
            status = "微信补充候选"
        elif relevance >= 20:
            status = "微信弱相关候选"
        else:
            status = "弱相关候选"

        results.append(
            WechatSearchResult(
                title=title,
                search_url=normalize_sogou_url(raw_href),
                url="",
                account=account,
                published_at=published_at,
                published_ts=published_ts,
                snippet=snippet,
                rank=start_rank + index,
                status=status,
                relevance=relevance,
            )
        )
    return results


def parse_feed_date(value: str) -> tuple[str, int]:
    if not value:
        return "", 0
    try:
        parsed = parsedate_to_datetime(value)
        return parsed.strftime("%Y-%m-%d"), int(parsed.timestamp())
    except Exception:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed.strftime("%Y-%m-%d"), int(parsed.timestamp())
        except Exception:
            return "", 0


def parse_wechat_rss_feed(xml_text: str, feed_url: str, keyword: str, start_rank: int = 0) -> list[WechatSearchResult]:
    try:
        root = ElementTree.fromstring(xml_text)
    except Exception:
        return []

    def local_name(tag: str) -> str:
        return tag.rsplit("}", 1)[-1].lower()

    items = [node for node in root.iter() if local_name(node.tag) in {"item", "entry"}]
    results: list[WechatSearchResult] = []
    for index, item in enumerate(items[:30], start=1):
        values: dict[str, str] = {}
        for child in list(item):
            name = local_name(child.tag)
            if name == "link":
                values.setdefault("link", child.attrib.get("href") or (child.text or ""))
            elif name in {"title", "description", "summary", "content", "pubdate", "published", "updated", "source"}:
                values.setdefault(name, child.text or "")
        title = normalize_space(clean_text(values.get("title", "")))
        url = normalize_sogou_url(values.get("link", ""))
        snippet = normalize_space(clean_text(values.get("description") or values.get("summary") or values.get("content") or ""))
        if not title or not url:
            continue
        published_at, published_ts = parse_feed_date(values.get("pubdate") or values.get("published") or values.get("updated") or "")
        account = normalize_space(clean_text(values.get("source", ""))) or urlparse(feed_url).netloc.lower().removeprefix("www.")
        relevance = wechat_relevance_score(title, snippet, account, keyword) + 8
        if relevance < 20:
            continue
        results.append(
            WechatSearchResult(
                title=title,
                search_url=url,
                url=url,
                account=account,
                published_at=published_at,
                published_ts=published_ts,
                snippet=snippet,
                rank=start_rank + index,
                status="白名单RSS候选",
                relevance=relevance,
            )
        )
    return results


def fetch_configured_wechat_feeds(keyword: str, limit: int = 10, start_rank: int = 0) -> list[WechatSearchResult]:
    feeds = configured_wechat_feeds()
    if not feeds:
        return []
    session = make_sogou_session()
    results: list[WechatSearchResult] = []
    rank = start_rank
    for feed_url in feeds:
        try:
            response = request_with_backoff(session, feed_url, timeout=10, allow_redirects=True, max_retries=2, base_delay=1.5)
            response.raise_for_status()
            items = parse_wechat_rss_feed(response.text, feed_url, keyword, start_rank=rank)
        except Exception:
            continue
        for item in items:
            rank += 1
            item.rank = rank
            results.append(item)
            if len(results) >= limit:
                return results
    return results


def restore_sogou_redirect_url(script_html: str) -> str:
    parts = re.findall(r"url\s*\+=\s*'([^']*)';", script_html or "")
    if parts:
        url = "".join(parts).replace("&amp;", "&")
        url = url.replace("¡Átamp=", "&timestamp=").replace("×tamp=", "&timestamp=")
        return url
    match = re.search(r"https://mp\.weixin\.qq\.com/s\?[^'\"<> ]+", script_html or "")
    if not match:
        return ""
    return match.group(0).replace("&amp;", "&").replace("¡Átamp=", "&timestamp=").replace("×tamp=", "&timestamp=")


def make_sogou_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(DEFAULT_HEADERS)
    return session


def parse_browser_headers(raw: str) -> dict[str, str]:
    text = (raw or "").strip()
    if not text:
        return {}
    headers: dict[str, str] = {}
    header_lines: list[str] = []
    header_lines.extend(re.findall(r"-H\s+['\"]([^'\"]+?:\s*[^'\"]+)['\"]", text))
    header_lines.extend(re.findall(r"--header\s+['\"]([^'\"]+?:\s*[^'\"]+)['\"]", text))
    header_lines.extend(text.splitlines())

    for raw_line in header_lines:
        line = raw_line.strip().strip("'\"")
        if not line:
            continue
        if line.lower().startswith("-h "):
            line = line[3:].strip().strip("'\"")
        if line.lower().startswith("--header "):
            line = line[9:].strip().strip("'\"")
        if ":" in line:
            name, value = line.split(":", 1)
            canonical = HEADER_ALLOWLIST.get(name.strip().lower())
            if canonical and value.strip():
                headers[canonical] = value.strip()

    if not headers and "=" in text:
        cookie = text
        if cookie.lower().startswith("cookie:"):
            cookie = cookie.split(":", 1)[1]
        headers["Cookie"] = cookie.strip()
    return headers


def make_browser_state_session(browser_state: str = "") -> requests.Session:
    session = make_sogou_session()
    headers = parse_browser_headers(browser_state)
    if headers:
        session.headers.update(headers)
    return session


def unwrap_search_url(url: str) -> str:
    if not url:
        return ""
    parsed = urlparse(url)
    if "duckduckgo.com" in parsed.netloc and parsed.query:
        match = re.search(r"[?&]uddg=([^&]+)", url)
        if match:
            from urllib.parse import unquote

            return unquote(match.group(1))
    return url


def search_duckduckgo_html(query: str, limit: int = 8) -> list[dict[str, str]]:
    session = make_sogou_session()
    response = request_with_backoff(
        session,
        "https://duckduckgo.com/html/",
        params={"q": query},
        timeout=10,
        allow_redirects=True,
        max_retries=2,
        base_delay=2.0,
    )
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")
    items: list[dict[str, str]] = []
    for result in soup.select(".result")[:limit]:
        link = result.select_one("a.result__a")
        snippet = result.select_one(".result__snippet")
        if not link:
            continue
        items.append({
            "title": normalize_space(link.get_text(" ", strip=True)),
            "url": unwrap_search_url(link.get("href", "")),
            "snippet": normalize_space(snippet.get_text(" ", strip=True) if snippet else ""),
        })
    return items


def search_bing_html(query: str, limit: int = 8) -> list[dict[str, str]]:
    session = make_sogou_session()
    response = request_with_backoff(
        session,
        "https://www.bing.com/search",
        params={"q": query},
        timeout=10,
        allow_redirects=True,
        max_retries=2,
        base_delay=2.0,
    )
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")
    items: list[dict[str, str]] = []
    for result in soup.select("li.b_algo")[:limit]:
        link = result.select_one("h2 a") or result.select_one("a")
        snippet = result.select_one(".b_caption p") or result.select_one("p")
        if not link:
            continue
        items.append({
            "title": normalize_space(link.get_text(" ", strip=True)),
            "url": unwrap_search_url(link.get("href", "")),
            "snippet": normalize_space(snippet.get_text(" ", strip=True) if snippet else ""),
        })
    return items


def search_wechat_redundant_channels(keyword: str, limit: int = 10, cache_ttl_days: int = 1) -> list[WechatSearchResult]:
    cache_file = cache_path(SEARCH_CACHE_DIR, cache_key(f"redundant|{keyword}|{limit}"))
    cached = read_cache(cache_file, cache_ttl_days)
    if cached:
        return [WechatSearchResult(**item) for item in cached]

    queries = [
        f'{keyword} 微信公众号 转载 OR 原文',
        f'{keyword} 公众号 调研纪要 OR 专家纪要',
        f'{keyword} site:mp.weixin.qq.com',
        f'{keyword} 百家号 知乎 头条 转载',
        f'{keyword} 券商研报 基金公司 观点',
    ]
    results: list[WechatSearchResult] = []
    seen: set[str] = set()
    rank = 0
    for query in queries:
        search_items: list[dict[str, str]] = []
        for searcher in (search_duckduckgo_html, search_bing_html):
            try:
                search_items.extend(searcher(query, limit=6))
            except Exception:
                continue
            time.sleep(1.0)
        for item in search_items:
            url = item.get("url", "").strip()
            title = item.get("title", "").strip()
            if not url.startswith(("http://", "https://")) or not title:
                continue
            dedupe = url.split("#", 1)[0]
            if dedupe in seen:
                continue
            seen.add(dedupe)
            rank += 1
            host = urlparse(url).netloc.lower().removeprefix("www.")
            status = "冗余候选"
            if "mp.weixin.qq.com" in host:
                status = "微信原文候选"
            elif any(domain in host for domain in ["baijiahao.baidu.com", "zhihu.com", "toutiao.com"]):
                status = "转载候选"
            elif any(token in title for token in ["研报", "纪要", "调研", "基金", "券商"]):
                status = "机构/纪要候选"
            results.append(
                WechatSearchResult(
                    title=title,
                    search_url=url,
                    url=url,
                    account=host,
                    published_at="",
                    published_ts=0,
                    snippet=item.get("snippet", ""),
                    rank=rank,
                    status=status,
                )
            )
            if len(results) >= limit:
                write_cache(cache_file, [asdict(result) for result in results])
                return results
    write_cache(cache_file, [asdict(result) for result in results])
    return results


def resolve_sogou_link(session: requests.Session, url: str, timeout: int = 15) -> str:
    if not url:
        return ""
    absolute = urljoin("https://weixin.sogou.com", url)
    if "mp.weixin.qq.com" in urlparse(absolute).netloc:
        return absolute
    response = request_with_backoff(session, absolute, timeout=timeout, allow_redirects=True, max_retries=3, base_delay=2.0)
    response.raise_for_status()
    if "antispider" in response.url or "请输入验证码" in response.text or "verify_page" in response.text:
        raise RuntimeError("搜狗微信跳转页触发验证码/反爬验证，无法自动解析真实微信链接。请稍后重试，或手动打开候选文章后复制 mp.weixin.qq.com 链接入库。")
    if "mp.weixin.qq.com" in urlparse(response.url).netloc:
        return response.url
    resolved = restore_sogou_redirect_url(response.text)
    if not resolved:
        raise RuntimeError("未能从搜狗跳转页解析真实微信文章链接。")
    return resolved


def search_sogou_wechat(
    keyword: str,
    limit: int = 10,
    pages: int = 3,
    cache_ttl_days: int = 1,
    min_delay_seconds: float = 2.0,
) -> list[WechatSearchResult]:
    queries = build_sogou_weixin_queries(keyword)
    cache_file = cache_path(SEARCH_CACHE_DIR, cache_key(f"sogou-weixin-v2|{keyword}|{limit}|{pages}|{'|'.join(queries)}"))
    cached = read_cache(cache_file, cache_ttl_days)
    if cached:
        return [WechatSearchResult(**item) for item in cached]

    session = make_sogou_session()
    results: list[WechatSearchResult] = []
    seen: set[str] = set()
    rank = 0

    for query in queries:
        for page in range(1, max(1, pages) + 1):
            response = request_with_backoff(
                session,
                SOGOU_WECHAT_URL,
                params={
                    "type": "2",
                    "query": query,
                    "ie": "utf8",
                    "s_from": "input",
                    "_sug_": "n",
                    "_sug_type_": "",
                    "page": str(page),
                },
                timeout=12,
                allow_redirects=True,
                max_retries=3,
                base_delay=max(2.0, min_delay_seconds),
            )
            response.raise_for_status()
            if "请输入验证码" in response.text or "antispider" in response.url or "antispider" in response.text:
                raise RuntimeError("搜狗微信搜索触发验证码/反爬验证，请稍后重试或减少搜索频率。")
            parsed_items = parse_sogou_weixin(response.text, keyword=query, start_rank=rank)
            if not parsed_items:
                break
            for item in parsed_items:
                dedupe_key = normalize_space(item.search_url or item.url or item.title).lower()
                title_key = normalize_space(item.title).lower()
                if not dedupe_key or dedupe_key in seen or title_key in seen:
                    continue
                seen.add(dedupe_key)
                seen.add(title_key)
                rank += 1
                item.rank = rank
                if item.relevance < 20:
                    item.status = "弱相关降权"
                results.append(item)
            if len(results) >= limit * 2:
                break
            time.sleep(max(1.0, min_delay_seconds))
        if len(results) >= limit * 2:
            break

    for item in fetch_configured_wechat_feeds(keyword, limit=limit, start_rank=rank):
        dedupe_key = normalize_space(item.search_url or item.url or item.title).lower()
        title_key = normalize_space(item.title).lower()
        if dedupe_key and dedupe_key not in seen and title_key not in seen:
            seen.add(dedupe_key)
            seen.add(title_key)
            results.append(item)

    results = [item for item in results if item.relevance >= 20]
    results = sorted(
        results,
        key=lambda item: (wechat_relevance_band(item.relevance), item.published_ts, item.relevance, -item.rank),
        reverse=True,
    )
    results = results[:limit]
    write_cache(cache_file, [asdict(item) for item in results])
    return results


def resolve_sogou_search_url(search_url: str) -> str:
    session = make_sogou_session()
    return resolve_sogou_link(session, search_url)


def resolve_sogou_search_url_with_browser_state(search_url: str, browser_state: str = "") -> str:
    session = make_browser_state_session(browser_state)
    return resolve_sogou_link(session, search_url)


def fetch_wechat_article(
    url: str,
    title_hint: str = "",
    account_hint: str = "",
    date_hint: str = "",
    browser_state: str = "",
) -> WechatArticle:
    cache_file = cache_path(ARTICLE_CACHE_DIR, cache_key(url))
    cached = read_cache(cache_file, ttl_days=7)
    if cached:
        return WechatArticle(**cached)

    session = make_browser_state_session(browser_state)
    response = request_with_backoff(session, url, timeout=18, allow_redirects=True, max_retries=3, base_delay=2.0)
    response.raise_for_status()
    if "环境异常" in response.text or "访问过于频繁" in response.text or "验证码" in response.text:
        raise RuntimeError("微信文章页返回环境/频率验证，无法公开抓取正文。")
    soup = BeautifulSoup(response.text, "html.parser")
    for tag in soup(["script", "style", "noscript", "iframe", "svg"]):
        tag.decompose()
    title_node = soup.select_one("#activity-name")
    account_node = soup.select_one("#js_name")
    date_node = soup.select_one("#publish_time")
    content_node = soup.select_one("#js_content") or soup.select_one(".rich_media_content")
    content = clean_text(content_node.get_text("\n", strip=True) if content_node else "")
    images = extract_wechat_images_from_soup(content_node)
    if len(content) < 120:
        raise RuntimeError("未能抽取到足够长的公众号正文，可能是链接过期、需要校验或页面结构变化。")
    article = WechatArticle(
        title=normalize_space(title_node.get_text(" ", strip=True) if title_node else title_hint),
        url=url,
        account=normalize_space(account_node.get_text(" ", strip=True) if account_node else account_hint),
        published_at=normalize_space(date_node.get_text(" ", strip=True) if date_node else date_hint),
        content=content,
        images=images,
        html_title=normalize_space(soup.title.get_text(" ", strip=True) if soup.title else ""),
    )
    write_cache(cache_file, asdict(article))
    return article


def fetch_wechat_article_with_browser_state(
    url: str,
    browser_state: str = "",
    title_hint: str = "",
    account_hint: str = "",
    date_hint: str = "",
) -> WechatArticle:
    return fetch_wechat_article(
        url,
        title_hint=title_hint,
        account_hint=account_hint,
        date_hint=date_hint,
        browser_state=browser_state,
    )


def fetch_public_article(url: str, title_hint: str = "", source_hint: str = "") -> WechatArticle:
    cache_file = cache_path(ARTICLE_CACHE_DIR, cache_key(f"public|{url}"))
    cached = read_cache(cache_file, ttl_days=7)
    if cached:
        return WechatArticle(**cached)
    session = make_sogou_session()
    response = request_with_backoff(session, url, timeout=15, allow_redirects=True, max_retries=2, base_delay=2.0)
    response.raise_for_status()
    content_type = response.headers.get("content-type", "")
    if "text/html" not in content_type and "text/plain" not in content_type:
        raise RuntimeError(f"转载页不是可抽取文本页面：{content_type or 'unknown content-type'}")
    if not response.encoding or response.encoding.lower() in {"iso-8859-1", "ascii"}:
        response.encoding = response.apparent_encoding or "utf-8"
    soup = BeautifulSoup(response.text, "html.parser")
    for tag in soup(["script", "style", "noscript", "iframe", "svg", "nav", "footer", "header", "aside", "form"]):
        tag.decompose()
    title_node = soup.select_one("h1") or soup.select_one("title")
    content_node = None
    for selector in [
        "article",
        "main",
        ".article-content",
        ".article__content",
        ".article-body",
        ".post-content",
        ".entry-content",
        ".content",
        ".main-content",
        "body",
    ]:
        node = soup.select_one(selector)
        if node and len(clean_text(node.get_text("\n", strip=True))) >= 120:
            content_node = node
            break
    content = clean_text(content_node.get_text("\n", strip=True) if content_node else "")
    if len(content) < 300:
        raise RuntimeError("未能从转载/备用页面抽取足够正文。")
    images = extract_wechat_images_from_soup(content_node)
    article = WechatArticle(
        title=normalize_space(title_node.get_text(" ", strip=True) if title_node else title_hint),
        url=url,
        account=source_hint or urlparse(url).netloc.lower().removeprefix("www."),
        published_at="",
        content=content,
        images=images,
        html_title=normalize_space(soup.title.get_text(" ", strip=True) if soup.title else ""),
    )
    write_cache(cache_file, asdict(article))
    return article


def extract_wechat_images_from_soup(content_node: Any) -> list[dict[str, str]]:
    if content_node is None:
        return []
    images: list[dict[str, str]] = []
    for idx, image in enumerate(content_node.select("img"), start=1):
        src = image.get("data-src") or image.get("src") or image.get("data-original") or ""
        if not src:
            continue
        parent_text = normalize_space((image.find_parent(["p", "section", "figure"]) or image.parent).get_text(" ", strip=True) if image.parent else "")
        images.append({
            "index": str(idx),
            "src": src,
            "alt": normalize_space(image.get("alt") or image.get("data-type") or ""),
            "nearby": parent_text[:240],
        })
    return images[:80]


def image_catalog_markdown(images: list[dict[str, Any]]) -> str:
    if not images:
        return ""
    lines = [
        "",
        "## 图片与图表线索",
        "",
        "以下图片 URL 来自公众号正文页面，仅作为图表/配图线索保存；模型使用图片中的数字或图表结论前，仍需人工核验图片内容或另找文字来源。",
        "",
    ]
    for item in images[:80]:
        src = str(item.get("src") or "").strip()
        if not src:
            continue
        index = item.get("index") or len(lines)
        alt = normalize_space(str(item.get("alt") or ""))
        nearby = normalize_space(str(item.get("nearby") or ""))
        lines.append(f"- 图{index}：{src}")
        if alt:
            lines.append(f"  - alt：{alt}")
        if nearby:
            lines.append(f"  - 附近文字：{nearby}")
        lines.append(f"  - 预览：![图{index}]({src})")
    return "\n".join(lines)


def article_to_markdown(article: WechatArticle, keyword: str, search_rank: int = 0) -> str:
    return "\n".join(
        [
            f"# {article.title or '微信公众号文章'}",
            "",
            "## 元数据",
            f"- 原文链接：{article.url}",
            f"- 公众号/作者：{article.account or '未识别'}",
            f"- 发布日期：{article.published_at or '未识别'}",
            f"- 搜索关键词：{keyword}",
            f"- 搜索排序：{search_rank or '未记录'}",
            "- 来源说明：由 IndustryScope 通过搜狗微信搜索发现，并从公开可访问的微信文章页面抽取正文。公众号内容质量参差不齐，强结论需与一手公开来源交叉验证。",
            "",
            "## 正文",
            "",
            article.content,
            image_catalog_markdown(article.images),
            "",
        ]
    )


def candidate_to_markdown(candidate: dict[str, Any], keyword: str, error: str = "") -> str:
    title = candidate.get("title") or "微信公众号候选文章"
    return "\n".join(
        [
            f"# {title}",
            "",
            "## 元数据",
            f"- 搜狗候选链接：{candidate.get('search_url') or '未记录'}",
            f"- 真实微信链接：{candidate.get('url') or '未解析'}",
            f"- 公众号/作者：{candidate.get('account') or '未识别'}",
            f"- 发布日期：{candidate.get('published_at') or '未识别'}",
            f"- 搜索关键词：{keyword}",
            f"- 搜索排序：{candidate.get('rank') or '未记录'}",
            f"- 相关性分数：{candidate.get('relevance') if candidate.get('relevance') not in [None, ''] else '未记录'}",
            f"- 候选状态：{candidate.get('status') or '候选'}",
            "- 入库状态：候选线索，未能自动抓取全文",
            f"- 抓取失败原因：{error or candidate.get('error') or '未记录'}",
            "- 来源说明：由 IndustryScope 通过搜狗微信搜索发现。该文档只保存标题、摘要和候选链接，作为待补全文线索；不得单独支撑市场规模、份额、融资、订单、财务或全球第一等强结论。",
            "",
            "## 摘要",
            "",
            candidate.get("snippet") or "无摘要。",
            "",
            "## 后续处理",
            "",
            "如需全文证据，请手动打开搜狗候选链接或微信文章，通过验证码后复制标题、原文链接和正文到“手动粘贴公众号全文入库”入口。只粘贴链接仍可能被微信环境校验拦截。",
            "",
        ]
    )


def ingest_wechat_candidate_stub(
    candidate: dict[str, Any],
    keyword: str,
    industry_tags: str = "",
    company_tags: str = "",
    technology_tags: str = "",
    error: str = "",
) -> dict[str, Any]:
    markdown_text = candidate_to_markdown(candidate, keyword, error=error)
    title = f"{candidate.get('title') or '微信公众号候选文章'}（候选线索）"
    safe_name = filename_safe(title)[:90]
    with tempfile.NamedTemporaryFile(delete=False, suffix=".md", mode="w", encoding="utf-8") as tmp:
        tmp.write(markdown_text)
        tmp_path = Path(tmp.name)
    try:
        doc = add_document(
            tmp_path,
            title=title,
            source_type=WECHAT_CANDIDATE_SOURCE_TYPE,
            source_org=candidate.get("account", ""),
            publish_date=candidate.get("published_at", ""),
            industry_tags=industry_tags or keyword,
            company_tags=company_tags,
            technology_tags=technology_tags,
            source_url=candidate.get("url") or candidate.get("search_url", ""),
        )
        return {"document": asdict(doc), "markdown": markdown_text}
    finally:
        tmp_path.unlink(missing_ok=True)


def ingest_wechat_fulltext(
    title: str,
    content: str,
    url: str = "",
    account: str = "",
    published_at: str = "",
    keyword: str = "",
    industry_tags: str = "",
    company_tags: str = "",
    technology_tags: str = "",
    images: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    cleaned_content = clean_text(content)
    if len(cleaned_content) < 120:
        raise RuntimeError("粘贴正文太短，无法作为全文证据入库。请至少粘贴正文主体内容。")
    article = WechatArticle(
        title=normalize_space(title) or "手动粘贴公众号全文",
        url=normalize_space(url),
        account=normalize_space(account),
        published_at=normalize_space(published_at),
        content=cleaned_content,
        images=images or [],
    )
    return ingest_wechat_article(
        article,
        keyword=keyword or title,
        industry_tags=industry_tags or keyword or title,
        company_tags=company_tags,
        technology_tags=technology_tags,
        search_rank=0,
    )


def parse_wechat_clip_payload(payload: str) -> dict[str, Any]:
    text = (payload or "").strip()
    if not text:
        raise RuntimeError("请先粘贴公众号采集 JSON。")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RuntimeError("采集内容不是有效 JSON。请确认使用的是 IndustryScope 公众号采集书签，并完整粘贴剪贴板内容。") from exc
    if not isinstance(data, dict):
        raise RuntimeError("采集 JSON 格式不正确。")
    content = clean_text(str(data.get("content") or ""))
    if len(content) < 120:
        raise RuntimeError("采集到的正文太短，可能还停留在验证页或未打开正文页。请通过验证后在文章正文页再次点击采集书签。")
    images = data.get("images") or []
    if not isinstance(images, list):
        images = []
    clean_images: list[dict[str, str]] = []
    for idx, item in enumerate(images[:80], start=1):
        if not isinstance(item, dict):
            continue
        src = str(item.get("src") or "").strip()
        if not src:
            continue
        clean_images.append({
            "index": str(item.get("index") or idx),
            "src": src,
            "alt": normalize_space(str(item.get("alt") or "")),
            "nearby": normalize_space(str(item.get("nearby") or ""))[:240],
        })
    return {
        "title": normalize_space(str(data.get("title") or "")),
        "url": normalize_space(str(data.get("url") or "")),
        "account": normalize_space(str(data.get("account") or "")),
        "published_at": normalize_space(str(data.get("published_at") or "")),
        "content": content,
        "images": clean_images,
        "captured_at": normalize_space(str(data.get("captured_at") or "")),
    }


def ingest_wechat_clip_payload(
    payload: str,
    keyword: str = "",
    industry_tags: str = "",
    company_tags: str = "",
    technology_tags: str = "",
) -> dict[str, Any]:
    data = parse_wechat_clip_payload(payload)
    result = ingest_wechat_fulltext(
        title=data.get("title") or "公众号采集正文",
        content=data.get("content", ""),
        url=data.get("url", ""),
        account=data.get("account", ""),
        published_at=data.get("published_at", ""),
        keyword=keyword or data.get("title", ""),
        industry_tags=industry_tags or keyword or data.get("title", ""),
        company_tags=company_tags,
        technology_tags=technology_tags,
        images=data.get("images", []),
    )
    result["clip"] = data
    return result


def ingest_wechat_article(
    article: WechatArticle,
    keyword: str,
    industry_tags: str = "",
    company_tags: str = "",
    technology_tags: str = "",
    search_rank: int = 0,
) -> dict[str, Any]:
    markdown_text = article_to_markdown(article, keyword, search_rank=search_rank)
    safe_name = filename_safe(article.title or f"wechat_{search_rank}")[:90]
    with tempfile.NamedTemporaryFile(delete=False, suffix=".md", mode="w", encoding="utf-8") as tmp:
        tmp.write(markdown_text)
        tmp_path = Path(tmp.name)
    try:
        doc = add_document(
            tmp_path,
            title=article.title or safe_name,
            source_type="公众号/媒体转载",
            source_org=article.account,
            publish_date=article.published_at,
            industry_tags=industry_tags or keyword,
            company_tags=company_tags,
            technology_tags=technology_tags,
            source_url=article.url,
        )
        return {"document": asdict(doc), "markdown": markdown_text}
    finally:
        tmp_path.unlink(missing_ok=True)
