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
6. 正文抓取优先使用 `trafilatura`，失败时回退 BeautifulSoup。
7. 质量检查新增：
   - 是否有开源研究工作流自检
   - 是否有多视角审视
   - 是否有来源相关性审查/剔除来源说明

## 后续可选重度集成

如果后续要继续增强，可以考虑两条路线：

1. 轻量路线：继续保持当前 Streamlit 单体架构，逐步加入可选搜索 API、PDF 抽取、公司公告定向抓取。
2. 重度路线：引入 LangGraph/open_deep_research 的状态图，把研究拆成 plan/search/extract/synthesize/audit/publish 多节点流程，适合做更慢但更稳定的“机构版”深研。

