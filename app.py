from __future__ import annotations

import tempfile
from datetime import datetime
from pathlib import Path

import streamlit as st

from report_engine import (
    ReportRequest,
    build_prompt,
    call_model,
    config_value,
    ensure_clickable_source_section,
    filename_safe,
    build_source_pdf_package,
    get_provider_api_key,
    render_report_html,
    report_quality,
    request_to_json,
    sample_report,
)
from openai import APITimeoutError

from knowledge_base import (
    SOURCE_TYPE_TIERS,
    add_document,
    delete_document,
    export_kb_json,
    kb_stats,
    load_documents,
    search_knowledge_base,
)


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
        "model": config_value("OPENAI_MODEL", "gpt-5.1"),
        "base_url": "",
        "env": "OPENAI_API_KEY",
        "web": "OpenAI hosted web_search",
    },
    "DeepSeek": {
        "model": config_value("DEEPSEEK_MODEL", "deepseek-v4-flash"),
        "base_url": config_value("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
        "env": "DEEPSEEK_API_KEY",
        "web": "本地网页检索 + 来源上下文",
    },
    "OpenAI兼容": {
        "model": config_value("OPENAI_COMPAT_MODEL", "gpt-oss-120b"),
        "base_url": config_value("OPENAI_COMPAT_BASE_URL", ""),
        "env": "OPENAI_COMPAT_API_KEY",
        "web": "本地网页检索 + 来源上下文",
    },
    "Anthropic": {
        "model": config_value("QWEAPI_MODEL", "claude-opus-4-8"),
        "base_url": config_value("QWEAPI_BASE_URL", "https://qweapi.com"),
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
            return config_value("DEEPSEEK_PRO_MODEL", "deepseek-v4-pro")
        return config_value("DEEPSEEK_FLASH_MODEL", config_value("DEEPSEEK_MODEL", "deepseek-v4-flash"))
    if provider in {"Anthropic", "qweapi"}:
        if depth == "深度版":
            return config_value("QWEAPI_OPUS_MODEL", config_value("QWEAPI_MODEL_DEEP", "claude-opus-4-8[1M]"))
        return config_value("QWEAPI_HAIKU_MODEL", config_value("QWEAPI_MODEL", "claude-opus-4-8"))
    return PROVIDER_PRESETS[provider]["model"]


def sidebar_request() -> tuple[ReportRequest, str, bool]:
    with st.sidebar:
        st.header("研究参数")
        industry = st.text_input("行业名称", value="具身智能", placeholder="例如：固态电池、AI制药、电子布")
        region = st.selectbox("地域范围", ["全球+中国专项", "全球", "中国", "美国", "欧洲", "自定义"], index=0)
        if region == "自定义":
            region = st.text_input("自定义地域范围", value="全球+中国专项")
        st.caption("分析立场：默认全覆盖，不再需要手动选择。")
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
        source_default = 18 if (depth or "标准版") == "深度版" else 14
        max_local_sources = st.slider(
            "高价值来源数",
            min_value=0,
            max_value=32,
            value=source_default,
            step=1,
            key=f"local_sources_{provider}_{depth or '标准版'}",
            help="仅 DeepSeek / Anthropic / OpenAI兼容模式使用。工具会先扩展多渠道候选池，再按 T0/T1/T2/T3 和信息浓度筛选这些高价值来源；设为 0 可跳过自动搜索。",
        )
        st.divider()
        st.header("专属知识库")
        knowledge_mode = st.selectbox(
            "知识库使用模式",
            ["自动判断", "优先知识库", "优先公开信息", "只用知识库", "不使用知识库"],
            index=0,
            help="自动判断：同时检索知识库和公开信息，并要求模型比较证据质量；只用知识库会关闭实时网页访问。",
        )
        kb_top_k = st.slider(
            "知识库片段数",
            min_value=0,
            max_value=30,
            value=12,
            step=1,
            help="生成报告前从专属文档知识库召回的片段数。",
        )
        if knowledge_mode == "只用知识库":
            live_web = False

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
        use_knowledge_base=knowledge_mode != "不使用知识库" and kb_top_k > 0,
        knowledge_mode=knowledge_mode,
        kb_top_k=kb_top_k,
    )
    return request, manual_key, demo_mode


def ensure_state() -> None:
    defaults = {
        "markdown": "",
        "html": "",
        "sources": [],
        "raw_response": {},
        "last_request": None,
        "source_package": b"",
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
        st.session_state["source_package"] = build_source_pdf_package(sources, req, st.session_state["html"], markdown_text)
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
        progress.progress(88, text="正在生成来源快照 PDF 证据包")
        source_package = build_source_pdf_package(sources, req, html_text, markdown_text)
        st.session_state["markdown"] = markdown_text
        st.session_state["sources"] = sources
        st.session_state["html"] = html_text
        st.session_state["source_package"] = source_package
        st.session_state["raw_response"] = raw
        st.session_state["last_request"] = req
        if raw.get("_fallback_from"):
            st.warning(
                f"qweapi 原模型 {raw.get('_fallback_from')} 暂不可用，"
                f"已自动改用 {raw.get('_request_model_used', '备用模型')} 完成生成。"
            )
        progress.progress(100, text="生成完成")
    except APITimeoutError:
        progress.empty()
        st.error("生成失败：请求超时。建议先切到“快速版”，或把“请求超时”调到 1200-1800 秒；也可以临时关闭实时网页访问先验证模型与 Key 是否可用。")
    except Exception as exc:
        progress.empty()
        message = str(exc)
        if "503" in message or "Service Unavailable" in message:
            st.error("生成失败：qweapi 上游服务暂时不可用（503）。工具已自动重试 Claude [1M]、普通 Opus 和 GPT 5.5 路径；仍失败时通常是中转站或该 Key 的模型通道暂时不可用，建议稍后重试或临时切换 DeepSeek/OpenAI兼容。")
        else:
            st.error(f"生成失败：{exc}")


def render_result() -> None:
    markdown_text = st.session_state.get("markdown", "")
    html_text = st.session_state.get("html", "")
    sources = st.session_state.get("sources", [])
    source_package = st.session_state.get("source_package", b"")
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
    c1, c2, c3, c4 = st.columns([1, 1, 1, 1])
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
    if source_package:
        c4.download_button(
            "下载完整证据包 ZIP",
            data=source_package,
            file_name=f"{safe_name}_evidence_pack_{stamp}.zip",
            mime="application/zip",
            use_container_width=True,
        )
    else:
        c4.info("暂无证据包")

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


def render_knowledge_base() -> None:
    st.subheader("专属文档知识库")
    stats = kb_stats()
    c1, c2, c3 = st.columns(3)
    c1.metric("文档数", stats["documents"])
    c2.metric("片段数", stats["chunks"])
    c3.metric("存储目录", stats["root"])

    with st.expander("上传并入库", expanded=True):
        uploaded = st.file_uploader(
            "上传文档",
            type=["pdf", "docx", "md", "txt", "html", "htm", "xlsx", "xls", "csv"],
            accept_multiple_files=True,
        )
        col_a, col_b = st.columns(2)
        with col_a:
            source_type = st.selectbox("来源类型", list(SOURCE_TYPE_TIERS.keys()), index=list(SOURCE_TYPE_TIERS.keys()).index("券商/投行/咨询研报"))
            source_org = st.text_input("来源机构", placeholder="例如：SemiAnalysis、Bernstein、某券商、公司名")
            publish_date = st.text_input("发布日期", placeholder="例如：2026-05-12 或 2026Q1")
        with col_b:
            industry_tags = st.text_input("行业标签", placeholder="例如：硅光芯片, CPO, 肌电手环")
            company_tags = st.text_input("公司标签", placeholder="例如：Meta, TSMC, Intel")
            technology_tags = st.text_input("技术标签", placeholder="例如：DLC电极, TGV, glass substrate")
        title_prefix = st.text_input("标题前缀（可选）", placeholder="留空则使用文件名")
        if st.button("入库上传文档", type="primary", use_container_width=True):
            if not uploaded:
                st.error("请先选择文件。")
            else:
                ok = 0
                errors: list[str] = []
                for file in uploaded:
                    suffix = Path(file.name).suffix
                    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
                        tmp.write(file.getbuffer())
                        tmp_path = Path(tmp.name)
                    try:
                        title = f"{title_prefix.strip()} {Path(file.name).stem}".strip() if title_prefix.strip() else Path(file.name).stem
                        doc = add_document(
                            tmp_path,
                            title=title,
                            source_type=source_type,
                            source_org=source_org,
                            publish_date=publish_date,
                            industry_tags=industry_tags,
                            company_tags=company_tags,
                            technology_tags=technology_tags,
                        )
                        ok += 1
                        st.toast(f"已入库：{doc.title}（{doc.chunk_count} 个片段）")
                    except Exception as exc:
                        errors.append(f"{file.name}: {exc}")
                    finally:
                        tmp_path.unlink(missing_ok=True)
                if ok:
                    st.success(f"成功入库 {ok} 个文档。")
                if errors:
                    st.error("\n".join(errors))

    docs = load_documents()
    st.subheader("已入库文档")
    if docs:
        rows = []
        for doc in docs.values():
            rows.append({
                "doc_id": doc.get("doc_id"),
                "标题": doc.get("title"),
                "类型": doc.get("source_type"),
                "分层": doc.get("source_tier"),
                "机构": doc.get("source_org"),
                "日期": doc.get("publish_date"),
                "片段": doc.get("chunk_count"),
                "行业标签": doc.get("industry_tags"),
                "公司标签": doc.get("company_tags"),
                "技术标签": doc.get("technology_tags"),
            })
        st.dataframe(rows, use_container_width=True, hide_index=True)
        delete_id = st.text_input("删除文档 doc_id", placeholder="粘贴上表 doc_id 后点击删除")
        if st.button("删除该文档", use_container_width=True):
            if delete_id.strip():
                delete_document(delete_id.strip())
                st.success("已删除。")
            else:
                st.error("请填写 doc_id。")
        st.download_button(
            "导出知识库索引 JSON",
            data=export_kb_json().encode("utf-8"),
            file_name=f"industryscope_kb_export_{datetime.now().strftime('%Y%m%d_%H%M')}.json",
            mime="application/json",
            use_container_width=True,
        )
    else:
        st.info("还没有入库文档。")

    st.subheader("检索测试")
    query = st.text_input("测试查询", value="肌电手环 DLC 电极 材料 Meta")
    top_k = st.slider("返回片段数", 1, 20, 8, key="kb_test_top_k")
    if st.button("检索知识库", use_container_width=True):
        results = search_knowledge_base(query, top_k=top_k)
        if not results:
            st.warning("没有命中。可以尝试增加行业/公司/技术标签，或换一组关键词。")
        for idx, item in enumerate(results, start=1):
            with st.expander(f"KB{idx}｜{item.get('title')}｜分数 {item.get('score')}｜{item.get('filename')}"):
                st.write({
                    "chunk_id": item.get("chunk_id"),
                    "source_type": item.get("source_type"),
                    "source_tier": item.get("source_tier"),
                    "source_org": item.get("source_org"),
                    "publish_date": item.get("publish_date"),
                    "page": item.get("page"),
                    "section": item.get("section"),
                    "matched_terms": item.get("matched_terms"),
                })
                st.text_area("片段", value=item.get("text", ""), height=220, key=f"kb_chunk_{item.get('chunk_id')}")


def main() -> None:
    ensure_state()
    req, manual_key, demo_mode = sidebar_request()

    st.title("IndustryScope 行业深研生成器")
    caption_suffix = config_value("CAPTION_SUFFIX", "")
    st.caption(f"输入行业，生成带可点击引用、引用审计、HTML 下载和来源 PDF 证据包的深度研究报告。{caption_suffix}")

    tab_generate, tab_kb = st.tabs(["生成研报", "知识库"])
    with tab_generate:
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
    with tab_kb:
        render_knowledge_base()


if __name__ == "__main__":
    main()
