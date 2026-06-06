from __future__ import annotations

import os
from datetime import datetime

import streamlit as st

from report_engine import (
    ReportRequest,
    build_prompt,
    call_model,
    ensure_clickable_source_section,
    filename_safe,
    get_provider_api_key,
    render_report_html,
    report_quality,
    request_to_json,
    sample_report,
)
from openai import APITimeoutError


st.set_page_config(
    page_title="IndustryScope 行业深研生成器",
    layout="wide",
    initial_sidebar_state="expanded",
)


st.markdown(
    """
    <style>
      .block-container { padding-top: 1.5rem; max-width: 1280px; }
      div[data-testid="stMetric"] {
        background: #ffffff;
        border: 1px solid #dfe5dc;
        padding: 12px 14px;
      }
      .stDownloadButton button, .stButton button {
        border-radius: 6px;
        font-weight: 650;
      }
      textarea { font-family: ui-monospace, SFMono-Regular, Consolas, monospace; }
    </style>
    """,
    unsafe_allow_html=True,
)


PROVIDER_PRESETS = {
    "OpenAI Responses": {
        "model": st.secrets.get("OPENAI_MODEL", os.getenv("OPENAI_MODEL", "gpt-5.1")),
        "base_url": "",
        "env": "OPENAI_API_KEY",
        "web": "OpenAI hosted web_search",
    },
    "DeepSeek": {
        "model": st.secrets.get("DEEPSEEK_MODEL", os.getenv("DEEPSEEK_MODEL", "deepseek-v4-flash")),
        "base_url": st.secrets.get("DEEPSEEK_BASE_URL", os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")),
        "env": "DEEPSEEK_API_KEY",
        "web": "本地网页检索 + 来源上下文",
    },
    "OpenAI兼容": {
        "model": st.secrets.get("OPENAI_COMPAT_MODEL", os.getenv("OPENAI_COMPAT_MODEL", "gpt-oss-120b")),
        "base_url": st.secrets.get("OPENAI_COMPAT_BASE_URL", os.getenv("OPENAI_COMPAT_BASE_URL", "")),
        "env": "OPENAI_COMPAT_API_KEY",
        "web": "本地网页检索 + 来源上下文",
    },
    "Anthropic": {
        "model": st.secrets.get("QWEAPI_MODEL", os.getenv("QWEAPI_MODEL", "claude-opus-4-8")),
        "base_url": st.secrets.get("QWEAPI_BASE_URL", os.getenv("QWEAPI_BASE_URL", "https://qweapi.com")),
        "env": "QWEAPI_AUTH_TOKEN / ANTHROPIC_AUTH_TOKEN",
        "web": "本地网页检索 + qweapi Claude/GPT 自动路由",
    },
}


MODEL_CHOICES = {
    "Anthropic": [
        "claude-opus-4-8",
        "claude-opus-4-8[1M]",
        "gpt-5.5",
        "自定义",
    ]
}


def default_model_for(provider: str, depth: str) -> str:
    if provider == "DeepSeek":
        if depth == "深度版":
            return st.secrets.get("DEEPSEEK_PRO_MODEL", os.getenv("DEEPSEEK_PRO_MODEL", "deepseek-v4-pro"))
        return st.secrets.get("DEEPSEEK_FLASH_MODEL", os.getenv("DEEPSEEK_FLASH_MODEL", os.getenv("DEEPSEEK_MODEL", "deepseek-v4-flash")))
    if provider in {"Anthropic", "qweapi"}:
        if depth == "深度版":
            return st.secrets.get("QWEAPI_OPUS_MODEL", st.secrets.get("QWEAPI_MODEL_DEEP", os.getenv("QWEAPI_OPUS_MODEL", os.getenv("QWEAPI_MODEL_DEEP", "claude-opus-4-8[1M]"))))
        return st.secrets.get("QWEAPI_HAIKU_MODEL", os.getenv("QWEAPI_HAIKU_MODEL", st.secrets.get("QWEAPI_MODEL", os.getenv("QWEAPI_MODEL", "claude-opus-4-8"))))
    return PROVIDER_PRESETS[provider]["model"]


def sidebar_request() -> tuple[ReportRequest, str, bool]:
    with st.sidebar:
        st.header("研究参数")
        industry = st.text_input("行业名称", value="具身智能", placeholder="例如：固态电池、AI制药、电子布")
        region = st.selectbox("地域范围", ["全球+中国专项", "全球", "中国", "美国", "欧洲", "自定义"], index=0)
        if region == "自定义":
            region = st.text_input("自定义地域范围", value="全球+中国专项")
        stance = st.selectbox("分析立场", ["PE/VC", "产业方", "二级市场", "战略咨询", "技术评估"], index=0)
        depth = st.segmented_control("报告深度", ["快速版", "标准版", "深度版"], default="标准版")
        provider = st.selectbox("模型提供方", list(PROVIDER_PRESETS.keys()), index=0)
        preset = PROVIDER_PRESETS[provider]
        model_default = default_model_for(provider, depth or "标准版")
        if provider in MODEL_CHOICES:
            choices = MODEL_CHOICES[provider]
            default_choice = model_default if model_default in choices else "自定义"
            model_choice = st.selectbox(
                "模型",
                choices,
                index=choices.index(default_choice),
                key=f"model_choice_{provider}_{depth or '标准版'}",
                help="Anthropic/qweapi：Claude 模型走 /v1/messages，GPT 5.5 自动走 /v1/chat/completions。",
            )
            if model_choice == "自定义":
                model = st.text_input("自定义模型", value=model_default, key=f"model_{provider}_{depth or '标准版'}")
            else:
                model = model_choice
        else:
            model = st.text_input(
                "模型",
                value=model_default,
                key=f"model_{provider}_{depth or '标准版'}",
                help="DeepSeek：快速版/标准版默认 flash，深度版默认 pro；OpenAI兼容接口可填服务商模型名。",
            )
        base_url = st.text_input("Base URL", value=preset["base_url"], help="OpenAI Responses 可留空；DeepSeek 默认 https://api.deepseek.com；Anthropic/qweapi 默认 https://qweapi.com。")
        live_web = st.toggle("实时网页访问", value=True, help=f"{provider}：{preset['web']}。关闭后只使用用户指定来源和模型已有知识。")
        prefer_wechat = st.toggle(
            "优先微信公众号文章",
            value=True,
            help="提高微信公众号/产业文章在检索和排序中的优先级，但仍会标注为需复核来源，不允许单独支撑强结论。",
        )
        source_default = 14 if (depth or "标准版") == "深度版" else 10
        max_local_sources = st.slider(
            "本地检索来源数",
            min_value=0,
            max_value=20,
            value=source_default,
            step=1,
            key=f"local_sources_{provider}_{depth or '标准版'}",
            help="仅 DeepSeek / Anthropic / OpenAI兼容模式使用。工具会自动过滤低相关来源；设为 0 可跳过自动搜索，只使用用户指定来源。",
        )

        st.divider()
        st.header("关注与来源")
        focus_questions = st.text_area(
            "重点问题",
            value="",
            placeholder="每行一个问题。留空则由工具自动提出关键问题。",
            height=92,
        )
        source_urls = st.text_area(
            "指定来源 URL",
            value="",
            placeholder="可粘贴年报、招股书、行业协会报告、新闻链接等。",
            height=92,
        )
        excluded_scope = st.text_area(
            "排除范围",
            value="",
            placeholder="例如：不讨论纯软件应用、不讨论消费级市场。",
            height=68,
        )

        with st.expander("搜索过滤"):
            allowed_domains = st.text_area(
                "只允许这些域名（可选）",
                value="",
                placeholder="例如：sec.gov\nstats.gov.cn\niea.org",
                height=70,
            )
            blocked_domains = st.text_area(
                "屏蔽这些域名",
                value="wikipedia.org,reddit.com,quora.com",
                height=70,
            )

        st.divider()
        st.header("API")
        manual_key = st.text_input("API Key", value="", type="password", help=f"可留空，使用环境变量 {preset['env']}。")
        timeout_seconds = st.slider("请求超时", min_value=120, max_value=1800, value=900, step=60, help="长研报 + web search 可能需要更久。标准版建议 900 秒，深度版建议 1200-1800 秒。")
        demo_mode = st.toggle("无 Key 时使用示例模式", value=True)

    request = ReportRequest(
        provider=provider,
        industry=industry.strip(),
        region=region,
        stance=stance,
        depth=depth or "标准版",
        focus_questions=focus_questions,
        source_urls=source_urls,
        excluded_scope=excluded_scope,
        model=model.strip() or default_model_for(provider, depth or "标准版"),
        live_web=live_web,
        base_url=base_url.strip(),
        allowed_domains=allowed_domains,
        blocked_domains=blocked_domains,
        timeout_seconds=timeout_seconds,
        max_local_sources=max_local_sources,
        prefer_wechat=prefer_wechat,
    )
    return request, manual_key, demo_mode


def ensure_state() -> None:
    defaults = {
        "markdown": "",
        "html": "",
        "sources": [],
        "raw_response": {},
        "last_request": None,
    }
    for key, value in defaults.items():
        st.session_state.setdefault(key, value)


def generate(req: ReportRequest, manual_key: str, demo_mode: bool) -> None:
    api_key = get_provider_api_key(req.provider, manual_key)
    if not req.industry:
        st.error("请先输入行业名称。")
        return

    if not api_key:
        if not demo_mode:
            env_hint = PROVIDER_PRESETS.get(req.provider, {}).get("env", "API_KEY")
            st.error(f"未检测到 API Key。请在侧边栏填写，或设置环境变量 {env_hint}。")
            return
        markdown_text, sources = sample_report(req)
        markdown_text = ensure_clickable_source_section(markdown_text, sources)
        st.session_state["markdown"] = markdown_text
        st.session_state["sources"] = sources
        st.session_state["html"] = render_report_html(markdown_text, req, sources)
        st.session_state["raw_response"] = {"mode": "demo"}
        st.session_state["last_request"] = req
        st.toast("已生成示例报告。填写 API Key 后可生成实时研报。")
        return

    progress = st.progress(0, text="正在准备研究提示词")
    try:
        progress.progress(20, text=f"正在调用 {req.provider} 生成研报")
        markdown_text, sources, raw = call_model(req, api_key)
        markdown_text = ensure_clickable_source_section(markdown_text, sources)
        progress.progress(78, text="正在渲染 HTML")
        html_text = render_report_html(markdown_text, req, sources)
        st.session_state["markdown"] = markdown_text
        st.session_state["sources"] = sources
        st.session_state["html"] = html_text
        st.session_state["raw_response"] = raw
        st.session_state["last_request"] = req
        progress.progress(100, text="生成完成")
    except APITimeoutError:
        progress.empty()
        st.error("生成失败：请求超时。建议先切到“快速版”，或把“请求超时”调到 1200-1800 秒；也可以临时关闭实时网页访问先验证模型与 Key 是否可用。")
    except Exception as exc:
        progress.empty()
        message = str(exc)
        if "503" in message or "Service Unavailable" in message:
            st.error("生成失败：Anthropic/qweapi 服务暂时不可用（503）。工具已自动重试并尝试从 [1M] 模型降级到普通模型；仍失败时建议稍后重试，或把模型手动改为 claude-opus-4-8。")
        else:
            st.error(f"生成失败：{exc}")


def render_result() -> None:
    markdown_text = st.session_state.get("markdown", "")
    html_text = st.session_state.get("html", "")
    sources = st.session_state.get("sources", [])
    req = st.session_state.get("last_request")

    if not markdown_text:
        st.info("在左侧填写行业和参数后点击生成。无 API Key 时可先用示例模式预览报告结构。")
        return

    quality = report_quality(markdown_text, sources)
    col1, col2, col3, col4, col5 = st.columns(5)
    col1.metric("可点击引用", quality["links"])
    col2.metric("API 来源", quality["api_sources"])
    col3.metric("不可点击脚注", quality.get("non_clickable_refs", 0))
    col4.metric("强结论词", len(quality.get("strong_term_hits", [])))
    col5.metric("结构提示", len(quality["warnings"]))
    if quality["warnings"]:
        st.warning("；".join(quality["warnings"]))
    if quality.get("strong_term_hits"):
        st.caption("强结论词：" + "、".join(quality["strong_term_hits"]))
    if quality.get("weak_source_hits"):
        st.caption("弱来源提示：" + "、".join(quality["weak_source_hits"]))

    safe_name = filename_safe(req.industry if req else "industry_report")
    stamp = datetime.now().strftime("%Y%m%d_%H%M")
    c1, c2, c3 = st.columns([1, 1, 1])
    c1.download_button(
        "下载 HTML",
        data=html_text.encode("utf-8"),
        file_name=f"{safe_name}_deep_research_{stamp}.html",
        mime="text/html",
        use_container_width=True,
    )
    c2.download_button(
        "下载 Markdown",
        data=markdown_text.encode("utf-8"),
        file_name=f"{safe_name}_deep_research_{stamp}.md",
        mime="text/markdown",
        use_container_width=True,
    )
    c3.download_button(
        "下载请求参数",
        data=request_to_json(req).encode("utf-8") if req else b"{}",
        file_name=f"{safe_name}_request_{stamp}.json",
        mime="application/json",
        use_container_width=True,
    )

    tab_report, tab_markdown, tab_sources, tab_prompt = st.tabs(["HTML 预览", "Markdown", "来源", "提示词"])
    with tab_report:
        st.components.v1.html(html_text, height=920, scrolling=True)
    with tab_markdown:
        st.text_area("Markdown 原文", value=markdown_text, height=720)
    with tab_sources:
        if sources:
            st.dataframe(sources, use_container_width=True, hide_index=True)
        else:
            st.info("未从 API 响应中解析到来源列表；请查看报告中的 Markdown 链接。")
    with tab_prompt:
        if req:
            st.text_area("生成提示词", value=build_prompt(req), height=720)


def main() -> None:
    ensure_state()
    req, manual_key, demo_mode = sidebar_request()

    st.title("IndustryScope 行业深研生成器")
    caption_suffix = st.secrets.get("CAPTION_SUFFIX", os.getenv("CAPTION_SUFFIX", ""))
    st.caption(f"输入行业，生成带可点击引用、引用审计和 HTML 下载的深度研究报告。{caption_suffix}")

    left, right = st.columns([1, 1])
    with left:
        generate_clicked = st.button("生成深度研报", type="primary", use_container_width=True)
    with right:
        prompt = build_prompt(req)
        st.download_button(
            "下载当前提示词",
            data=prompt.encode("utf-8"),
            file_name=f"{filename_safe(req.industry or 'industry')}_prompt.txt",
            mime="text/plain",
            use_container_width=True,
        )

    if generate_clicked:
        generate(req, manual_key, demo_mode)

    render_result()


if __name__ == "__main__":
    main()
