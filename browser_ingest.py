from __future__ import annotations

import json
import re
import time
from dataclasses import asdict
from typing import Any
from urllib.parse import unquote

import requests
from bs4 import BeautifulSoup

from wechat_ingest import WechatArticle, clean_text, ingest_wechat_article, normalize_space


DEFAULT_CDP_ENDPOINT = "http://127.0.0.1:9222"


CDP_EXTRACT_SCRIPT = r"""
(() => {
  const clean = (value) => (value || "").replace(/\s+/g, " ").trim();
  const pick = (selectors) => {
    for (const selector of selectors) {
      const node = document.querySelector(selector);
      if (node && clean(node.innerText || node.textContent)) return clean(node.innerText || node.textContent);
    }
    return "";
  };
  const contentNode = document.querySelector("#js_content") ||
    document.querySelector(".rich_media_content") ||
    document.querySelector("article") ||
    document.querySelector("main") ||
    document.body;
  const images = Array.from((contentNode || document).querySelectorAll("img")).map((image, index) => {
    const parent = image.closest("p,section,figure") || image.parentElement || {};
    return {
      index: String(index + 1),
      src: image.getAttribute("data-src") || image.currentSrc || image.src || "",
      alt: clean(image.alt || image.getAttribute("data-type") || ""),
      nearby: clean(parent.innerText || "").slice(0, 240),
    };
  }).filter((item) => item.src);
  return {
    title: pick(["#activity-name", "h1", "title"]) || clean(document.title),
    url: location.href,
    account: pick(["#js_name", ".profile_nickname", ".rich_media_meta_nickname"]),
    published_at: pick(["#publish_time", ".rich_media_meta_text"]),
    content: (contentNode && contentNode.innerText ? contentNode.innerText : document.body.innerText || "").trim(),
    html_title: clean(document.title),
    images,
  };
})()
"""


def normalize_cdp_endpoint(endpoint: str = "") -> str:
    endpoint = (endpoint or DEFAULT_CDP_ENDPOINT).strip().rstrip("/")
    if endpoint.startswith("ws://") or endpoint.startswith("wss://"):
        return endpoint
    if not endpoint.startswith(("http://", "https://")):
        endpoint = f"http://{endpoint}"
    return endpoint


def list_cdp_pages(endpoint: str = DEFAULT_CDP_ENDPOINT, timeout: int = 5) -> list[dict[str, Any]]:
    endpoint = normalize_cdp_endpoint(endpoint)
    if endpoint.startswith(("ws://", "wss://")):
        return [{"title": "Direct CDP WebSocket", "url": "", "webSocketDebuggerUrl": endpoint, "type": "page"}]
    response = requests.get(f"{endpoint}/json", timeout=timeout)
    response.raise_for_status()
    pages = response.json()
    if not isinstance(pages, list):
        raise RuntimeError("Chrome DevTools /json 返回格式不正确。")
    return [
        page
        for page in pages
        if isinstance(page, dict)
        and page.get("webSocketDebuggerUrl")
        and page.get("type", "page") in {"page", "webview"}
    ]


def select_cdp_page(
    pages: list[dict[str, Any]],
    *,
    url_hint: str = "",
    title_hint: str = "",
) -> dict[str, Any]:
    if not pages:
        raise RuntimeError("没有发现可读取的浏览器页面。请确认 Chrome 已用远程调试模式启动，且公众号文章页已打开。")

    url_hint_norm = normalize_space(url_hint).lower()
    title_hint_norm = normalize_space(title_hint).lower()
    mp_pages = [page for page in pages if "mp.weixin.qq.com" in str(page.get("url", "")).lower()]

    def score(page: dict[str, Any]) -> int:
        page_url = str(page.get("url", "")).lower()
        page_title = normalize_space(str(page.get("title", ""))).lower()
        value = 0
        if "mp.weixin.qq.com" in page_url:
            value += 100
        if url_hint_norm and (url_hint_norm in page_url or page_url in url_hint_norm):
            value += 80
        if title_hint_norm and (title_hint_norm in page_title or page_title in title_hint_norm):
            value += 60
        if page_url.startswith("http"):
            value += 10
        return value

    candidates = mp_pages or pages
    return max(candidates, key=score)


def cdp_call(ws, method: str, params: dict[str, Any] | None = None, call_id: int = 1) -> dict[str, Any]:
    ws.send(json.dumps({"id": call_id, "method": method, "params": params or {}}))
    deadline = time.time() + 15
    while time.time() < deadline:
        message = json.loads(ws.recv())
        if message.get("id") == call_id:
            if "error" in message:
                raise RuntimeError(message["error"].get("message", "CDP 调用失败。"))
            return message.get("result", {})
    raise RuntimeError(f"CDP 调用超时：{method}")


def extract_from_cdp_page(ws_url: str) -> dict[str, Any]:
    try:
        import websocket
    except ImportError as exc:
        raise RuntimeError("缺少 websocket-client 依赖。请先安装 requirements.txt，或在 Streamlit Cloud 重新部署。") from exc
    ws = websocket.create_connection(ws_url, timeout=15)
    try:
        cdp_call(ws, "Runtime.enable", call_id=1)
        result = cdp_call(
            ws,
            "Runtime.evaluate",
            {
                "expression": CDP_EXTRACT_SCRIPT,
                "awaitPromise": True,
                "returnByValue": True,
            },
            call_id=2,
        )
        value = result.get("result", {}).get("value")
        if not isinstance(value, dict):
            raise RuntimeError("浏览器页面没有返回可解析的正文对象。")
        return value
    finally:
        ws.close()


def article_from_browser_payload(payload: dict[str, Any], title_hint: str = "") -> WechatArticle:
    content = clean_text(str(payload.get("content") or ""))
    if len(content) < 120:
        raise RuntimeError("浏览器页面正文太短。请确认当前页已经通过验证，并停留在公众号正文页。")
    images = payload.get("images") or []
    if not isinstance(images, list):
        images = []
    clean_images: list[dict[str, str]] = []
    for idx, item in enumerate(images[:80], start=1):
        if not isinstance(item, dict):
            continue
        src = str(item.get("src") or "").strip()
        if not src:
            continue
        clean_images.append(
            {
                "index": str(item.get("index") or idx),
                "src": src,
                "alt": normalize_space(str(item.get("alt") or "")),
                "nearby": normalize_space(str(item.get("nearby") or ""))[:240],
            }
        )
    return WechatArticle(
        title=normalize_space(str(payload.get("title") or title_hint or "浏览器采集公众号正文")),
        url=normalize_space(str(payload.get("url") or "")),
        account=normalize_space(str(payload.get("account") or "")),
        published_at=normalize_space(str(payload.get("published_at") or "")),
        content=content,
        html_title=normalize_space(str(payload.get("html_title") or "")),
        images=clean_images,
    )


def fetch_wechat_article_from_cdp(
    endpoint: str = DEFAULT_CDP_ENDPOINT,
    *,
    url_hint: str = "",
    title_hint: str = "",
) -> WechatArticle:
    pages = list_cdp_pages(endpoint)
    page = select_cdp_page(pages, url_hint=url_hint, title_hint=title_hint)
    payload = extract_from_cdp_page(str(page["webSocketDebuggerUrl"]))
    return article_from_browser_payload(payload, title_hint=title_hint)


def ingest_current_browser_wechat_article(
    endpoint: str,
    *,
    keyword: str = "",
    industry_tags: str = "",
    company_tags: str = "",
    technology_tags: str = "",
    url_hint: str = "",
    title_hint: str = "",
    search_rank: int = 0,
) -> dict[str, Any]:
    article = fetch_wechat_article_from_cdp(endpoint, url_hint=url_hint, title_hint=title_hint)
    result = ingest_wechat_article(
        article,
        keyword=keyword or article.title,
        industry_tags=industry_tags or keyword or article.title,
        company_tags=company_tags,
        technology_tags=technology_tags,
        search_rank=search_rank,
    )
    result["article"] = asdict(article)
    return result


def html_to_wechat_article(html_text: str, url: str = "", title_hint: str = "") -> WechatArticle:
    soup = BeautifulSoup(html_text or "", "html.parser")
    for tag in soup(["script", "style", "noscript", "iframe", "svg"]):
        tag.decompose()
    title_node = soup.select_one("#activity-name") or soup.select_one("h1") or soup.select_one("title")
    account_node = soup.select_one("#js_name") or soup.select_one(".profile_nickname")
    date_node = soup.select_one("#publish_time")
    content_node = soup.select_one("#js_content") or soup.select_one(".rich_media_content") or soup.select_one("article") or soup.body
    content = clean_text(content_node.get_text("\n", strip=True) if content_node else "")
    if len(content) < 120:
        raise RuntimeError("HTML 正文太短，可能仍是验证页或不是公众号正文。")
    images: list[dict[str, str]] = []
    if content_node:
        for idx, image in enumerate(content_node.select("img"), start=1):
            src = image.get("data-src") or image.get("src") or image.get("data-original") or ""
            if not src:
                continue
            parent = image.find_parent(["p", "section", "figure"]) or image.parent
            nearby = normalize_space(parent.get_text(" ", strip=True) if parent else "")
            images.append(
                {
                    "index": str(idx),
                    "src": src,
                    "alt": normalize_space(image.get("alt") or image.get("data-type") or ""),
                    "nearby": nearby[:240],
                }
            )
    return WechatArticle(
        title=normalize_space(title_node.get_text(" ", strip=True) if title_node else title_hint),
        url=url,
        account=normalize_space(account_node.get_text(" ", strip=True) if account_node else ""),
        published_at=normalize_space(date_node.get_text(" ", strip=True) if date_node else ""),
        content=content,
        images=images[:80],
        html_title=normalize_space(soup.title.get_text(" ", strip=True) if soup.title else ""),
    )


def data_url_to_wechat_article(data_url: str, title_hint: str = "") -> WechatArticle:
    match = re.match(r"data:text/html[^,]*,(.*)$", data_url or "", flags=re.IGNORECASE | re.DOTALL)
    if not match:
        raise RuntimeError("请粘贴 data:text/html 开头的页面源码 data URL。")
    raw = match.group(1)
    html_text = unquote(raw)
    return html_to_wechat_article(html_text, title_hint=title_hint)
