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
    autosync_kb_snapshot,
    delete_document,
    export_kb_json,
    export_kb_snapshot_bytes,
    import_kb_snapshot_bytes,
    kb_stats,
    load_documents,
    restore_kb_snapshot_from_s3,
    search_knowledge_base,
    s3_sync_enabled,
    try_restore_from_s3_if_empty,
    upload_kb_snapshot_to_s3,
)
from quark_ingest import (
    SUPPORTED_SUFFIXES as QUARK_SUPPORTED_SUFFIXES,
    ingest_quark_files,
    scan_quark_share,
)
from wechat_ingest import (
    fetch_wechat_article,
    ingest_wechat_article,
    ingest_wechat_candidate_stub,
    ingest_wechat_fulltext,
    resolve_sogou_search_url,
    search_sogou_wechat,
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
        "wechat_candidates": [],
        "kb_auto_restore_checked": False,
        "quark_files": [],
        "quark_info": {},
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


def ingest_wechat_candidates(
    candidates: list[dict],
    selected_indexes: list[int],
    keyword: str,
    industry_tags: str,
    company_tags: str,
    technology_tags: str,
) -> tuple[int, list[str]]:
    ok = 0
    stub_ok = 0
    errors: list[str] = []
    if not selected_indexes:
        return ok, ["请至少选择一篇文章。"]
    progress = st.progress(0, text="正在入库公众号文章")
    try:
        for order, candidate_index in enumerate(selected_indexes, start=1):
            item = candidates[candidate_index]
            title = item.get("title", f"公众号文章 {candidate_index + 1}")
            try:
                progress.progress(order / len(selected_indexes), text=f"正在处理：{title[:36]}")
                article_url = resolve_sogou_search_url(item.get("search_url", ""))
                if not article_url:
                    raise RuntimeError("未能从搜狗跳转页解析真实微信文章链接。")
                article = fetch_wechat_article(
                    article_url,
                    title_hint=title,
                    account_hint=item.get("account", ""),
                    date_hint=item.get("published_at", ""),
                )
                result = ingest_wechat_article(
                    article,
                    keyword=keyword,
                    industry_tags=industry_tags or keyword,
                    company_tags=company_tags,
                    technology_tags=technology_tags,
                    search_rank=int(item.get("rank", candidate_index + 1)),
                )
                ok += 1
                st.toast(f"已入库：{result['document'].get('title', title)}")
            except Exception as exc:
                try:
                    stub = ingest_wechat_candidate_stub(
                        item,
                        keyword=keyword,
                        industry_tags=industry_tags or keyword,
                        company_tags=company_tags,
                        technology_tags=technology_tags,
                        error=str(exc),
                    )
                    stub_ok += 1
                    st.toast(f"已入库候选线索：{stub['document'].get('title', title)}")
                except Exception as stub_exc:
                    errors.append(f"{title}: 全文抓取失败：{exc}；候选线索入库也失败：{stub_exc}")
    finally:
        progress.empty()
    if stub_ok:
        st.warning(f"{stub_ok} 篇文章未能自动抓全文，已作为“候选线索/待补全文”正式入库。")
    return ok + stub_ok, errors


def sync_kb_after_write() -> None:
    message = autosync_kb_snapshot()
    if message:
        if "失败" in message:
            st.warning(message)
        elif "已上传" in message:
            st.toast(message)


def render_knowledge_base() -> None:
    if not st.session_state.get("kb_auto_restore_checked"):
        restore_message = try_restore_from_s3_if_empty()
        st.session_state["kb_auto_restore_checked"] = True
        if restore_message:
            if "失败" in restore_message:
                st.warning(restore_message)
            else:
                st.success(restore_message)

    st.subheader("专属文档知识库")
    stats = kb_stats()
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("文档数", stats["documents"])
    c2.metric("片段数", stats["chunks"])
    c3.metric("知识库大小", stats["size_human"])
    c4.metric("远端持久化", "已启用" if stats["remote_sync"] else "未配置")
    st.caption(f"本地缓存目录：{stats['root']}。Streamlit Cloud 本地磁盘可能在重启/重新部署后丢失；建议使用下方快照备份，或配置 S3/R2 远端持久化。")

    with st.expander("上传并入库", expanded=True):
        st.caption("可一次选择多个文件，工具会逐个入库并记录失败项。注意 Streamlit 会先接收上传内容，超大批量仍受部署平台内存和单文件大小限制。")
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
                total_size = sum(getattr(file, "size", 0) or 0 for file in uploaded)
                if total_size > 350 * 1024 * 1024:
                    st.warning("本批上传文件总量较大，入库时可能较慢；Streamlit 会先接收上传内容，若平台内存不足仍可能重启。工具不会限制单次数量，会尽量逐个处理。")
                upload_progress = st.progress(0, text="正在入库上传文档")
                for index, file in enumerate(uploaded, start=1):
                    upload_progress.progress(index / len(uploaded), text=f"正在处理 {index}/{len(uploaded)}：{file.name[:42]}")
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
                upload_progress.empty()
                if ok:
                    st.success(f"成功入库 {ok} 个文档。")
                    sync_kb_after_write()
                if errors:
                    st.error("\n".join(errors))

    with st.expander("知识库备份、恢复与远端持久化", expanded=False):
        st.caption("这里不是普通文档上传入口。快照 ZIP 用于整库备份/恢复；普通 PDF、DOCX、TXT 等请使用上方“上传并入库”。")
        snap_col1, snap_col2 = st.columns(2)
        with snap_col1:
            st.download_button(
                "下载知识库完整快照 ZIP",
                data=export_kb_snapshot_bytes(),
                file_name=f"industryscope_kb_snapshot_{datetime.now().strftime('%Y%m%d_%H%M')}.zip",
                mime="application/zip",
                use_container_width=True,
            )
        with snap_col2:
            if st.button("立即同步到远端 S3/R2", use_container_width=True, disabled=not s3_sync_enabled()):
                try:
                    st.success(upload_kb_snapshot_to_s3())
                except Exception as exc:
                    st.error(f"远端同步失败：{exc}")

        restore_file = st.file_uploader(
            "恢复知识库快照 ZIP（只接受 .zip，不接受 PDF/DOCX）",
            type=["zip"],
            key="kb_snapshot_restore",
        )
        restore_mode = st.radio("恢复模式", ["合并到当前知识库", "替换当前知识库"], horizontal=True)
        restore_col1, restore_col2 = st.columns(2)
        with restore_col1:
            if st.button("导入快照 ZIP", use_container_width=True):
                if not restore_file:
                    st.error("请先选择知识库快照 ZIP。普通 PDF/DOCX 请上传到上方“上传并入库”。")
                else:
                    try:
                        result = import_kb_snapshot_bytes(
                            restore_file.getvalue(),
                            merge=restore_mode == "合并到当前知识库",
                        )
                        sync_kb_after_write()
                        st.success(f"已恢复 {result['documents']} 个文档、{result['chunks']} 个片段。")
                    except Exception as exc:
                        st.error(f"恢复失败：{exc}")
        with restore_col2:
            if st.button("从远端 S3/R2 恢复", use_container_width=True, disabled=not s3_sync_enabled()):
                try:
                    result = restore_kb_snapshot_from_s3(merge=restore_mode == "合并到当前知识库")
                    st.success(f"已从 {result.get('remote')} 恢复 {result['documents']} 个文档、{result['chunks']} 个片段。")
                except Exception as exc:
                    st.error(f"远端恢复失败：{exc}")

    with st.expander("夸克网盘分享同步", expanded=False):
        st.caption("用于大批量外部文档入库，避免通过浏览器一次上传数百个文件。公开分享通常可扫描目录；下载原文入库可能需要配置 QUARK_COOKIE 登录态。微信公众号自动补新功能保留在下方。")
        quark_share_text = st.text_area(
            "夸克分享链接或整段分享文本",
            value="",
            placeholder="例如：https://pan.quark.cn/s/5268ba221cc4?pwd=WcJR\n提取码：WcJR",
            height=76,
        )
        q1, q2, q3 = st.columns([1, 1, 1])
        with q1:
            quark_passcode = st.text_input("提取码（可选）", value="", placeholder="例如：WcJR")
        with q2:
            quark_max_files = st.number_input("最多扫描文件数", min_value=10, max_value=20000, value=1000, step=50)
        with q3:
            quark_size_limit_mb = st.number_input("单文件下载上限MB", min_value=1, max_value=1000, value=120, step=10)
        quark_cookie = st.text_input(
            "QUARK_COOKIE（可选，建议配置到 Streamlit Secrets）",
            value="",
            placeholder="不填也可扫描公开分享；若下载提示 require login，需要填入夸克网页版登录后的 Cookie。",
            type="password",
        )
        qtag1, qtag2, qtag3 = st.columns(3)
        with qtag1:
            quark_source_type = st.selectbox("夸克入库来源类型", list(SOURCE_TYPE_TIERS.keys()), index=list(SOURCE_TYPE_TIERS.keys()).index("券商/投行/咨询研报"), key="quark_source_type")
            quark_industry_tags = st.text_input("夸克入库行业标签", value="", key="quark_industry_tags")
        with qtag2:
            quark_source_org = st.text_input("夸克入库来源机构", value="夸克网盘", key="quark_source_org")
            quark_company_tags = st.text_input("夸克入库公司标签", value="", key="quark_company_tags")
        with qtag3:
            quark_technology_tags = st.text_input("夸克入库技术标签", value="", key="quark_technology_tags")
            st.caption("支持：" + ", ".join(sorted(QUARK_SUPPORTED_SUFFIXES)))

        if st.button("扫描夸克分享目录", use_container_width=True):
            if not quark_share_text.strip():
                st.error("请先填写夸克分享链接或整段分享文本。")
            else:
                try:
                    with st.spinner("正在扫描夸克分享目录..."):
                        info, files = scan_quark_share(
                            quark_share_text.strip(),
                            passcode=quark_passcode.strip(),
                            cookie=quark_cookie.strip(),
                            max_files=int(quark_max_files),
                        )
                    st.session_state["quark_info"] = info.__dict__
                    st.session_state["quark_files"] = [file.__dict__ for file in files]
                    st.success(f"扫描完成：{len(files)} 个文件，总大小约 {round(sum(f.size for f in files) / 1024 / 1024, 1)} MB。")
                except Exception as exc:
                    st.session_state["quark_files"] = []
                    st.error(f"夸克扫描失败：{exc}")

        quark_files = st.session_state.get("quark_files", [])
        if quark_files:
            rows = []
            for idx, file in enumerate(quark_files, start=1):
                suffix = file.get("suffix", "")
                rows.append({
                    "选择": suffix in QUARK_SUPPORTED_SUFFIXES,
                    "序号": idx,
                    "文件名": file.get("name", ""),
                    "路径": file.get("path", ""),
                    "大小MB": round((file.get("size", 0) or 0) / 1024 / 1024, 2),
                    "类型": suffix,
                })
            edited = st.data_editor(
                rows,
                use_container_width=True,
                hide_index=True,
                disabled=["序号", "文件名", "路径", "大小MB", "类型"],
                key="quark_file_editor",
            )
            if st.button("下载所选夸克文件并入库", type="primary", use_container_width=True):
                selected = [quark_files[int(row["序号"]) - 1] for row in edited if row.get("选择")]
                if not selected:
                    st.error("请至少选择一个夸克文件。")
                else:
                    with st.spinner("正在从夸克下载并入库。大文件会比较慢，请保持页面打开..."):
                        ok, errors = ingest_quark_files(
                            quark_share_text.strip(),
                            quark_passcode.strip(),
                            selected,
                            cookie=quark_cookie.strip(),
                            source_type=quark_source_type,
                            source_org=quark_source_org.strip() or "夸克网盘",
                            industry_tags=quark_industry_tags.strip(),
                            company_tags=quark_company_tags.strip(),
                            technology_tags=quark_technology_tags.strip(),
                            size_limit_mb=int(quark_size_limit_mb),
                        )
                    if ok:
                        st.success(f"成功入库 {ok} 个夸克文件。")
                        sync_kb_after_write()
                    if errors:
                        st.error("\n".join(errors[:20]) + ("\n..." if len(errors) > 20 else ""))

    with st.expander("微信公众号自动补新", expanded=False):
        st.caption("通过搜狗微信搜索发现公众号文章；能抓到正文的会作为全文证据入库，遇到验证码/环境校验时只入库为候选线索，需人工补全文后才能作为报告证据。")
        col_w1, col_w2, col_w3 = st.columns([2, 1, 1])
        with col_w1:
            wechat_keyword = st.text_input("搜索关键词", value="", placeholder="例如：肌电手环 DLC 电极；玻璃基板封装 台积电 TGV")
        with col_w2:
            wechat_candidate_count = st.slider("候选篇数", 5, 30, 10, step=1)
        with col_w3:
            wechat_ingest_count = st.slider("默认入库最近篇数", 1, 20, 5, step=1)
        col_tag1, col_tag2, col_tag3 = st.columns(3)
        with col_tag1:
            wechat_industry_tags = st.text_input("公众号入库行业标签", value="", placeholder="默认使用搜索关键词")
        with col_tag2:
            wechat_company_tags = st.text_input("公众号入库公司标签", value="")
        with col_tag3:
            wechat_technology_tags = st.text_input("公众号入库技术标签", value="")
        manual_wechat_urls = st.text_area(
            "手动粘贴微信文章链接（可选）",
            value="",
            placeholder="每行一个 mp.weixin.qq.com 链接。用于搜狗跳转触发验证码时的稳定备用入口。",
            height=68,
        )
        st.markdown("#### 手动粘贴公众号全文入库")
        st.caption("如果搜狗或微信触发图片验证，请在浏览器里人工打开文章并复制正文。这里粘贴的全文会作为真正的知识库证据；只保存候选链接的条目不会支撑报告强结论。")
        manual_full_title = st.text_input("文章标题", value="", key="manual_wechat_full_title")
        col_full1, col_full2 = st.columns(2)
        with col_full1:
            manual_full_url = st.text_input("原文链接", value="", placeholder="https://mp.weixin.qq.com/s/...", key="manual_wechat_full_url")
            manual_full_account = st.text_input("公众号/作者", value="", key="manual_wechat_full_account")
        with col_full2:
            manual_full_date = st.text_input("发布日期", value="", placeholder="YYYY-MM-DD，可留空", key="manual_wechat_full_date")
            manual_full_keyword = st.text_input("归档关键词", value="", placeholder="默认使用上方搜索关键词或标题", key="manual_wechat_full_keyword")
        manual_full_content = st.text_area(
            "正文全文",
            value="",
            placeholder="粘贴文章正文主体。建议保留小标题、表格文字、来源说明和图片下方文字。",
            height=240,
            key="manual_wechat_full_content",
        )
        if st.button("入库手动粘贴的公众号全文", use_container_width=True):
            try:
                result = ingest_wechat_fulltext(
                    title=manual_full_title.strip(),
                    content=manual_full_content,
                    url=manual_full_url.strip(),
                    account=manual_full_account.strip(),
                    published_at=manual_full_date.strip(),
                    keyword=manual_full_keyword.strip() or wechat_keyword.strip() or manual_full_title.strip(),
                    industry_tags=wechat_industry_tags.strip() or wechat_keyword.strip() or manual_full_title.strip(),
                    company_tags=wechat_company_tags.strip(),
                    technology_tags=wechat_technology_tags.strip(),
                )
                st.success(f"已入库公众号全文：{result['document'].get('title', manual_full_title or '未命名文章')}")
                sync_kb_after_write()
            except Exception as exc:
                st.error(f"全文入库失败：{exc}")

        if st.button("搜索并入库最近公众号文章", type="primary", use_container_width=True):
            if not wechat_keyword.strip():
                st.error("请先填写搜索关键词。")
            else:
                try:
                    with st.spinner("正在通过搜狗微信搜索公众号文章..."):
                        candidates = search_sogou_wechat(wechat_keyword.strip(), limit=wechat_candidate_count, pages=4)
                    st.session_state["wechat_candidates"] = [candidate.__dict__ for candidate in candidates]
                    if candidates:
                        st.success(f"找到 {len(candidates)} 条候选。日期无法识别的文章会保留搜索排序。")
                        selected_indexes = list(range(min(wechat_ingest_count, len(candidates))))
                        ok, errors = ingest_wechat_candidates(
                            st.session_state["wechat_candidates"],
                            selected_indexes,
                            keyword=wechat_keyword.strip(),
                            industry_tags=wechat_industry_tags.strip() or wechat_keyword.strip(),
                            company_tags=wechat_company_tags.strip(),
                            technology_tags=wechat_technology_tags.strip(),
                        )
                        if ok:
                            st.success(f"已入库 {ok} 条公众号结果；其中抓取失败的条目会标记为候选线索，待人工补全文。")
                            sync_kb_after_write()
                        if errors:
                            st.error("\n".join(errors))
                    else:
                        st.warning("没有找到候选文章。可以换更具体的关键词，或稍后重试。")
                except Exception as exc:
                    st.session_state["wechat_candidates"] = []
                    st.error(f"搜索失败：{exc}")

        candidates = st.session_state.get("wechat_candidates", [])
        if candidates:
            rows = []
            for idx, item in enumerate(candidates, start=1):
                rows.append({
                    "选择": idx <= wechat_ingest_count,
                    "序号": idx,
                    "标题": item.get("title", ""),
                    "公众号": item.get("account", ""),
                    "日期": item.get("published_at", "") or "未识别",
                    "摘要": item.get("snippet", ""),
                })
            edited = st.data_editor(
                rows,
                use_container_width=True,
                hide_index=True,
                disabled=["序号", "标题", "公众号", "日期", "摘要"],
                key="wechat_candidate_editor",
            )
            if st.button("将当前勾选候选补充入库", use_container_width=True):
                selected_indexes = [int(row["序号"]) - 1 for row in edited if row.get("选择")]
                ok, errors = ingest_wechat_candidates(
                    candidates,
                    selected_indexes,
                    keyword=wechat_keyword.strip(),
                    industry_tags=wechat_industry_tags.strip() or wechat_keyword.strip(),
                    company_tags=wechat_company_tags.strip(),
                    technology_tags=wechat_technology_tags.strip(),
                )
                if ok:
                    st.success(f"成功补充入库 {ok} 条公众号结果；候选线索需人工补全文后才会作为报告证据。")
                    sync_kb_after_write()
                if errors:
                    st.error("\n".join(errors))

        if st.button("入库手动粘贴的微信文章链接", use_container_width=True):
            urls = [line.strip() for line in manual_wechat_urls.splitlines() if line.strip()]
            if not urls:
                st.error("请先粘贴至少一个微信文章链接。")
            else:
                ok = 0
                errors: list[str] = []
                progress = st.progress(0, text="正在入库手动微信链接")
                try:
                    for idx, url in enumerate(urls, start=1):
                        try:
                            progress.progress(idx / len(urls), text=f"正在抓取第 {idx} 篇")
                            article = fetch_wechat_article(url)
                            result = ingest_wechat_article(
                                article,
                                keyword=wechat_keyword.strip() or article.title,
                                industry_tags=wechat_industry_tags.strip() or wechat_keyword.strip() or article.title,
                                company_tags=wechat_company_tags.strip(),
                                technology_tags=wechat_technology_tags.strip(),
                                search_rank=idx,
                            )
                            ok += 1
                            st.toast(f"已入库：{result['document'].get('title', article.title)}")
                        except Exception as exc:
                            errors.append(f"{url}: {exc}")
                finally:
                    progress.empty()
                if ok:
                    st.success(f"成功入库 {ok} 篇手动微信文章。")
                    sync_kb_after_write()
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
                "原始链接": doc.get("source_url", ""),
            })
        st.dataframe(rows, use_container_width=True, hide_index=True)
        delete_id = st.text_input("删除文档 doc_id", placeholder="粘贴上表 doc_id 后点击删除")
        if st.button("删除该文档", use_container_width=True):
            if delete_id.strip():
                delete_document(delete_id.strip())
                sync_kb_after_write()
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
