from __future__ import annotations

import html
import json
import re
import tempfile
import time
from dataclasses import dataclass, asdict, field
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from knowledge_base import WECHAT_CANDIDATE_SOURCE_TYPE, add_document, filename_safe


SOGOU_WECHAT_URL = "https://weixin.sogou.com/weixin"
WECHAT_COLLECTOR_BOOKMARKLET = '''javascript:(async()=>{const T=s=>(s||"").replace(/\\s+/g," ").trim(),Q=s=>document.querySelector(s),C=Q("#js_content")||Q(".rich_media_content")||document.body,I=[...C.querySelectorAll("img")].map((m,i)=>({index:i+1,src:m.getAttribute("data-src")||m.currentSrc||m.src||"",alt:T(m.alt||m.getAttribute("data-type")||""),nearby:T((m.closest("p,section,figure")||m.parentElement||{}).innerText||"").slice(0,240)})).filter(x=>x.src),P={source:"IndustryScopeWeChatClip",version:1,captured_at:new Date().toISOString(),title:T((Q("#activity-name")||Q("h1")||{}).innerText),url:location.href,account:T((Q("#js_name")||Q(".profile_nickname")||{}).innerText),published_at:T((Q("#publish_time")||{}).innerText),content:(C.innerText||"").trim(),images:I};const S=JSON.stringify(P);try{await navigator.clipboard.writeText(S)}catch(e){const a=document.createElement("textarea");a.value=S;document.body.appendChild(a);a.select();document.execCommand("copy");a.remove()}alert("已复制到剪贴板：回到 IndustryScope 粘贴并入库。图片链接 "+I.length+" 个。");try{window.opener&&window.opener.focus();setTimeout(()=>window.close(),300)}catch(e){}})()'''
DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,text/plain;q=0.8,*/*;q=0.7",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Referer": "https://weixin.sogou.com/",
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


def clean_text(text: str) -> str:
    text = html.unescape(text or "")
    text = text.replace("\xa0", " ")
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


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


def resolve_sogou_link(session: requests.Session, url: str, timeout: int = 15) -> str:
    if not url:
        return ""
    absolute = urljoin("https://weixin.sogou.com", url)
    if "mp.weixin.qq.com" in urlparse(absolute).netloc:
        return absolute
    response = session.get(absolute, timeout=timeout, allow_redirects=True)
    response.raise_for_status()
    if "antispider" in response.url or "请输入验证码" in response.text or "verify_page" in response.text:
        raise RuntimeError("搜狗微信跳转页触发验证码/反爬验证，无法自动解析真实微信链接。请稍后重试，或手动打开候选文章后复制 mp.weixin.qq.com 链接入库。")
    if "mp.weixin.qq.com" in urlparse(response.url).netloc:
        return response.url
    resolved = restore_sogou_redirect_url(response.text)
    if not resolved:
        raise RuntimeError("未能从搜狗跳转页解析真实微信文章链接。")
    return resolved


def search_sogou_wechat(keyword: str, limit: int = 10, pages: int = 3) -> list[WechatSearchResult]:
    session = make_sogou_session()
    results: list[WechatSearchResult] = []
    seen: set[str] = set()
    rank = 0

    for page in range(1, max(1, pages) + 1):
        response = session.get(
            SOGOU_WECHAT_URL,
            params={
                "type": "2",
                "query": keyword,
                "ie": "utf8",
                "s_from": "input",
                "_sug_": "n",
                "_sug_type_": "",
                "page": str(page),
            },
            timeout=12,
        )
        response.raise_for_status()
        if "请输入验证码" in response.text or "antispider" in response.url or "antispider" in response.text:
            raise RuntimeError("搜狗微信搜索触发验证码/反爬验证，请稍后重试或减少搜索频率。")
        soup = BeautifulSoup(response.text, "html.parser")
        items = soup.select("ul.news-list li")
        if not items:
            break
        for li in items:
            anchor = li.select_one("h3 a") or li.select_one("a")
            if not anchor:
                continue
            title = normalize_space(anchor.get_text(" ", strip=True))
            href = anchor.get("href", "")
            search_url = urljoin("https://weixin.sogou.com", href)
            snippet_node = li.select_one(".txt-info")
            snippet = normalize_space(snippet_node.get_text(" ", strip=True) if snippet_node else "")
            account_node = li.select_one(".all-time-y2") or li.select_one(".account")
            account = normalize_space(account_node.get_text(" ", strip=True) if account_node else "")
            published_at, published_ts = extract_sogou_date(li)
            dedupe_key = href or title
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            rank += 1
            results.append(
                WechatSearchResult(
                    title=title,
                    search_url=search_url,
                    url="",
                    account=account,
                    published_at=published_at,
                    published_ts=published_ts,
                    snippet=snippet,
                    rank=rank,
                )
            )
        if len(results) >= limit:
            break
        time.sleep(0.6)

    results = sorted(results, key=lambda item: (item.published_ts, -item.rank), reverse=True)
    return results[:limit]


def resolve_sogou_search_url(search_url: str) -> str:
    session = make_sogou_session()
    return resolve_sogou_link(session, search_url)


def fetch_wechat_article(url: str, title_hint: str = "", account_hint: str = "", date_hint: str = "") -> WechatArticle:
    session = requests.Session()
    session.headers.update(DEFAULT_HEADERS)
    response = session.get(url, timeout=18)
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
    return WechatArticle(
        title=normalize_space(title_node.get_text(" ", strip=True) if title_node else title_hint),
        url=url,
        account=normalize_space(account_node.get_text(" ", strip=True) if account_node else account_hint),
        published_at=normalize_space(date_node.get_text(" ", strip=True) if date_node else date_hint),
        content=content,
        images=images,
        html_title=normalize_space(soup.title.get_text(" ", strip=True) if soup.title else ""),
    )


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
