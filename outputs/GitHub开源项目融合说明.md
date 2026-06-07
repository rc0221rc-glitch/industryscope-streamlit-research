# IndustryScope GitHub 开源项目融合说明

生成日期：2026-06-06

## 筛选原则

本轮筛选不是为了堆依赖，而是寻找能直接提升“行业深度研究报告生成质量”的高星开源项目。优先级如下：

1. 能提升研究工作流：研究计划、迭代搜索、来源审计、结论审计。
2. 能提升网页抓取质量：正文抽取、噪声过滤、Markdown 化。
3. 能提升报告结构：多视角提纲、反证、引用质量。
4. 许可证与当前工具兼容，且不需要额外部署复杂服务。

## 参考项目

| 项目 | GitHub 星标 | 许可证 | 适合吸收的能力 | 本轮处理 |
|---|---:|---|---|---|
| `unclecode/crawl4ai` | 67k+ | Apache-2.0 | LLM-ready 网页抓取、Markdown 化、正文清洗 | 吸收方法论；用轻量 `trafilatura` 先替代手写正文抽取 |
| `assafelovic/gpt-researcher` | 27k+ | Apache-2.0 | autonomous research、任务拆解、多来源聚合、引用报告 | 融入“研究计划 -> 来源审计 -> 结论审计”流程 |
| `stanford-oval/storm` | 28k+ | MIT | 多视角知识策展、先提纲后写作 | 融入技术专家、产业链、客户、投资人、怀疑者等多视角要求 |
| `dzhng/deep-research` | 19k+ | MIT | breadth/depth 迭代搜索、递归追问 | 扩展查询计划，覆盖市场、技术、竞争、政策、财务、反证 |
| `langchain-ai/open_deep_research` | 11k+ | MIT | 可配置研究图、plan/search/write/publish 流程 | 融入生成提示词与质量门，不引入 LangGraph 重依赖 |
| `adbar/trafilatura` | 6k+ | Apache-2.0 | 高质量正文抽取、去噪、元数据提取 | 已加入 `requirements.txt`，作为抓取正文优先路径 |
| `D4Vinci/Scrapling` | 61k+ | BSD-3-Clause | 高性能 HTML Parser、CSS/XPath 选择器、适合结构化正文抽取；Fetcher/动态抓取层更强但会牵涉 Playwright | 已轻量集成 `Selector` 层，用于微信公众号、新闻、研报正文容器抽取；暂不默认启用动态浏览器抓取 |
| `scrapy/scrapy` | 62k+ | BSD-3-Clause | 大规模爬虫框架 | 暂不引入，当前单次行研不需要完整爬虫系统 |
| `mendableai/firecrawl` | 129k+ | AGPL-3.0 | 搜索/抓取/Markdown API | 暂不内嵌，AGPL 与服务依赖不适合当前本地 Streamlit 工具 |

## 已落地改动

1. 新增 `OPEN_SOURCE_RESEARCH_METHODS` 提示词模块。
2. 报告大纲新增“研究计划与检索策略”和“开源研究工作流自检”。
3. 本地检索从通用 query 升级为研究计划式 query：
   - 市场规模和预测口径
   - 技术路线和瓶颈
   - 竞争公司与年报/投资者材料
   - 政策、监管和出口管制
   - 投融资、IPO、估值
   - 失败案例、风险、反证
4. 来源排序加入权威性评分，优先政府/监管、交易所披露、论文、公司披露、权威数据机构。
5. 来源上下文加入来源类型与权威性加分，帮助模型区分强证据和背景材料。
6. 正文抓取升级为 `Scrapling Selector -> trafilatura -> BeautifulSoup` 三层流水线：
   - Scrapling 优先识别 `#js_content`、`.rich_media_area_primary_inner`、`article`、`main`、`.article-body`、`.entry-content` 等正文容器，显著提升微信公众号和产业文章抽取质量。
   - Trafilatura 继续承担通用新闻/报告页正文抽取。
   - BeautifulSoup 作为最后兜底，并移除导航、表单、脚本、页脚等噪声。
   - 来源上下文新增“正文抽取方法”，模型必须把 `search snippet only`、`no readable text extracted`、行业不相关正文降级或剔除。
7. 质量检查新增：
   - 是否有开源研究工作流自检
   - 是否有多视角审视
   - 是否有来源相关性审查/剔除来源说明
8. 信息收集升级为“宽检索 + 深信号检索 + 最新动作检索”：
   - 通用行业会强制搜索材料、工艺、设备、良率、可靠性、封装、专利、论文、拆解、招聘、供应链和客户认证。
   - 肌电/EMG 手环自动扩展 `DLC/diamond-like carbon`、干电极、皮肤-电极界面、阻抗、Meta/CTRL-Labs、专利和 Nature/论文关键词。
   - 玻璃基板封装自动扩展 `glass core substrate`、TGV、panel-level packaging、warpage、CTE、RDL、TSMC/Intel/Samsung/Corning 等关键词。
   - 新增 Google News RSS 与 arXiv 轻量入口；失败时静默回退，不影响主流程。
9. 来源筛选升级为“候选池 + 来源画像 + 信息浓度分层”：
   - 多渠道候选池包括搜索引擎、Google News RSS、OpenAlex、Crossref、arXiv、GitHub、专利/学术/官方种子入口和微信公众号。
   - 每条来源自动标注 T0/T1/T2/T3、信息源渠道、高质量信息浓度、使用限制。
   - T0/T1 优先支撑核心结论；T2 用于新闻和产业观点；T3 仅作为线索入口，不能单独支撑强结论。
   - 报告新增“信息源渠道分层复盘”，输出本次来源池质量结构和下次优先追踪的信息源。

## Scrapling 融合判断

Scrapling 对本工具有帮助，但最适合的不是完整爬虫框架化改造，而是把它的轻量 Parser/Selector 能力放进“证据正文抽取层”。原因如下：

1. 当前工具的质量短板主要是来源正文抽取不稳定，尤其是微信公众号、产业媒体和研报营销页；Scrapling 的 CSS/XPath 选择能力正好能补这个洞。
2. Streamlit Cloud 对浏览器依赖、系统包和冷启动时间更敏感；Scrapling 的 Fetcher/动态抓取层会牵涉 Playwright，因此不适合作为默认部署路径。
3. Selector 层依赖轻、许可证兼容，可以与现有 `requests`、`trafilatura`、`BeautifulSoup` 并行，失败时自动回退，不会让主流程变脆。
4. 本轮已用微信光芯片参考报告实测，工具可通过 `Scrapling Selector .rich_media_area_primary_inner` 抽取正文，并把方法写入来源上下文，方便后续引用审计。

## 后续可选重度集成

如果后续要继续增强，可以考虑两条路线：

1. 轻量路线：继续保持当前 Streamlit 单体架构，逐步加入可选搜索 API、PDF 抽取、公司公告定向抓取。
2. 重度路线：引入 LangGraph/open_deep_research 的状态图，把研究拆成 plan/search/extract/synthesize/audit/publish 多节点流程，适合做更慢但更稳定的“机构版”深研。
