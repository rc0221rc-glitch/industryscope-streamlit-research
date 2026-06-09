# IndustryScope 行业深研生成器

一个部署在 Streamlit 上的行业深度研究工具。输入行业名称和研究参数后，工具会生成一篇带可点击引用、引用审计表和 HTML 下载的深度研报。

工具支持多模型提供方：

- OpenAI Responses：使用 OpenAI 官方 `web_search` 工具检索公开资料。
- DeepSeek：使用 DeepSeek OpenAI-compatible Chat Completions；工具先本地检索/抓取网页，再把来源上下文交给模型。
- Anthropic：默认 Base URL 可指向 qweapi；Claude/Opus 模型走 Anthropic Messages，`gpt-5.5` 等 GPT 模型自动走 OpenAI Chat Completions，工具先本地检索/抓取网页，再把来源上下文交给模型。
- OpenAI 兼容接口：适配其他兼容 Chat Completions 的服务商，同样使用本地检索/抓取来源上下文。

## 快速运行

```powershell
python -m pip install -r requirements.txt
streamlit run app.py
```

可在侧边栏填写 `OPENAI_API_KEY`，也可以通过环境变量配置：

```powershell
$env:OPENAI_API_KEY="sk-..."
$env:OPENAI_MODEL="gpt-5.1"
streamlit run app.py
```

DeepSeek 配置示例：

```powershell
$env:DEEPSEEK_API_KEY="sk-..."
$env:DEEPSEEK_BASE_URL="https://api.deepseek.com"
$env:DEEPSEEK_MODEL="deepseek-v4-flash"
streamlit run app.py
```

OpenAI-compatible 配置示例：

```powershell
$env:OPENAI_COMPAT_API_KEY="..."
$env:OPENAI_COMPAT_BASE_URL="https://your-provider.example/v1"
$env:OPENAI_COMPAT_MODEL="your-model"
streamlit run app.py
```

Anthropic / qweapi 配置示例：

```powershell
$env:QWEAPI_AUTH_TOKEN="sk-..."
$env:QWEAPI_BASE_URL="https://qweapi.com"
$env:QWEAPI_MODEL="claude-opus-4-8"
$env:QWEAPI_MODEL_DEEP="claude-opus-4-8[1M]"
streamlit run app.py
```

在侧边栏选择 `Anthropic` 后，可直接选择：

- `claude-opus-4-8`
- `claude-opus-4-8[1M]`
- `gpt-5.5`

## 知识库持久化

Streamlit Community Cloud 的本地磁盘不适合作为长期知识库存储：应用重启、重新部署或资源回收后，`data/knowledge_base` 可能丢失。

当前工具提供两层保护：

- 手动快照：在“知识库 -> 知识库备份、恢复与远端持久化”中下载完整 ZIP 快照；恢复时上传该 ZIP，可合并或替换当前知识库。
- 远端快照：配置 S3/R2 后，每次上传、删除、公众号入库会自动同步完整知识库快照；应用重启且本地知识库为空时会自动尝试恢复。

Cloudflare R2 / S3-compatible 配置示例：

```powershell
$env:INDUSTRYSCOPE_KB_S3_BUCKET="your-bucket"
$env:INDUSTRYSCOPE_KB_S3_KEY="industryscope/kb_snapshot.zip"
$env:INDUSTRYSCOPE_KB_S3_ENDPOINT_URL="https://<account-id>.r2.cloudflarestorage.com"
$env:INDUSTRYSCOPE_KB_S3_REGION="auto"
$env:INDUSTRYSCOPE_KB_S3_ACCESS_KEY_ID="..."
$env:INDUSTRYSCOPE_KB_S3_SECRET_ACCESS_KEY="..."
streamlit run app.py
```

在 Streamlit Cloud 中，把同名变量填入 App -> Settings -> Secrets 即可。

上传说明：工具不设置单次数量限制，会逐个文件入库并记录失败项；但 Streamlit 和部署平台仍有单文件大小、内存和运行时长限制。上千份直播转写资料建议启用 R2/S3 远端持久化，并在上传后及时确认快照同步成功。

夸克网盘外部源：知识库页支持扫描夸克公开分享目录，适合 600MB/数百文件这类大批量资料，不再经过浏览器上传控件。公开分享通常可以枚举文件；若要服务器端下载原文并入库，可能需要在 Streamlit Secrets 中配置 `QUARK_COOKIE`。

## 微信公众号正文增强

公众号正文获取采用分层策略：先用公开 HTTP 抽取，失败后进入“待手动补全文队列”；用户也可以用 Chrome 书签采集、手动粘贴全文，或启用“本机浏览器抽取入库”。

本机浏览器抽取借鉴 web-access / OpenCLI Browser Bridge 的思路：IndustryScope 通过 Chrome DevTools Protocol 读取你已经打开并通过验证的真实浏览器 DOM，不破解验证码，不绕过登录或付费限制。

本地使用步骤：

```powershell
start chrome --remote-debugging-port=9222 --user-data-dir="%TEMP%\industryscope-chrome"
```

然后在这个 Chrome 窗口里打开公众号文章，通过验证并停留在正文页；回到知识库页，保持 `Chrome DevTools / web-access 地址` 为 `http://127.0.0.1:9222`，点击“从当前 Chrome 公众号页抽取并入库”。

注意：Streamlit Cloud 服务器不能直接读取用户电脑上的 Chrome。这个能力适用于本地运行 IndustryScope，或未来配置本机 web-access/OpenCLI 代理桥接的场景；云端部署仍保留自动抽取、失败队列和书签采集流程。

## 设计文档

工具的详细设计、参考报告分析和增强点见：

- `outputs/行业研究工具设计说明.md`

## 主要能力

- 输入行业后生成结构化深度研报
- 支持 OpenAI / DeepSeek / OpenAI-compatible provider
- 使用 OpenAI hosted search 或本地网页检索取得公开资料
- 本地检索升级为多渠道候选池：搜索引擎、Google News RSS、OpenAlex、Crossref、arXiv、GitHub、专利/学术/官方种子入口、微信公众号线索并行扩展
- 对候选来源进行 T0/T1/T2/T3 分层和“高质量信息浓度”评分，优先使用监管公告、年报、专利、顶级论文、政府/标准、公司官方和权威数据机构
- 新增专利报告书信息源：强制检索 WIPO/EPO/CNIPA/Google Patents 相关入口、patent landscape/report/insight、FTO、专利导航、专利地图、专利族、IPC/CPC、主要申请人和申请趋势，用于发现技术路线、竞争布局和潜在空白点
- 默认同时覆盖 PE/VC、产业方、二级市场、战略咨询、技术评估、客户/采购方、怀疑者/空头视角，不再要求用户手动选择单一研究立场
- 新增厂商深研硬性结构：国内外厂商不能只列名单，必须逐家公司比较价值链位置、技术/产品路线、客户阶段、量产/认证、融资/财务、最新进展、优势短板，并把厂商进展映射为技术路线成熟度证据
- 新增新兴/未来产业拐点预判：按 0-12、12-24、24-36、36 个月以上拆分时间窗口，分别评估商业、技术、供应链/上游成熟度、资本和政策先决条件、领先指标与失败信号
- 新增专属文档知识库 MVP：支持上传 PDF/DOCX/Markdown/TXT/HTML/Excel/CSV，解析切块后本地持久化检索；生成报告时与公开网页证据并行召回，并强制输出“知识库与公开信息证据对比”
- 新增“深信号/隐藏拐点”检索：强制追问材料、工艺、专利、论文、拆解、招聘、供应链、客户认证和最近公司动作，避免漏掉类似肌电手环 DLC 电极材料、玻璃基板封装 TGV/翘曲/台积电动作这类关键变量
- 新增“最新动作与信息时效”要求：优先检索最近 180/90/30 天公开资料，模型内置知识截止日期不得覆盖实时来源
- 借鉴 GPT Researcher / open_deep_research / STORM / deep-research 的研究工作流：研究计划、多视角提纲、迭代检索、来源审计、结论审计
- 本地检索会按相关性与来源权威性排序，并过滤电商、歌词、视频、地图、登录页等低价值来源
- 可默认优先检索微信公众号文章，用于发现中国市场产业线索；公众号来源会被标注为需复核，不单独支撑强结论
- 微信公众号内部会继续分层：调研纪要、专家访谈、电话会、产业链纪要、海外投行/研究机构翻译摘译、SemiAnalysis/Bernstein 等来源会被识别为高价值线索；营销号、荐股号、课程号会降权
- 使用 `Scrapling Selector + trafilatura + BeautifulSoup` 三层正文抽取：优先识别微信公众号、新闻、研报、公告正文容器；失败时回退通用正文抽取，避免把搜索摘要、导航页或电商页当证据
- 强制输出可点击引用
- HTML 单文件导出
- 生成完整证据包 ZIP：包含报告 HTML/Markdown、请求参数、来源索引，以及每条信息源的 PDF 文本快照；原始链接拒绝嵌入或打不开时仍可离线审计来源摘要和抓取状态
- 引用审计表与质量检查
- 无 API Key 时提供本地示例报告，方便预览样式
