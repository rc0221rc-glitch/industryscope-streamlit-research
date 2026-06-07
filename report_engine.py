from __future__ import annotations

import html
import json
import os
import re
import time
from urllib.parse import parse_qs, quote, unquote, urlparse
from dataclasses import dataclass, asdict
from datetime import datetime
from typing import Any
from xml.etree import ElementTree as ET

import markdown as markdown_lib
import requests
from bs4 import BeautifulSoup
from jinja2 import Template
from openai import OpenAI

try:
    import trafilatura
except ImportError:  # Optional: keep the app usable before requirements are installed.
    trafilatura = None

try:
    from scrapling import Selector as ScraplingSelector
except ImportError:  # Optional parser enhancement; BeautifulSoup remains the final fallback.
    ScraplingSelector = None

try:
    import streamlit as st
except ImportError:
    st = None


DEPTH_CONFIG = {
    "快速版": {
        "sections": "保留核心栏目，正文约 4,000-6,000 中文字。",
        "search_context_size": "medium",
        "reasoning_effort": "medium",
    },
    "标准版": {
        "sections": "覆盖完整行研框架，正文约 8,000-12,000 中文字。",
        "search_context_size": "high",
        "reasoning_effort": "high",
    },
    "深度版": {
        "sections": "覆盖完整行研框架，并增加反证、二阶问题和领先指标，正文约 12,000-18,000 中文字。",
        "search_context_size": "high",
        "reasoning_effort": "high",
    },
}


SOURCE_RULES = """
证据规则（必须执行）：
1. 每个关键事实、数字、融资事件、政策、公司进展都必须给出 Markdown 可点击链接，格式必须是 [来源标题](https://...)；禁止只写 [1.IDC]、[36氪] 这类不可点击脚注。
2. 来源分级：S=监管/公告/年报/招股书/政府/标准/顶级论文；A=权威数据机构/行业协会/头部咨询原文；B=主流财经媒体/券商研报/公司访谈；C=自媒体/百科/转载/未署名文章。执行摘要和投资结论不得主要依赖 C 级来源。
3. 核心数字必须写清口径：时间、地区、统计对象、单位、是否预测、是否收入/出货/安装/订单/产能。不同口径冲突时，必须并列呈现，不得强行合并。
4. 对至少 8 个核心数据点做交叉验证。不能双源验证时必须标注“单一来源，低/中置信度”。
5. 严格区分：已发生事实、公司目标、第三方预测、模型推断、作者观点。未来日期事件一律不得写成事实。
6. 不能编造 URL、公司、融资、财务、市场份额、政策、论文、客户绑定关系。来源没有明确支持时，写“证据不足”，不要补脑。
7. 引用审计表必须列出所有关键来源的 URL、类型、支持了哪条结论、证据强度、风险。来源质量评估不得自夸；必须指出缺口和弱证据。
"""


FAILURE_PATTERNS = """
请主动防止以下参考报告中暴露出的错误：
1. 层级混淆：不要把“芯片、器件、光模块、交换机、CPO封装、系统客户、光计算芯片”混成同一市场份额或同一竞争格局。
2. 概念泛化：行业名是“硅光芯片”时，不要把所有光芯片、VCSEL、InP EML、TFLN、光模块、光计算都不加边界地混入核心结论；可作为相邻赛道比较，但要标明关系。
3. 未来事实化：报告日期之后的 IPO、融资、政策、客户订单、产品量产不能当作已发生事实。
4. 弱来源强结论：网易、雪球、自媒体、Wikipedia、二手融资新闻不能支撑“全球第一、独家供应、90%份额、订单排到2028、估值锚”等强结论。
5. 市场规模误用：必须说明是 silicon photonics、photonic integrated circuit、optical transceiver、optical module、laser chip 还是 optical engine 市场；不同市场不可混加。
6. 份额无分母：任何“市占率第一、CR3、CR4、TOP10”都必须写明分母、地区、产品、年份和来源。
7. 财务错配：上市公司数据优先用年报/季报/交易所公告；不要用股吧、二手媒体替代。
8. 技术路线错判：TRL、良率、功耗、成本、带宽、波导损耗等工程指标必须说明实验室/样机/量产/客户验证阶段。
9. 投资建议越界：一级市场标的推荐必须给出估值、轮次、收入验证、客户验证、退出路径和失败条件；不能只因“国产替代/AI概念”给高评级。
"""


REFERENCE_REPORT_LESSONS = """
参考报告优点（要继承）：
1. 先界定研究对象、研究边界和投资立场，再进入结论。
2. 用时间线、关键参数、路线比较、产业链图谱、BOM、财务估值、政策、风险机会形成完整扫描。
3. 每个行业都追问二阶问题：当前瓶颈解决后，下一个瓶颈和价值迁移会在哪里。
4. 执行摘要信息密度高，能把市场规模、供需、竞争、融资、政策和投资结论压缩到一页。
5. 有一级市场视角，会关心未上市标的、融资轮次、估值、退出路径和投资窗口。

参考报告缺点（必须避免）：
1. 引用很多但不可点击，读者无法复核。
2. 口径不清：市场规模、出货量、产能、订单、收入、估值、份额经常混用。
3. 证据分级缺失：媒体、公众号、Wikipedia、券商、公司公告被放在同等权重。
4. 强结论过多：全球第一、独家供应、垄断、确定性极高等表述没有足够一手证据。
5. 技术阶段混淆：论文/样机/客户验证/量产/规模收入之间边界不清。
6. 未来事实化：把预测、计划、传闻、未发生日期当成事实写入。
7. 投资建议偏概念驱动：没有充分给出估值、收入验证、毛利、客户集中度和失败条件。

因此，你必须在报告最后输出“反错误审计”小节，逐条说明：
- 哪些强结论被降级或删除，为什么；
- 哪些数据存在口径冲突；
- 哪些关键判断只有单一来源；
- 哪些内容需要用户继续人工核验。
"""


OPEN_SOURCE_RESEARCH_METHODS = """
开源深度研究工具方法论（已内化到本工具，必须执行）：
1. GPT Researcher / open_deep_research 式工作流：先拆解研究任务和检索计划，再写报告；不要边搜边顺手下结论。输出前必须完成“研究计划、来源审计、结论审计”三步。
2. STORM 式多视角知识策展：至少从技术专家、产业链经营者、客户/采购方、一级市场投资人、二级市场分析师、怀疑者/空头六个视角提出问题。执行摘要必须回应这些视角中的关键分歧。
3. deep-research 式迭代检索：不得只用一轮宽泛关键词。必须覆盖 breadth（市场/技术/竞争/政策/财务）和 depth（二阶问题/反证/原始公告/论文/年报）两类查询意图。
4. Scrapling / Crawl4AI / Trafilatura 式内容清洗：只把工具明确抽取到的网页正文、公告正文、论文摘要、市场报告页面当作证据；搜索结果摘要只能作为候选线索。导航、登录页、电商页、视频页、歌词页、地图页、论坛闲聊、搜索结果页不得支撑结论。来源上下文中的“正文抽取方法”如果显示为 search snippet only、no readable text extracted 或正文与行业无关，必须把该来源降级或剔除。
5. 研究报告不是资料堆砌：每个来源都要回答“它支持了哪条判断、支持强度多高、不能支持什么”。如果来源只证明概念存在，不能外推到市场份额、收入或投资评级。
6. 发布前自检：报告最后必须输出“开源研究工作流自检”，逐项说明多视角是否覆盖、是否做了迭代检索、是否剔除低质来源、是否完成反证检查。
"""


RECENCY_AND_DEEP_SIGNAL_RULES = """
最新信息与深信号挖掘规则（必须执行）：
1. 不得依赖模型内置知识截止日期做最新判断。当前日期之后不可写成事实；当前日期之前但模型可能未知的内容，必须以实时来源或用户指定来源为准。
2. 报告必须优先检索并审计最近 180 天、最近 90 天和最近 30 天的公开信息：公司公告、投资者材料、专利、论文、招聘、会议、供应链订单、客户认证、媒体采访、微信公众号产业文章。
3. 每个行业都必须做“隐藏拐点/深信号”挖掘，不得只停留在市场规模和竞争格局。必须追问：真正限制产品性能或商业化的材料、界面、工艺、设备、良率、可靠性、封装、算法、数据、认证、供应商和客户导入分别是什么。
4. 任何产品型行业必须拆成“材料/核心部件/传感或执行器/芯片或控制器/封装或结构件/软件算法/数据校准/量产测试/客户认证”九层；任何制造型行业必须拆成“基材/设备/关键工艺/良率/检测/产能/客户认证/成本曲线/替代路线”九层。
5. 对疑似关键拐点，必须输出“深信号候选表”：候选拐点、为什么可能重要、证据来源、反证、需要继续核验的关键词。即使证据不足，也要把它列为待核验假设，而不是漏掉。
6. 示例：肌电/EMG 手环不能只写 AI 交互和 Meta 产品演示，必须检索 dry electrode、skin-electrode interface、impedance、diamond-like carbon/DLC、biocompatibility、sweat/durability、CTRL-Labs/Meta neural wristband、patent/paper 等关键词；玻璃基板封装不能只写先进封装需求，必须检索 glass core substrate、TGV、panel-level packaging、warpage、CTE、RDL、TSMC/Intel/Samsung/玻璃通孔/最新动作。
7. 如果检索没有发现用户认为重要的材料/工艺/拐点，报告必须在“待核验清单”中显式写出“未找到足够证据，不得据此断言不存在”。
"""


SECTION_TASKS = """
分章任务卡（每章都要按任务执行）：

## 研究边界与多立场框架
写一段“为什么这个行业边界容易被误读”。定义核心对象、价值链位置、包含/排除范围、相邻概念映射表。对于相邻概念，说明它们是上游、下游、替代、互补还是不同统计口径。随后输出多立场问题矩阵，必须同时覆盖 PE/VC、产业方、二级市场、战略咨询、技术评估、客户/采购方、怀疑者/空头七种视角。

## 核心问题
提出 5-7 个真正影响投资判断的问题，覆盖：需求确定性、技术路线胜率、供应链瓶颈、竞争格局、利润池、估值/退出、监管/地缘。每个问题说明为什么它重要。

## 研究计划与检索策略
先输出一张研究计划表：研究问题、需要的证据类型、优先来源、检索关键词、若找不到证据如何降级结论。关键词必须覆盖中英文、公司公告、政策/监管、论文/会议、市场数据、反证/失败案例。

## 执行摘要
输出 6-8 条结论，每条必须包含：一句话判断、2-3 个可点击证据、置信度、最大反证/失败条件、未来 6-18 个月要跟踪的信号。禁止没有证据的口号式结论。

## 深信号与隐藏拐点
必须输出表格：深信号候选、所属层级（材料/工艺/部件/封装/算法/客户认证/供应链/政策/其他）、为什么可能是行业拐点、支持证据、反证或不确定性、下一步核验关键词。不要因为证据不足就省略候选，证据不足时标注“待核验假设”。

## 最新动作与信息时效
必须列出最近 180 天、90 天、30 天内找到的关键更新；若未找到，写明检索过哪些关键词和来源类型。对 2026 年以来的新公司动作、客户导入、产线、政策、专利、论文、会议披露必须优先纳入。

## 历史沿革
不是堆时间线。每个里程碑必须说明“它改变了什么约束”：成本、性能、良率、客户采用、政策、资本化。技术史和商业史分开，不要把论文突破等同于量产。

## 当前水平与瓶颈
按“指标-当前最好公开水平-量产水平-目标水平-差距-瓶颈成因-证据”输出。工程指标必须区分实验室、样机、客户验证、规模量产。

## 市场规模与需求
至少列 2-3 个来源的市场规模预测，逐一写明口径差异。拆解需求公式：终端需求 × 单机用量/渗透率 × ASP/价格趋势。单独列“不能使用的口径”。

## 技术路线对比
比较主路线、替代路线和相邻路线：工作原理、可量产性、成本结构、性能上限、供应链、代表企业、客户采用、二阶问题。不要只写优缺点，要写“什么条件下路线会赢/输”。

## 竞争格局
按价值链分层：上游材料/设备、芯片/器件、封装/模块、系统/客户。每层分别列全球与中国代表公司。份额类结论必须有分母和来源；没有硬数据时用“公开证据显示领先/活跃”，不要写精确份额。

## 产业链与利润池
画文字版产业链图谱，标注卡点、国产化率、毛利区间、议价权。说明利润池在何处，为什么不是概念最热的地方。

## 成本结构与单位经济
输出 BOM/成本拆解时必须标注是估算还是披露。列敏感性：良率、ASP、材料价格、客户认证周期、产能利用率。不能拿单一媒体估算当行业事实。

## 政策、监管与地缘
只引用政策原文或权威转载。写清政策是“鼓励、补贴、准入、限制、出口管制、采购倾斜”哪一类；不要把政策支持直接等同于商业成功。

## 投融资与估值
一级市场：轮次、金额、投资方、估值是否披露、收入/订单验证、退出路径。二级市场：收入、毛利、利润、PS/PE、增长来源。未披露估值不得臆测。

## 风险、机会与领先指标
风险必须可观测。每个机会对应一个反证。领先指标要具体到“看什么公告/财报科目/客户认证/招标/良率/ASP/库存/产能”。

## 投资结论
输出 bull/base/bear 三情景，并给出触发条件、对应标的类型、应避免的标的类型。最后列“本报告最不确定的 5 个判断”。

## 引用审计表
不得省略。至少 10 条来源，深度版至少 18 条。每条写 Source ID、来源标题、URL、来源类型、支持结论、发布时间/访问时间、证据强度、风险。

## 反错误审计
用表格输出：潜在错误类型、报告中如何规避、仍需人工核验的点。至少覆盖：口径混用、层级混淆、未来事实化、弱来源强结论、技术阶段误判、投资建议越界。

## 开源研究工作流自检
按表格输出：借鉴方法、已执行动作、证据、未完成/残余风险。至少覆盖 GPT Researcher/open_deep_research 的研究计划与来源审计、STORM 的多视角提纲、deep-research 的迭代检索、Scrapling/Crawl4AI/Trafilatura 的正文清洗、发布前引用检查。
"""


REPORT_OUTLINE = """
请按以下结构输出 Markdown：

# {industry}深度研究报告

## 封面与元信息
## 研究边界与多立场框架
## 本报告要回答的核心问题
## 研究计划与检索策略
## 执行摘要
## 深信号与隐藏拐点
## 最新动作与信息时效
## 技术/产业历史沿革
## 当前水平与核心瓶颈
## 市场规模、口径冲突与需求驱动
## 技术路线或产品路线对比
## 竞争格局与代表企业
## 产业链图谱与价值分配
## 成本结构与单位经济
## 政策、监管与地缘因素
## 投融资、财务与估值
## 风险、机会与领先指标
## 投资/战略结论
## 引用审计表
## 研究局限与待核验清单
## 反错误审计
## 开源研究工作流自检
"""


HTML_TEMPLATE = """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{{ title }}</title>
  <style>
    :root {
      --ink: #20231f;
      --muted: #5e665f;
      --line: #dfe5dc;
      --paper: #fbfcf8;
      --panel: #ffffff;
      --accent: #1f7a5c;
      --accent-soft: #e5f2ec;
      --warn: #a65f00;
      --shadow: 0 12px 32px rgba(30, 42, 34, 0.08);
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      background: var(--paper);
      color: var(--ink);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif;
      line-height: 1.72;
    }
    .topbar {
      border-bottom: 1px solid var(--line);
      background: rgba(255,255,255,0.92);
      position: sticky;
      top: 0;
      z-index: 2;
      backdrop-filter: blur(10px);
    }
    .topbar-inner {
      max-width: 1120px;
      margin: 0 auto;
      padding: 12px 24px;
      display: flex;
      gap: 16px;
      align-items: center;
      justify-content: space-between;
      color: var(--muted);
      font-size: 14px;
    }
    .report {
      max-width: 1120px;
      margin: 0 auto;
      padding: 42px 24px 72px;
    }
    .hero {
      padding: 38px 40px;
      background: var(--panel);
      border: 1px solid var(--line);
      box-shadow: var(--shadow);
      margin-bottom: 28px;
    }
    .eyebrow {
      color: var(--accent);
      font-weight: 700;
      letter-spacing: 0;
      margin-bottom: 10px;
    }
    h1 {
      font-size: clamp(30px, 5vw, 56px);
      line-height: 1.08;
      margin: 0 0 14px;
      letter-spacing: 0;
    }
    .meta-grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(170px, 1fr));
      gap: 10px;
      color: var(--muted);
      font-size: 14px;
      margin-top: 18px;
    }
    .meta-item {
      border: 1px solid var(--line);
      background: #f7faf6;
      padding: 10px 12px;
    }
    .content {
      background: var(--panel);
      border: 1px solid var(--line);
      box-shadow: var(--shadow);
      padding: 36px 40px;
      overflow-wrap: anywhere;
    }
    h2 {
      margin-top: 36px;
      padding-top: 18px;
      border-top: 1px solid var(--line);
      font-size: 25px;
      line-height: 1.25;
    }
    h3 { margin-top: 26px; font-size: 19px; }
    p { margin: 12px 0; }
    a { color: var(--accent); text-decoration-thickness: 1px; text-underline-offset: 3px; }
    table {
      width: 100%;
      border-collapse: collapse;
      margin: 18px 0 26px;
      display: block;
      overflow-x: auto;
      white-space: nowrap;
      border: 1px solid var(--line);
    }
    th, td {
      padding: 10px 12px;
      border: 1px solid var(--line);
      vertical-align: top;
      white-space: normal;
      min-width: 120px;
    }
    th { background: var(--accent-soft); text-align: left; }
    blockquote {
      margin: 18px 0;
      padding: 12px 16px;
      border-left: 4px solid var(--accent);
      background: #f5faf7;
      color: var(--muted);
    }
    code {
      background: #eef3ee;
      padding: 2px 5px;
      border-radius: 4px;
    }
    .quality {
      margin-bottom: 22px;
      background: var(--panel);
      border: 1px solid var(--line);
      padding: 18px 20px;
      color: var(--muted);
    }
    .quality strong { color: var(--ink); }
    .footer {
      margin-top: 28px;
      color: var(--muted);
      font-size: 13px;
      text-align: center;
    }
    @media (max-width: 720px) {
      .report { padding: 20px 12px 44px; }
      .hero, .content { padding: 24px 18px; }
      .topbar-inner { padding: 10px 12px; align-items: flex-start; flex-direction: column; }
      table { font-size: 13px; }
    }
  </style>
</head>
<body>
  <div class="topbar">
    <div class="topbar-inner">
      <span>IndustryScope 行业深研生成器</span>
      <span>{{ generated_at }}</span>
    </div>
  </div>
  <main class="report">
    <section class="hero">
      <div class="eyebrow">Deep Research Report</div>
      <h1>{{ title }}</h1>
      <div class="meta-grid">
        {% for item in meta_items %}
        <div class="meta-item"><strong>{{ item.label }}</strong><br>{{ item.value }}</div>
        {% endfor %}
      </div>
    </section>
    {% if quality_html %}
    <section class="quality">{{ quality_html }}</section>
    {% endif %}
    <article class="content">
      {{ body_html }}
    </article>
    <div class="footer">Generated by IndustryScope. 请基于原始链接复核重大投资、法律、医学或工程决策。</div>
  </main>
</body>
</html>
"""


@dataclass
class ReportRequest:
    provider: str
    industry: str
    region: str
    depth: str
    focus_questions: str
    source_urls: str
    excluded_scope: str
    model: str
    live_web: bool
    base_url: str = ""
    allowed_domains: str = ""
    blocked_domains: str = "wikipedia.org,reddit.com,quora.com"
    timeout_seconds: int = 900
    max_local_sources: int = 8
    prefer_wechat: bool = True

    @property
    def stance(self) -> str:
        return "全立场覆盖（PE/VC、产业方、二级市场、战略咨询、技术评估、客户/采购方、怀疑者/空头）"


def build_prompt(req: ReportRequest) -> str:
    depth = DEPTH_CONFIG.get(req.depth, DEPTH_CONFIG["标准版"])
    today = datetime.now().strftime("%Y-%m-%d")
    source_urls = req.source_urls.strip() or "无用户指定来源。请主动检索公开资料。"
    focus = req.focus_questions.strip() or "请自行提出 4-6 个最重要的研究问题。"
    excluded = req.excluded_scope.strip() or "无特别排除项。"
    blocked = req.blocked_domains.strip() or "无。"
    allowed = req.allowed_domains.strip() or "无。"
    web_note = "请主动使用 web_search 检索公开资料。" if req.live_web else "当前为无实时搜索模式；若资料不足，必须明确说明证据不足，不要编造来源。"
    wechat_note = "优先搜索并纳入 site:mp.weixin.qq.com 的微信公众号/产业文章作为中国市场线索，但必须标注为 B/C 或 C 级来源；不得让公众号单独支撑市场份额、融资估值、客户订单、财务、全球第一等强结论。" if req.prefer_wechat else "不特别优先微信公众号文章。"
    return f"""你是一名严谨的产业研究负责人，正在为投资/战略决策生成可交付研报。

当前日期：{today}
模型提供方：{req.provider}
行业：{req.industry}
地域范围：{req.region}
分析立场：{req.stance}
报告深度：{req.depth}，{depth["sections"]}
用户关注问题：{focus}
用户指定来源：{source_urls}
排除范围：{excluded}
实时搜索要求：{web_note}
微信公众号渠道偏好：{wechat_note}
优先/限定域名：{allowed}
提示词层面屏蔽域名：{blocked}

{SOURCE_RULES}

{FAILURE_PATTERNS}

{REFERENCE_REPORT_LESSONS}

{OPEN_SOURCE_RESEARCH_METHODS}

{RECENCY_AND_DEEP_SIGNAL_RULES}

{SECTION_TASKS}

请优先搜索 2024-2026 年最新资料，尤其是当前日期之前最近 180/90/30 天的新动作；涉及历史沿革时可使用更早资料。你的模型内置知识可能早于当前日期，因此所有“最新、最近、今年、上月、本月、新动作”都必须以实时来源或用户指定来源为准。
请主动检索中英文资料，全球行业优先英文一手来源，中国专项优先中文官方/公告/主流财经来源；若开启微信公众号优先，公众号可用于发现产业链线索、访谈、公司动态和中文语境中的争议点，但必须在引用审计中降权并说明待核验项。
如果来源之间数字冲突，请不要强行统一，必须列出冲突口径并给出你采用的口径。

{REPORT_OUTLINE.format(industry=req.industry)}

输出要求：
- 只输出 Markdown 正文，不要包裹代码块。
- 使用中文。
- 表格不要太宽；必要时拆成多个表。
- 所有 URL 必须是 Markdown 链接形式，例如 [来源标题](https://example.com)。
- 引用审计表必须至少包含 12 条来源；快速版至少 8 条。
- 不要编造来源、公司事件、市场数据、论文、政策或 URL。
- 如果可用来源不足 5 条，不要全文终止；输出“证据受限版研报”，把强结论降级为假设，并列出缺失资料清单和建议补充来源。
"""


def _split_domains(value: str) -> list[str]:
    return [x.strip() for x in re.split(r"[,，\n]", value or "") if x.strip()]


def call_openai(req: ReportRequest, api_key: str) -> tuple[str, list[dict[str, str]], dict[str, Any]]:
    client = OpenAI(api_key=api_key, timeout=req.timeout_seconds, max_retries=1)
    depth = DEPTH_CONFIG.get(req.depth, DEPTH_CONFIG["标准版"])

    web_tool: dict[str, Any] = {
        "type": "web_search",
        "search_context_size": depth["search_context_size"],
    }

    allowed = _split_domains(req.allowed_domains)
    filters: dict[str, list[str]] = {}
    if allowed:
        filters["allowed_domains"] = allowed
    if filters:
        web_tool["filters"] = filters

    params: dict[str, Any] = {
        "model": req.model,
        "reasoning": {"effort": depth["reasoning_effort"]},
        "input": build_prompt(req),
    }
    if req.live_web:
        params.update(
            {
                "tools": [web_tool],
                "tool_choice": "required",
                "include": ["web_search_call.action.sources"],
            }
        )

    try:
        response = client.responses.create(**params)
    except Exception as exc:
        # Some non-reasoning models reject the reasoning parameter. Retry once with
        # the same prompt and tools before surfacing the error.
        if "reasoning" not in str(exc).lower():
            raise
        params.pop("reasoning", None)
        response = client.responses.create(**params)
    text = getattr(response, "output_text", "") or ""
    data = response.model_dump() if hasattr(response, "model_dump") else json.loads(response.model_dump_json())
    sources = collect_sources(data)
    return text, sources, data


def search_public_web(req: ReportRequest, max_results: int = 12) -> list[dict[str, str]]:
    """Collect public source candidates for providers without hosted web search."""
    deadline = time.monotonic() + {"快速版": 18, "标准版": 28, "深度版": 42}.get(req.depth, 28)
    aliases = industry_search_aliases(req.industry)
    queries = build_research_queries(req, aliases)
    results: list[dict[str, str]] = []
    seen: set[str] = set()

    user_urls = re.findall(r"https?://[^\s)）]+", req.source_urls or "")
    for url in user_urls:
        normalized = url.strip("，,。.;；")
        if normalized not in seen:
            seen.add(normalized)
            results.append({
                "title": "用户指定来源",
                "url": normalized,
                "snippet": "",
                "relevance": str(max(6, source_relevance_score(req.industry, "用户指定来源", normalized, ""))),
            })

    if not req.live_web:
        return results[:max_results]

    allowed_domains = set(_split_domains(req.allowed_domains))
    blocked_domains = set(_split_domains(req.blocked_domains))

    def domain_ok(url: str) -> bool:
        host = urlparse(url).netloc.lower().removeprefix("www.")
        if allowed_domains and not any(host == d or host.endswith("." + d) for d in allowed_domains):
            return False
        if any(host == d or host.endswith("." + d) for d in blocked_domains):
            return False
        if is_low_value_domain(host):
            return False
        return True

    def add(title: str, url: str, snippet: str = "") -> None:
        clean_url = unwrap_search_url(url)
        if not clean_url.startswith(("http://", "https://")):
            return
        if clean_url in seen or not domain_ok(clean_url):
            return
        score = source_relevance_score(req.industry, title, clean_url, snippet)
        if score < 2:
            return
        seen.add(clean_url)
        results.append({
            "title": title.strip()[:220] or clean_url,
            "url": clean_url,
            "snippet": snippet.strip()[:500],
            "relevance": str(score),
        })

    for item in curated_source_candidates(req.industry):
        add(item["title"], item["url"], item.get("snippet", ""))

    query_limit = {"快速版": 8, "标准版": 16, "深度版": 26}.get(req.depth, 16)
    for query in queries[:query_limit]:
        if time.monotonic() > deadline:
            break
        try:
            for item in google_news_rss_search(query):
                add(item.get("title", ""), item.get("url", ""), item.get("snippet", ""))
        except Exception:
            pass
        if time.monotonic() > deadline:
            break
        try:
            for item in arxiv_search(query):
                add(item.get("title", ""), item.get("url", ""), item.get("snippet", ""))
        except Exception:
            pass
        if time.monotonic() > deadline:
            break
        try:
            for item in jina_search(query):
                add(item.get("title", ""), item.get("url", ""), item.get("snippet", ""))
        except Exception:
            pass
        if time.monotonic() > deadline:
            break
        try:
            for item in duckduckgo_html_search(query):
                add(item.get("title", ""), item.get("url", ""), item.get("snippet", ""))
        except Exception:
            pass
        if time.monotonic() > deadline:
            break
        try:
            for item in bing_html_search(query):
                add(item.get("title", ""), item.get("url", ""), item.get("snippet", ""))
        except Exception:
            continue
    return rank_sources(results, prefer_wechat=req.prefer_wechat, max_results=max_results)


def industry_search_aliases(industry: str) -> list[str]:
    """Add common English aliases so Chinese queries still work when search engines degrade."""
    lowered = industry.lower()
    aliases = [industry]
    if any(term in industry for term in ["硅光", "光芯片", "硅光芯片"]) or "silicon photonic" in lowered:
        aliases.extend([
            "silicon photonics",
            "silicon photonic integrated circuit",
            "photonic integrated circuit",
            "CPO co-packaged optics",
            "optical transceiver",
        ])
    if any(term in industry for term in ["肌电", "肌電", "手环", "手環", "神经腕带", "腕带"]) or any(term in lowered for term in ["emg", "electromyography", "neural wristband"]):
        aliases.extend([
            "EMG wristband",
            "electromyography wristband",
            "neural wristband",
            "surface EMG",
            "sEMG dry electrode",
            "skin electrode interface",
            "diamond-like carbon electrode",
            "DLC electrode",
            "Meta neural wristband",
            "CTRL-Labs wristband",
        ])
    if any(term in industry for term in ["玻璃基板", "玻璃基板封装", "玻璃封装", "先进封装"]) or any(term in lowered for term in ["glass substrate", "glass core", "advanced packaging"]):
        aliases.extend([
            "glass core substrate",
            "glass substrate advanced packaging",
            "glass interposer",
            "through glass via",
            "TGV glass substrate",
            "panel level packaging glass substrate",
            "RDL glass substrate",
            "TSMC glass substrate packaging",
        ])
    return list(dict.fromkeys([item for item in aliases if item.strip()]))


def build_research_queries(req: ReportRequest, aliases: list[str]) -> list[str]:
    """Research-plan style queries inspired by GPT Researcher/deep-research."""
    core = " ".join(aliases[:3])
    primary = aliases[1] if len(aliases) > 1 else req.industry
    year_month = datetime.now().strftime("%Y-%m")
    wechat_queries = []
    if req.prefer_wechat:
        wechat_queries = [
            f"site:mp.weixin.qq.com {req.industry} 深度 产业链 市场规模",
            f"site:mp.weixin.qq.com {req.industry} 技术路线 竞争格局 投融资",
            f"site:mp.weixin.qq.com {req.industry} 产业纪要 专家访谈 客户验证",
            f"site:mp.weixin.qq.com {req.industry} 国产替代 风险 反证",
            f"site:mp.weixin.qq.com {req.industry} 材料 工艺 瓶颈 良率 客户认证",
            f"site:mp.weixin.qq.com {req.industry} 专利 论文 拆解 供应链 最新",
            f"{req.industry} 微信公众号 深度研究 行业报告",
        ]
    deep_signal_queries = build_deep_signal_queries(req, aliases)
    queries = wechat_queries + deep_signal_queries + [
        f"{req.industry} 最新 进展 动作 {year_month} 2026",
        f"{req.industry} 最近 30 天 90 天 180 天 公司 动作 2026",
        f"{req.industry} 技术路线 市场规模 产业链 竞争格局 2026",
        f"{req.industry} 白皮书 行业报告 年报 招股书 2025 2026",
        f"{req.industry} 政策 监管 标准 产业规划 2025 2026",
        f"{req.industry} 投融资 并购 IPO 估值 2025 2026",
        f"{req.industry} 上市公司 年报 财报 投资者关系 2025 2026",
        f"{req.industry} 失败 风险 瓶颈 良率 成本 客户认证",
        f"{req.industry} 材料 工艺 设备 良率 可靠性 封装 量产 瓶颈",
        f"{req.industry} 专利 论文 会议 招聘 供应商 客户认证",
        f"{req.industry} 拆解 teardown BOM 关键材料 关键部件",
        f"{req.industry} hidden bottleneck key material process patent",
        f"{core} market size forecast CAGR industry report 2026",
        f"{core} latest news company update 2026",
        f"{core} technology roadmap bottleneck yield cost 2025 2026",
        f"{core} materials process reliability patent paper 2024 2025 2026",
        f"{core} teardown bill of materials key component supplier",
        f"{core} leading companies annual report investor presentation 2025 2026",
        f"{core} supply chain value chain profit pool customer adoption",
        f"{core} standards policy export control regulation",
        f"{primary} paper review IEEE Nature SPIE OFC 2024 2025 2026",
    ]
    if "中国" in req.region:
        queries.extend(
            [
                f"{req.industry} 中国 市场规模 政策 竞争格局 2026",
                f"{req.industry} A股 港股 上市公司 年报 2025 2026",
                f"{req.industry} 招股书 问询函 交易所 公告",
                f"{req.industry} 国产替代 出口管制 供应链 风险",
            ]
        )
    focus_terms = [line.strip() for line in re.split(r"[\n；;]", req.focus_questions or "") if line.strip()]
    for term in focus_terms[:4]:
        queries.append(f"{req.industry} {term} 证据 来源 数据 2025 2026")
    return list(dict.fromkeys(queries))


def build_deep_signal_queries(req: ReportRequest, aliases: list[str]) -> list[str]:
    industry = req.industry
    lowered = " ".join([industry] + aliases).lower()
    queries: list[str] = []
    if any(term in industry for term in ["肌电", "肌電", "手环", "手環", "神经腕带", "腕带"]) or any(term in lowered for term in ["emg", "electromyography", "neural wristband"]):
        queries.extend([
            "Meta neural wristband EMG electrode diamond-like carbon DLC",
            "\"diamond-like carbon\" \"EMG\" electrode wristband",
            "\"DLC\" \"dry electrode\" \"EMG\" wristband",
            "\"skin-electrode interface\" \"surface EMG\" dry electrode impedance",
            "CTRL-Labs Meta neural wristband electrode patent",
            "Meta Reality Labs EMG wristband dry electrode patent",
            "surface EMG dry electrode material impedance sweat durability review 2024 2025",
            "sEMG wearable dry electrodes diamond like carbon biocompatibility",
            "site:patents.google.com Meta EMG wristband electrode",
            "site:patents.google.com CTRL-Labs wristband electrode EMG",
            "site:tech.facebook.com EMG wristband electrode",
            "site:about.meta.com neural wristband EMG electrode",
            "site:mp.weixin.qq.com 肌电 手环 电极 材料 DLC 类金刚石碳",
            "site:mp.weixin.qq.com Meta 肌电手环 电极 材料 神经腕带",
        ])
    if any(term in industry for term in ["玻璃基板", "玻璃基板封装", "玻璃封装", "先进封装"]) or any(term in lowered for term in ["glass substrate", "glass core", "advanced packaging"]):
        queries.extend([
            "TSMC glass substrate advanced packaging 2026 latest",
            "TSMC glass core substrate CoWoS advanced packaging 2026",
            "台积电 玻璃基板 先进封装 2026 最新 动作",
            "台积电 玻璃通孔 TGV 玻璃基板 封装",
            "glass core substrate TGV panel level packaging warpage CTE RDL 2026",
            "Intel glass substrate advanced packaging 2030 TGV RDL",
            "Samsung glass substrate advanced packaging 2026",
            "Ajinomoto glass core substrate advanced packaging",
            "Corning glass substrate semiconductor packaging TGV",
            "abf substrate vs glass core substrate advanced packaging bottleneck",
            "site:mp.weixin.qq.com 玻璃基板 封装 台积电 TGV 最新",
            "site:mp.weixin.qq.com 玻璃基板 先进封装 产业链 良率 翘曲",
        ])
    return queries


def is_low_value_domain(host: str) -> bool:
    if "mp.weixin.qq.com" in host:
        return False
    if host in {"patents.google.com", "scholar.google.com", "news.google.com"}:
        return False
    low_value_domains = [
        "youtube.com", "youtu.be", "google.com", "google.com.mx", "maps.google.com",
        "facebook.com", "instagram.com", "tiktok.com", "pinterest.com", "x.com",
        "twitter.com", "reddit.com", "quora.com", "genius.com", "lyrics.com",
        "yelp.com", "yellowpages.com", "cars.com", "claycooley.com", "off---white.com",
        "off-white.com", "xfinity.com", "comcast.com",
        "baike.baidu.com", "wikipedia.org", "wikiwand.com",
    ]
    return any(host == domain or host.endswith("." + domain) for domain in low_value_domains)


def source_relevance_score(industry: str, title: str, url: str, snippet: str = "") -> int:
    text = f"{title} {url} {snippet}".lower()
    host = urlparse(url).netloc.lower().removeprefix("www.")
    score = 0

    for alias in industry_search_aliases(industry):
        alias_l = alias.lower()
        if alias_l and alias_l in text:
            score += 4 if alias_l == industry.lower() else 3

    domain_terms = [
        "semiconductor", "photonics", "photonic", "silicon", "optical", "transceiver",
        "co-packaged", "copackaged", "cpo", "pic", "laser", "modulator", "datacenter",
        "data center", "foundry", "wafer", "chip", "chiplet", "光子", "硅光", "光芯片",
        "光模块", "光通信", "光电", "半导体", "晶圆", "封装",
        "material", "materials", "process", "patent", "paper", "review", "roadmap",
        "yield", "reliability", "impedance", "electrode", "interface", "sensor",
        "substrate", "interposer", "via", "rdl", "packaging", "supplier", "customer",
        "certification", "qualification", "recruiting", "job", "conference", "teardown",
        "材料", "工艺", "专利", "论文", "综述", "良率", "可靠性", "阻抗", "电极",
        "界面", "传感器", "基板", "通孔", "供应商", "客户认证", "招聘", "会议", "拆解",
        "最新", "进展", "动作", "量产", "试产", "产线",
        "emg", "semg", "electromyography", "wristband", "neural", "dlc", "diamond-like carbon",
        "dry electrode", "skin-electrode", "肌电", "手环", "腕带", "类金刚石碳", "干电极",
        "glass core", "glass substrate", "through glass via", "tgv", "panel-level", "玻璃基板", "玻璃通孔",
    ]
    score += sum(1 for term in domain_terms if term in text)

    high_quality_hosts = [
        "sec.gov", "investor.", "annualreports.com", "ieee.org", "nature.com",
        "spiedigitallibrary.org", "semiconductors.org", "semi.org", "yolegroup.com",
        "lightcounting.com", "omdia.com", "idc.com", "gartner.com", "marketsandmarkets.com",
        "grandviewresearch.com", "imarcgroup.com", "intel.com", "cisco.com", "broadcom.com",
        "marvell.com", "coherent.com", "lumentum.com", "synopsys.com", "cadence.com",
        "tsmc.com", "imec-int.com", "imec.be", "imec.org", "ofcconference.org",
        "patents.google.com", "uspto.gov", "wipo.int", "acm.org", "mdpi.com",
        "about.meta.com", "tech.facebook.com", "ai.meta.com", "realitylabs.com",
        "samsung.com", "corning.com", "ajinomoto.com",
        "caict.ac.cn", "miit.gov.cn", "sse.com.cn", "szse.cn", "hkexnews.hk",
    ]
    if any(token in host for token in high_quality_hosts):
        score += 2

    unrelated_terms = [
        "off-white", "sneaker", "shoes", "fashion", "sunglasses", "lyrics", "rihanna",
        "disturbia", "maps", "login", "password", "kia", "dealer", "restaurant",
        "hotel", "travel", "youtube", "gmail", "xfinity", "comcast",
    ]
    if any(term in text for term in unrelated_terms):
        score -= 8
    if "mp.weixin.qq.com" in host:
        score += 4
    return score


def rank_sources(sources: list[dict[str, str]], prefer_wechat: bool = False, max_results: int | None = None) -> list[dict[str, str]]:
    def score(item: dict[str, str]) -> int:
        return int(item.get("relevance", "0")) + source_authority_score(item.get("url", ""))

    ranked = sorted(sources, key=score, reverse=True)
    if not prefer_wechat or not max_results:
        return ranked[:max_results] if max_results else ranked

    wechat = [item for item in ranked if "mp.weixin.qq.com" in urlparse(item.get("url", "")).netloc.lower()]
    non_wechat = [item for item in ranked if item not in wechat]
    if not wechat:
        return ranked[:max_results]

    wechat_slots = min(len(wechat), max(1, max_results // 4))
    selected = wechat[:wechat_slots]
    for item in non_wechat:
        if len(selected) >= max_results:
            break
        selected.append(item)
    for item in wechat[wechat_slots:]:
        if len(selected) >= max_results:
            break
        selected.append(item)
    return selected[:max_results]


def source_authority_score(url: str) -> int:
    host = urlparse(url).netloc.lower().removeprefix("www.")
    path = urlparse(url).path.lower()
    score = 0
    if "mp.weixin.qq.com" in host:
        score += 4
    if host.endswith((".gov", ".edu")) or ".gov." in host or ".edu." in host:
        score += 6
    if any(token in host for token in ["sec.gov", "sse.com.cn", "szse.cn", "hkexnews.hk", "cninfo.com.cn"]):
        score += 6
    if any(token in host for token in ["ieee.org", "nature.com", "science.org", "springer.com", "spiedigitallibrary.org", "arxiv.org"]):
        score += 5
    if any(token in host for token in ["patents.google.com", "uspto.gov", "wipo.int"]):
        score += 5
    if any(token in host for token in ["about.meta.com", "tech.facebook.com", "ai.meta.com", "tsmc.com", "intel.com", "samsung.com", "corning.com"]):
        score += 4
    if any(token in host for token in ["yolegroup.com", "lightcounting.com", "omdia.com", "idc.com", "gartner.com", "marketsandmarkets.com"]):
        score += 4
    if any(token in host for token in ["investor", "annualreports.com"]) or any(token in path for token in ["annual", "10-k", "investor", "presentation"]):
        score += 3
    if any(token in path for token in ["login", "signin", "privacy", "terms"]) or re.search(r"(^|/)search($|/)", path):
        score -= 3
    return score


def classify_source_type(url: str) -> str:
    host = urlparse(url).netloc.lower().removeprefix("www.")
    path = urlparse(url).path.lower()
    if "mp.weixin.qq.com" in host:
        return "B/C 微信公众号/产业文章，需交叉验证"
    if host.endswith((".gov", ".edu")) or ".gov." in host:
        return "S 政府/监管/高校"
    if any(token in host for token in ["sec.gov", "sse.com.cn", "szse.cn", "hkexnews.hk", "cninfo.com.cn"]):
        return "S 监管公告/交易所披露"
    if any(token in host for token in ["ieee.org", "nature.com", "science.org", "springer.com", "spiedigitallibrary.org", "arxiv.org"]):
        return "S 论文/学术资料"
    if any(token in host for token in ["patents.google.com", "uspto.gov", "wipo.int"]):
        return "S 专利/知识产权"
    if any(token in host for token in ["investor", "annualreports.com"]) or any(token in path for token in ["annual", "10-k", "investor", "presentation"]):
        return "S/A 公司披露"
    if any(token in host for token in ["yolegroup.com", "lightcounting.com", "omdia.com", "idc.com", "gartner.com", "marketsandmarkets.com", "grandviewresearch.com", "imarcgroup.com"]):
        return "A 数据机构/行业研究"
    if any(token in host for token in ["reuters.com", "bloomberg.com", "wsj.com", "ft.com", "caixin.com"]):
        return "B 主流财经媒体"
    return "B/C 普通网页，需复核"


def curated_source_candidates(industry: str) -> list[dict[str, str]]:
    lowered = industry.lower()
    sources: list[dict[str, str]] = []
    if any(term in industry for term in ["肌电", "肌電", "手环", "手環", "神经腕带", "腕带"]) or any(term in lowered for term in ["emg", "electromyography", "neural wristband"]):
        sources.extend([
            {
                "title": "Meta Quest Blog - Control Shift: Reality Labs sEMG research in Nature",
                "url": "https://www.meta.com/blog/quest/control-shift-new-reality-labs-research-semg-nature/",
                "snippet": "Meta Reality Labs discusses surface EMG neural wristband research and human-computer interaction implications.",
            },
            {
                "title": "Nature - A generic non-invasive neuromotor interface for human-computer interaction",
                "url": "https://www.nature.com/articles/s41586-025-09128-4",
                "snippet": "Nature paper on Meta Reality Labs non-invasive neuromotor interface using surface EMG.",
            },
            {
                "title": "PMC review - Surface EMG electrodes and skin-electrode interface",
                "url": "https://www.ncbi.nlm.nih.gov/pmc/?term=surface+EMG+dry+electrode+skin+electrode+interface",
                "snippet": "Search entry for peer-reviewed literature on sEMG dry electrodes, impedance, sweat, durability, and skin-electrode interface.",
            },
            {
                "title": "Google Patents search - Meta/CTRL-Labs EMG wristband electrodes",
                "url": "https://patents.google.com/?q=(Meta+OR+CTRL-Labs)+EMG+wristband+electrode",
                "snippet": "Patent search entry for Meta/CTRL-Labs EMG wristband electrode structures and materials.",
            },
            {
                "title": "Google Patents search - diamond-like carbon EMG dry electrode",
                "url": "https://patents.google.com/?q=%22diamond-like+carbon%22+EMG+dry+electrode",
                "snippet": "Patent search entry for DLC/diamond-like carbon dry electrodes and EMG electrode material claims.",
            },
            {
                "title": "Google Scholar search - diamond-like carbon dry electrodes EMG",
                "url": "https://scholar.google.com/scholar?q=diamond-like+carbon+dry+electrode+EMG",
                "snippet": "Academic search entry for DLC dry electrodes, biocompatibility, impedance, and wearable sEMG.",
            },
        ])
    if any(term in industry for term in ["玻璃基板", "玻璃基板封装", "玻璃封装", "先进封装"]) or any(term in lowered for term in ["glass substrate", "glass core", "advanced packaging"]):
        sources.extend([
            {
                "title": "Intel newsroom - Glass substrates for advanced packaging",
                "url": "https://www.intel.com/content/www/us/en/newsroom/news/intel-unveils-industry-leading-glass-substrates.html",
                "snippet": "Intel official announcement on glass substrates for next-generation advanced packaging.",
            },
            {
                "title": "TSMC newsroom - advanced packaging and 2026 technology symposium search",
                "url": "https://pr.tsmc.com/english/search?keywords=advanced%20packaging%20glass%20substrate",
                "snippet": "TSMC official newsroom search entry for advanced packaging, CoWoS, glass substrate and latest company actions.",
            },
            {
                "title": "TSMC investor relations - annual reports and advanced packaging disclosures",
                "url": "https://investor.tsmc.com/english/annual-reports",
                "snippet": "TSMC annual reports and investor materials for capital spending, advanced packaging and technology roadmap verification.",
            },
            {
                "title": "TrendForce search - glass substrate advanced packaging",
                "url": "https://www.trendforce.com/searchNews?query=glass%20substrate%20advanced%20packaging",
                "snippet": "Industry news search entry for glass core substrates, TGV, panel-level packaging and supply chain moves.",
            },
            {
                "title": "Semiconductor Engineering search - glass substrate packaging",
                "url": "https://semiengineering.com/?s=glass+substrate+packaging",
                "snippet": "Technical industry search entry for glass substrate packaging, warpage, CTE, RDL and panel-level process issues.",
            },
            {
                "title": "Corning semiconductor packaging glass search",
                "url": "https://www.corning.com/worldwide/en/search.html?q=semiconductor%20packaging%20glass%20substrate",
                "snippet": "Corning official search entry for glass materials relevant to semiconductor packaging substrates.",
            },
        ])
    if not any(term in industry for term in ["硅光", "光芯片", "硅光芯片"]):
        return sources
    sources.extend([
        {
            "title": "Intel Silicon Photonics product overview",
            "url": "https://www.intel.com/content/www/us/en/products/details/network-io/silicon-photonics.html",
            "snippet": "Intel silicon photonics products and optical connectivity for data centers.",
        },
        {
            "title": "MarketsandMarkets Silicon Photonics Market report",
            "url": "https://www.marketsandmarkets.com/Market-Reports/silicon-photonics-116.html",
            "snippet": "Silicon photonics market size, growth, applications, and company coverage.",
        },
        {
            "title": "Grand View Research Silicon Photonics Market Size",
            "url": "https://www.grandviewresearch.com/industry-analysis/silicon-photonics-market",
            "snippet": "Silicon photonics market size and forecast by component and application.",
        },
        {
            "title": "IMARC Silicon Photonics Market Report",
            "url": "https://www.imarcgroup.com/silicon-photonics-market",
            "snippet": "Silicon photonics market trends, share, size and forecast.",
        },
        {
            "title": "Coherent datacom transceivers and silicon photonics context",
            "url": "https://www.coherent.com/networking/datacom-transceivers",
            "snippet": "Optical transceivers, datacom, silicon photonics adjacent product context.",
        },
        {
            "title": "Synopsys Silicon Photonics Design",
            "url": "https://www.synopsys.com/photonic-solutions.html",
            "snippet": "Photonic IC design tools and silicon photonics design workflow.",
        },
        {
            "title": "IEEE Silicon Photonics topic search",
            "url": "https://ieeexplore.ieee.org/search/searchresult.jsp?newsearch=true&queryText=silicon%20photonics",
            "snippet": "IEEE publications on silicon photonics and photonic integrated circuits.",
        },
        {
            "title": "OFC Conference silicon photonics technical program search",
            "url": "https://www.ofcconference.org/en-us/home/search/?searchtext=silicon%20photonics",
            "snippet": "Optical Fiber Communication Conference silicon photonics papers and sessions.",
        },
        {
            "title": "Yole Group silicon photonics market and technology analysis",
            "url": "https://www.yolegroup.com/product/report/silicon-photonics-2024/",
            "snippet": "Silicon photonics market, technology, and industry analysis from Yole Group.",
        },
        {
            "title": "LightCounting optical transceivers market research",
            "url": "https://www.lightcounting.com/",
            "snippet": "Optical transceiver market research relevant to silicon photonics demand and CPO adoption.",
        },
        {
            "title": "Cisco Silicon One and co-packaged optics context",
            "url": "https://www.cisco.com/c/en/us/solutions/silicon-one.html",
            "snippet": "Switch ASIC and networking context for co-packaged optics and optical interconnect adoption.",
        },
        {
            "title": "Broadcom optical networking products",
            "url": "https://www.broadcom.com/products/fiber-optic-modules-components",
            "snippet": "Fiber optic modules and components; relevant to optical interconnect and silicon photonics ecosystem.",
        },
        {
            "title": "Marvell data center optics and interconnect",
            "url": "https://www.marvell.com/products/optical-connectivity.html",
            "snippet": "Optical connectivity product portfolio for datacenter interconnect.",
        },
        {
            "title": "Lumentum cloud and networking optical products",
            "url": "https://www.lumentum.com/en/products/cloud-networking",
            "snippet": "Optical components and modules for cloud networking and datacenter demand.",
        },
        {
            "title": "Cadence photonic IC design",
            "url": "https://www.cadence.com/en_US/home/tools/custom-ic-analog-rf-design/photonic-ic-design.html",
            "snippet": "Photonic IC design flow and EDA tools relevant to silicon photonics design.",
        },
        {
            "title": "imec silicon photonics research",
            "url": "https://www.imec-int.com/en/expertise/silicon-photonics",
            "snippet": "Silicon photonics R&D platform and technology context.",
        },
        {
            "title": "TSMC silicon photonics platform context",
            "url": "https://www.tsmc.com/english/dedicatedFoundry/technology/silicon_photonics",
            "snippet": "Foundry silicon photonics platform context and process ecosystem.",
        },
        {
            "title": "中际旭创投资者关系公告入口",
            "url": "https://www.cninfo.com.cn/new/disclosure/stock?stockCode=300308&orgId=9900023121",
            "snippet": "A-share optical module company announcements; downstream demand and financial verification.",
        },
        {
            "title": "新易盛投资者关系公告入口",
            "url": "https://www.cninfo.com.cn/new/disclosure/stock?stockCode=300502&orgId=9900025257",
            "snippet": "A-share optical module company announcements; downstream datacom optics financial context.",
        },
        {
            "title": "光迅科技投资者关系公告入口",
            "url": "https://www.cninfo.com.cn/new/disclosure/stock?stockCode=002281&orgId=9900007421",
            "snippet": "Chinese optical device and module company announcements; optical chip/device ecosystem context.",
        },
    ])
    return sources


def unwrap_search_url(url: str) -> str:
    parsed = urlparse(url)
    if "duckduckgo.com" in parsed.netloc and parsed.query:
        uddg = parse_qs(parsed.query).get("uddg")
        if uddg:
            return unquote(uddg[0])
    if "google." in parsed.netloc and parsed.path == "/url":
        target = parse_qs(parsed.query).get("q") or parse_qs(parsed.query).get("url")
        if target:
            return unquote(target[0])
    return url


def google_news_rss_search(query: str) -> list[dict[str, str]]:
    response = requests.get(
        "https://news.google.com/rss/search",
        params={"q": query, "hl": "zh-CN", "gl": "CN", "ceid": "CN:zh-Hans"},
        headers={"User-Agent": "Mozilla/5.0", "Accept": "application/rss+xml, application/xml;q=0.9, */*;q=0.8"},
        timeout=8,
    )
    response.raise_for_status()
    root = ET.fromstring(response.content)
    items: list[dict[str, str]] = []
    for item in root.findall(".//item")[:8]:
        title = item.findtext("title") or ""
        link = item.findtext("link") or ""
        pub_date = item.findtext("pubDate") or ""
        source = item.findtext("source") or ""
        items.append({
            "title": title,
            "url": link,
            "snippet": f"Google News RSS; source={source}; pubDate={pub_date}",
        })
    return items


def arxiv_search(query: str) -> list[dict[str, str]]:
    terms = [term for term in re.findall(r"[A-Za-z][A-Za-z0-9\-]{2,}", query) if term.lower() not in {"and", "the", "with", "for", "latest", "company", "update"}]
    if not terms:
        return []
    query_l = query.lower()
    if not any(token in query_l for token in ["emg", "semg", "electromyography", "photonic", "substrate", "packaging", "glass", "electrode", "sensor", "semiconductor"]):
        return []
    search_query = " OR ".join(f"all:{term}" for term in terms[:5])
    response = requests.get(
        "http://export.arxiv.org/api/query",
        params={"search_query": search_query, "start": 0, "max_results": 5, "sortBy": "submittedDate", "sortOrder": "descending"},
        headers={"User-Agent": "IndustryScope/1.0"},
        timeout=8,
    )
    response.raise_for_status()
    root = ET.fromstring(response.content)
    ns = {"atom": "http://www.w3.org/2005/Atom"}
    items: list[dict[str, str]] = []
    for entry in root.findall("atom:entry", ns):
        title = " ".join((entry.findtext("atom:title", default="", namespaces=ns) or "").split())
        link = ""
        for candidate in entry.findall("atom:link", ns):
            if candidate.attrib.get("rel") == "alternate":
                link = candidate.attrib.get("href", "")
                break
        summary = " ".join((entry.findtext("atom:summary", default="", namespaces=ns) or "").split())
        haystack = f"{title} {summary}".lower()
        matched_terms = sum(1 for term in terms[:6] if term.lower() in haystack)
        must_have = [token for token in ["emg", "semg", "electromyography", "electrode", "wristband", "substrate", "packaging", "glass", "tgv", "photonic"] if token in query_l]
        has_must = not must_have or any(token in haystack for token in must_have)
        if matched_terms < 3 or not has_must:
            continue
        published = entry.findtext("atom:published", default="", namespaces=ns) or ""
        items.append({"title": title, "url": link, "snippet": f"arXiv published={published}; {summary[:320]}"})
    return items


def jina_search(query: str) -> list[dict[str, str]]:
    response = requests.get(
        f"https://s.jina.ai/{quote(query)}",
        headers={"User-Agent": "Mozilla/5.0", "Accept": "text/plain"},
        timeout=10,
    )
    response.raise_for_status()
    items: list[dict[str, str]] = []
    current: dict[str, str] = {}
    for raw_line in response.text.splitlines():
        line = raw_line.strip()
        title_match = re.match(r"^\[\d+\]\s+Title:\s*(.+)$", line)
        url_match = re.match(r"^\[\d+\]\s+URL Source:\s*(https?://\S+)$", line)
        desc_match = re.match(r"^\[\d+\]\s+Description:\s*(.+)$", line)
        if title_match:
            if current.get("url"):
                items.append(current)
            current = {"title": title_match.group(1), "url": "", "snippet": ""}
        elif url_match:
            current["url"] = url_match.group(1)
        elif desc_match:
            current["snippet"] = desc_match.group(1)
    if current.get("url"):
        items.append(current)
    return items


def duckduckgo_html_search(query: str) -> list[dict[str, str]]:
    response = requests.get(
        "https://duckduckgo.com/html/",
        params={"q": query},
        headers={"User-Agent": "Mozilla/5.0"},
        timeout=8,
    )
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")
    items: list[dict[str, str]] = []
    for result in soup.select(".result"):
        link = result.select_one("a.result__a")
        snippet = result.select_one(".result__snippet")
        if not link:
            continue
        items.append(
            {
                "title": link.get_text(" ", strip=True),
                "url": link.get("href", ""),
                "snippet": snippet.get_text(" ", strip=True) if snippet else "",
            }
        )
    return items


def bing_html_search(query: str) -> list[dict[str, str]]:
    response = requests.get(
        "https://www.bing.com/search",
        params={"q": query},
        headers={"User-Agent": "Mozilla/5.0"},
        timeout=8,
    )
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")
    items: list[dict[str, str]] = []
    for result in soup.select("li.b_algo"):
        link = result.select_one("h2 a") or result.select_one("a")
        snippet = result.select_one(".b_caption p") or result.select_one("p")
        if not link:
            continue
        items.append(
            {
                "title": link.get_text(" ", strip=True),
                "url": link.get("href", ""),
                "snippet": snippet.get_text(" ", strip=True) if snippet else "",
            }
        )
    return items


CONTENT_SELECTORS = (
    "#js_content",
    ".rich_media_content",
    ".rich_media_area_primary_inner",
    "article",
    "main",
    "[role='main']",
    ".article-content",
    ".article__content",
    ".article-body",
    ".article",
    ".post-content",
    ".entry-content",
    ".news-content",
    ".story-content",
    ".report-content",
    ".content-body",
    ".main-content",
    "#content",
)


def normalize_extracted_text(text: str, limit: int = 3200) -> str:
    text = html.unescape(text or "").replace("\xa0", " ")
    text = re.sub(r"在小说阅读器读本章|去阅读|在小说阅读器中沉浸阅读", "", text)
    text = re.sub(r"[ \t\r\f\v]{2,}", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    lines: list[str] = []
    previous = ""
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line == previous:
            continue
        lines.append(line)
        previous = line
    return "\n".join(lines).strip()[:limit]


def extracted_text_quality(text: str) -> float:
    if not text:
        return 0
    compact = re.sub(r"\s+", "", text)
    if len(compact) < 120:
        return 0
    paragraphs = len([line for line in text.splitlines() if len(line.strip()) >= 18])
    digits = len(re.findall(r"\d", text))
    chinese_chars = len(re.findall(r"[\u4e00-\u9fff]", text))
    alpha_words = len(re.findall(r"[A-Za-z]{4,}", text))
    source_markers = len(re.findall(r"来源|数据|公告|报告|年报|论文|市场|公司|政策|according|report|market|revenue", text, re.I))
    navigation_noise = len(
        re.findall(
            r"登录|注册|购物车|加入购物车|cookie|javascript|subscribe|newsletter|sign in|menu|privacy policy|all rights reserved",
            text,
            re.I,
        )
    )
    return len(compact) + paragraphs * 90 + min(digits, 80) * 2 + chinese_chars * 0.15 + alpha_words + source_markers * 60 - navigation_noise * 260


def extract_with_scrapling(html_text: str, url: str) -> tuple[str, str]:
    if ScraplingSelector is None:
        return "", ""
    try:
        page = ScraplingSelector(html_text, url=url)
    except Exception:
        return "", ""

    best_text = ""
    best_selector = ""
    best_score = 0.0
    for selector in CONTENT_SELECTORS:
        try:
            nodes = page.css(selector)
        except Exception:
            continue
        for node in nodes[:4]:
            try:
                text = normalize_extracted_text(str(node.get_all_text(separator="\n", strip=True)))
            except Exception:
                continue
            score = extracted_text_quality(text)
            if score > best_score:
                best_text = text
                best_selector = selector
                best_score = score
    if best_text and best_score > 160:
        return best_text, f"Scrapling Selector {best_selector}"
    return "", ""


def extract_with_trafilatura(html_text: str, url: str) -> tuple[str, str]:
    if trafilatura is None:
        return "", ""
    try:
        extracted = trafilatura.extract(
            html_text,
            url=url,
            include_comments=False,
            include_tables=True,
            favor_precision=True,
            output_format="txt",
        )
    except Exception:
        return "", ""
    text = normalize_extracted_text(extracted or "")
    if len(text) > 160:
        return text, "Trafilatura precision"
    return "", ""


def extract_with_beautifulsoup(html_text: str) -> tuple[str, str]:
    soup = BeautifulSoup(html_text, "html.parser")
    for tag in soup(["script", "style", "nav", "footer", "header", "aside", "noscript", "form", "button"]):
        tag.decompose()
    text = normalize_extracted_text(soup.get_text("\n", strip=True), limit=2400)
    if len(text) > 80:
        return text, "BeautifulSoup fallback"
    return "", ""


def fetch_source_text_with_method(url: str, timeout: int = 15) -> tuple[str, str]:
    if url.lower().split("?")[0].endswith((".pdf", ".xlsx", ".xls", ".docx", ".pptx")):
        return "", "binary document skipped"
    response = requests.get(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,text/plain;q=0.8,*/*;q=0.7",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        },
        timeout=timeout,
    )
    response.raise_for_status()
    content_type = response.headers.get("content-type", "")
    if "text/html" not in content_type and "text/plain" not in content_type:
        return "", f"unsupported content-type: {content_type or 'unknown'}"
    if not response.encoding or response.encoding.lower() in {"iso-8859-1", "ascii"}:
        response.encoding = response.apparent_encoding or "utf-8"

    candidates: list[tuple[float, str, str]] = []
    for text, method in (
        extract_with_scrapling(response.text, url),
        extract_with_trafilatura(response.text, url),
        extract_with_beautifulsoup(response.text),
    ):
        if text:
            candidates.append((extracted_text_quality(text), text, method))

    if not candidates:
        return "", "no readable text extracted"
    _, text, method = max(candidates, key=lambda item: item[0])
    return text, method


def fetch_source_text(url: str, timeout: int = 15) -> str:
    text, _ = fetch_source_text_with_method(url, timeout=timeout)
    return text


def build_source_context(sources: list[dict[str, str]], industry: str = "", per_source_chars: int = 2400) -> str:
    blocks: list[str] = []
    for idx, source in enumerate(sources, start=1):
        text = ""
        extraction_method = "not fetched"
        try:
            text, extraction_method = fetch_source_text_with_method(source["url"])
            text = text[:per_source_chars]
            if industry and text and source_relevance_score(industry, source.get("title", ""), source["url"], text) < 2:
                text = "正文抓取结果与行业关键词不匹配，已自动剔除；请仅把该来源作为候选入口，必要时人工打开 URL 核验。"
        except Exception:
            text = source.get("snippet", "")
            extraction_method = "fetch failed; search snippet only"
        snippet = source.get("snippet", "")
        citation = f"[S{idx} {source.get('title', source['url'])}]({source['url']})"
        block = (
            f"[S{idx}] {source.get('title', source['url'])}\n"
            f"URL: {source['url']}\n"
            f"来源类型: {classify_source_type(source['url'])}\n"
            f"必须使用的Markdown引用片段: {citation}\n"
            f"检索摘要: {snippet}\n"
            f"相关性评分: {source.get('relevance', '未评分')}（低于2的来源不会进入上下文）\n"
            f"权威性加分: {source_authority_score(source['url'])}\n"
            f"正文抽取方法: {extraction_method}\n"
            f"使用约束: {'公众号可作为产业线索，强结论必须再找一手/权威来源交叉验证。' if 'mp.weixin.qq.com' in urlparse(source['url']).netloc.lower() else '按来源类型决定证据强度。'}\n"
            f"网页正文节选: {text}\n"
        )
        blocks.append(block)
    return "\n---\n".join(blocks)


def call_chat_compatible(req: ReportRequest, api_key: str) -> tuple[str, list[dict[str, str]], dict[str, Any]]:
    default_sources = {"快速版": 8, "标准版": 16, "深度版": 24}.get(req.depth, 16)
    max_sources = min(req.max_local_sources, default_sources)
    sources = search_public_web(req, max_sources)
    source_context = build_source_context(sources, req.industry)
    client = OpenAI(
        api_key=api_key,
        base_url=req.base_url or None,
        timeout=req.timeout_seconds,
        max_retries=1,
    )
    depth = DEPTH_CONFIG.get(req.depth, DEPTH_CONFIG["标准版"])
    prompt = build_prompt(req)
    if source_context:
        prompt += f"""

以下是工具预先检索和抓取的公开来源。第一步必须做来源相关性审查：若来源与「{req.industry}」无关、是电商/歌词/视频/地图/登录页/论坛噪声，必须在引用审计中列为“剔除来源”，正文不得引用其事实。请优先使用相关来源，并在正文中使用这些 URL 做可点击引用。若某个重要结论无法由来源支持，必须标注“证据不足/低置信度”，但不要因为部分来源无效而终止全文；应输出“证据受限版研报”，把强结论降级为待核验假设。
微信公众号优先规则：若来源中包含 mp.weixin.qq.com，可优先用于发现中国市场产业线索、专家观点、公司动态和争议点；但在证据分级中必须标注为 B/C 或 C，所有市场规模、份额、融资估值、客户订单、全球第一/独家/垄断等强结论必须由公告、年报、政策原文、论文、权威数据机构或主流财经媒体交叉验证。
硬性要求：执行摘要每条尽量使用 2 个 Markdown 链接；每个关键表格的“来源”列必须使用 Markdown 链接或写“证据不足”；引用审计表的 URL 列必须使用 Markdown 链接。

{source_context}
"""
    else:
        prompt += """

本次没有成功抓取到外部来源。请不要编造 URL；请输出“证据受限版研报”，只能基于用户指定来源和你明确知道的稳定事实写作，所有关键数字、融资、份额、客户绑定关系必须显著标注“证据不足/待核验”，不得全文终止。
"""

    messages = [
        {"role": "system", "content": "你是严谨的行业研究员。只输出可交付 Markdown 研报，不要输出闲聊或代码块。"},
        {"role": "user", "content": prompt},
    ]
    params: dict[str, Any] = {
        "model": req.model,
        "messages": messages,
        "stream": False,
    }
    if req.provider == "DeepSeek":
        params["reasoning_effort"] = "high" if depth["reasoning_effort"] != "xhigh" else "max"
        if "flash" in req.model:
            params["extra_body"] = {"thinking": {"type": "disabled"}}
        else:
            params["extra_body"] = {"thinking": {"type": "enabled"}}
    try:
        response = client.chat.completions.create(**params)
    except Exception as exc:
        if req.provider != "DeepSeek" or "reasoning" not in str(exc).lower():
            raise
        params.pop("reasoning_effort", None)
        response = client.chat.completions.create(**params)
    text = response.choices[0].message.content or ""
    data = response.model_dump() if hasattr(response, "model_dump") else json.loads(response.model_dump_json())
    return text, sources, data


def extract_anthropic_text(data: dict[str, Any]) -> str:
    parts: list[str] = []
    for item in data.get("content", []) or []:
        if isinstance(item, dict):
            if item.get("type") == "text":
                parts.append(str(item.get("text", "")))
            elif "text" in item:
                parts.append(str(item.get("text", "")))
        elif isinstance(item, str):
            parts.append(item)
    return "\n".join(part for part in parts if part).strip()


def anthropic_model_fallbacks(model: str) -> list[str]:
    models = [model]
    if "[1M]" in model:
        models.append(model.replace("[1M]", ""))
    if model != "claude-opus-4-8":
        models.append("claude-opus-4-8")
    return list(dict.fromkeys(models))


def post_anthropic_message(
    url: str,
    headers: dict[str, str],
    payload: dict[str, Any],
    timeout_seconds: int,
) -> requests.Response:
    retry_statuses = {429, 500, 502, 503, 504}
    model_candidates = anthropic_model_fallbacks(str(payload.get("model", "")))
    last_response: requests.Response | None = None
    last_error: Exception | None = None

    for model_index, model in enumerate(model_candidates):
        payload["model"] = model
        attempts = 3 if model_index == 0 else 2
        for attempt in range(attempts):
            try:
                response = requests.post(url, headers=headers, json=payload, timeout=timeout_seconds)
                if response.status_code in {401, 403}:
                    bearer_headers = dict(headers)
                    bearer_headers.pop("x-api-key", None)
                    bearer_headers["authorization"] = f"Bearer {headers.get('x-api-key', '')}"
                    response = requests.post(url, headers=bearer_headers, json=payload, timeout=timeout_seconds)
                if response.status_code not in retry_statuses:
                    return response
                last_response = response
            except requests.RequestException as exc:
                last_error = exc
            time.sleep(min(2 ** attempt, 8))

    if last_response is not None:
        return last_response
    if last_error is not None:
        raise last_error
    raise RuntimeError("Anthropic compatible request failed before receiving a response.")


def build_chat_source_prompt(req: ReportRequest) -> tuple[str, list[dict[str, str]]]:
    default_sources = {"快速版": 8, "标准版": 16, "深度版": 24}.get(req.depth, 16)
    max_sources = min(req.max_local_sources, default_sources)
    sources = search_public_web(req, max_sources)
    source_context = build_source_context(sources, req.industry)
    prompt = build_prompt(req)
    if source_context:
        prompt += f"""

以下是工具预先检索和抓取的公开来源。第一步必须做来源相关性审查：若来源与「{req.industry}」无关、是电商/歌词/视频/地图/登录页/论坛噪声，必须在引用审计中列为“剔除来源”，正文不得引用其事实。请优先使用相关来源，并在正文中使用这些 URL 做可点击引用。若某个重要结论无法由来源支持，必须标注“证据不足/低置信度”，但不要因为部分来源无效而终止全文；应输出“证据受限版研报”，把强结论降级为待核验假设。
微信公众号优先规则：若来源中包含 mp.weixin.qq.com，可优先用于发现中国市场产业线索、专家观点、公司动态和争议点；但在证据分级中必须标注为 B/C 或 C，所有市场规模、份额、融资估值、客户订单、全球第一/独家/垄断等强结论必须由公告、年报、政策原文、论文、权威数据机构或主流财经媒体交叉验证。
硬性要求：执行摘要每条尽量使用 2 个 Markdown 链接；每个关键表格的“来源”列必须使用 Markdown 链接或写“证据不足”；引用审计表的 URL 列必须使用 Markdown 链接。

{source_context}
"""
    else:
        prompt += """

本次没有成功抓取到外部来源。请不要编造 URL；请输出“证据受限版研报”，只能基于用户指定来源和你明确知道的稳定事实写作，所有关键数字、融资、份额、客户绑定关系必须显著标注“证据不足/待核验”，不得全文终止。
"""
    return prompt, sources


def is_openai_routed_qweapi_model(model: str) -> bool:
    lowered = model.lower()
    return lowered.startswith(("gpt-", "o", "deepseek-", "gemini-", "grok-"))


def call_qweapi_openai(req: ReportRequest, api_key: str) -> tuple[str, list[dict[str, str]], dict[str, Any]]:
    prompt, sources = build_chat_source_prompt(req)
    client = OpenAI(
        api_key=api_key,
        base_url=(req.base_url or "https://qweapi.com").rstrip("/") + "/v1",
        timeout=req.timeout_seconds,
        max_retries=2,
    )
    messages = [
        {"role": "system", "content": "你是严谨的行业研究员。只输出可交付 Markdown 研报，不要输出闲聊或代码块。"},
        {"role": "user", "content": prompt},
    ]
    params: dict[str, Any] = {
        "model": req.model,
        "messages": messages,
        "stream": False,
        "max_tokens": {"快速版": 6000, "标准版": 12000, "深度版": 20000}.get(req.depth, 12000),
    }
    try:
        response = client.chat.completions.create(**params)
    except Exception as exc:
        if "max_tokens" not in str(exc).lower():
            raise
        params.pop("max_tokens", None)
        response = client.chat.completions.create(**params)
    text = response.choices[0].message.content or ""
    data = response.model_dump() if hasattr(response, "model_dump") else json.loads(response.model_dump_json())
    return text, sources, data


def call_anthropic_compatible(req: ReportRequest, api_key: str) -> tuple[str, list[dict[str, str]], dict[str, Any]]:
    if is_openai_routed_qweapi_model(req.model):
        return call_qweapi_openai(req, api_key)

    prompt, sources = build_chat_source_prompt(req)
    base_url = (req.base_url or "https://qweapi.com").rstrip("/")
    url = f"{base_url}/v1/messages"
    max_tokens = {"快速版": 6000, "标准版": 12000, "深度版": 20000}.get(req.depth, 12000)
    payload = {
        "model": req.model,
        "max_tokens": max_tokens,
        "temperature": 0.2,
        "system": "你是严谨的行业研究员。只输出可交付 Markdown 研报，不要输出闲聊或代码块。",
        "messages": [{"role": "user", "content": prompt}],
    }
    headers = {
        "content-type": "application/json",
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "anthropic-dangerous-direct-browser-access": "true",
    }
    response = post_anthropic_message(url, headers, payload, req.timeout_seconds)
    response.raise_for_status()
    data = response.json()
    data["_request_model_used"] = payload.get("model")
    text = extract_anthropic_text(data)
    return text, sources, data


def call_model(req: ReportRequest, api_key: str) -> tuple[str, list[dict[str, str]], dict[str, Any]]:
    if req.provider == "OpenAI Responses":
        return call_openai(req, api_key)
    if req.provider in {"Anthropic", "qweapi"}:
        return call_anthropic_compatible(req, api_key)
    return call_chat_compatible(req, api_key)


def collect_sources(data: Any) -> list[dict[str, str]]:
    seen: set[str] = set()
    out: list[dict[str, str]] = []

    def add(url: str | None, title: str | None = None, source_type: str | None = None) -> None:
        if not url or not isinstance(url, str) or not url.startswith(("http://", "https://")):
            return
        cleaned = url.strip()
        if cleaned in seen:
            return
        seen.add(cleaned)
        out.append({
            "title": (title or cleaned).strip()[:220],
            "url": cleaned,
            "type": (source_type or "").strip()[:80],
        })

    def walk(obj: Any) -> None:
        if isinstance(obj, dict):
            if obj.get("type") == "url_citation":
                add(obj.get("url"), obj.get("title"))
            if "url" in obj:
                add(obj.get("url"), obj.get("title") or obj.get("name"), obj.get("source_type") or obj.get("type"))
            for value in obj.values():
                walk(value)
        elif isinstance(obj, list):
            for item in obj:
                walk(item)

    walk(data)
    return out


def render_markdown_to_html(markdown_text: str) -> str:
    return markdown_lib.markdown(
        markdown_text,
        extensions=["tables", "fenced_code", "toc", "sane_lists", "nl2br"],
        output_format="html5",
    )


def ensure_clickable_source_section(markdown_text: str, sources: list[dict[str, str]] | None = None) -> str:
    sources = sources or []
    existing_links = re.findall(r"\[[^\]]+\]\(https?://[^)]+\)", markdown_text)
    if existing_links or not sources:
        return markdown_text
    lines = [
        "",
        "## 可点击来源清单（自动补充）",
        "",
        "正文未正确嵌入可点击引用。以下为本次检索/输入的来源，报告中的事实应基于这些链接重新核验：",
        "",
    ]
    for idx, source in enumerate(sources, start=1):
        title = source.get("title") or f"Source {idx}"
        url = source.get("url", "")
        if url.startswith(("http://", "https://")):
            lines.append(f"- S{idx}: [{title}]({url})")
    return markdown_text.rstrip() + "\n" + "\n".join(lines)


def report_quality(markdown_text: str, sources: list[dict[str, str]] | None = None) -> dict[str, Any]:
    links = re.findall(r"\[[^\]]+\]\(https?://[^)]+\)", markdown_text)
    non_clickable_refs = re.findall(r"\[[0-9一二三四五六七八九十]+[.\-、][^\]]+\]", markdown_text)
    headings = re.findall(r"^#{2,3}\s+", markdown_text, flags=re.MULTILINE)
    has_audit = "引用审计表" in markdown_text and ("证据强度" in markdown_text or "Source ID" in markdown_text)
    has_error_audit = "反错误审计" in markdown_text
    has_workflow_audit = "开源研究工作流自检" in markdown_text or "研究工作流自检" in markdown_text
    has_limitations = "待核验" in markdown_text or "研究局限" in markdown_text
    has_failure = "失败条件" in markdown_text
    has_scenarios = all(x in markdown_text.lower() for x in ["bull", "base", "bear"])
    has_multi_perspective = any(term in markdown_text for term in ["PE/VC", "产业方", "二级市场", "战略咨询", "技术评估", "客户", "采购方", "怀疑者", "空头"])
    has_deep_signals = any(term in markdown_text for term in ["深信号", "隐藏拐点", "关键材料", "关键工艺", "待核验假设"])
    has_recency_section = any(term in markdown_text for term in ["最新动作", "信息时效", "最近 180 天", "最近180天", "最近 90 天", "最近90天", "最近 30 天", "最近30天"])
    has_rejected_sources = any(term in markdown_text for term in ["剔除来源", "低相关来源", "来源相关性审查"])
    strong_patterns = ["全球\\s*第一", "独家", "垄断", "唯一", "确定性\\s*极高", "必然", "毁灭性", "订单\\s*排至", "市占率\\s*第一"]
    strong_term_hits = []
    for pattern in strong_patterns:
        if re.search(pattern, markdown_text, flags=re.IGNORECASE):
            strong_term_hits.append(pattern.replace("\\s*", ""))
    weak_sources = ["wikipedia", "网易", "163.com", "雪球", "头条", "观察者网", "Wccftech", "reddit", "自媒体", "微信公众号", "公众号", "mp.weixin.qq.com"]
    weak_source_hits = [term for term in weak_sources if term.lower() in markdown_text.lower()]
    mouthful_terms = ["口径", "分母", "统计对象", "是否预测", "单一来源", "证据不足"]
    evidence_hygiene_hits = [term for term in mouthful_terms if term in markdown_text]
    warnings: list[str] = []
    if len(links) < 10:
        warnings.append("可点击引用偏少，建议补充更多一手来源。")
    if non_clickable_refs:
        warnings.append("检测到不可点击脚注样式，需改为 Markdown 链接。")
    if sources and not links:
        warnings.append("模型没有把检索来源写入正文，已自动补充来源清单；建议重试或缩小研究范围。")
    if not has_audit:
        warnings.append("未检测到完整引用审计表。")
    if not has_error_audit:
        warnings.append("未检测到反错误审计小节。")
    if not has_workflow_audit:
        warnings.append("未检测到开源研究工作流自检。")
    if not has_limitations:
        warnings.append("未检测到研究局限/待核验清单。")
    if not has_failure:
        warnings.append("执行摘要可能缺少失败条件。")
    if not has_scenarios:
        warnings.append("投资结论可能缺少 bull/base/bear 情景。")
    if strong_term_hits and len(links) < max(12, len(strong_term_hits) * 2):
        warnings.append("检测到强结论词，但引用密度不足，需降级或补一手来源。")
    if weak_source_hits and "证据强度" not in markdown_text:
        warnings.append("检测到弱来源名称，但缺少证据强度标注。")
    if not has_multi_perspective:
        warnings.append("多立场审视不足，建议同时覆盖 PE/VC、产业方、二级市场、战略咨询、技术评估、客户/采购方、怀疑者视角。")
    if not has_deep_signals:
        warnings.append("未检测到深信号/隐藏拐点小节，可能漏掉材料、工艺、专利、客户认证等关键变量。")
    if not has_recency_section:
        warnings.append("未检测到最新动作/信息时效小节，可能漏掉最近 30/90/180 天更新。")
    if sources and not has_rejected_sources:
        warnings.append("未检测到来源相关性审查或剔除来源说明。")
    if len(evidence_hygiene_hits) < 3:
        warnings.append("证据卫生不足：建议补充口径、分母、单一来源/证据不足说明。")
    return {
        "links": len(links),
        "non_clickable_refs": len(non_clickable_refs),
        "headings": len(headings),
        "api_sources": len(sources or []),
        "has_audit": has_audit,
        "has_error_audit": has_error_audit,
        "has_workflow_audit": has_workflow_audit,
        "has_limitations": has_limitations,
        "has_failure_conditions": has_failure,
        "has_scenarios": has_scenarios,
        "has_multi_perspective": has_multi_perspective,
        "has_deep_signals": has_deep_signals,
        "has_recency_section": has_recency_section,
        "has_rejected_sources": has_rejected_sources,
        "strong_term_hits": strong_term_hits,
        "weak_source_hits": weak_source_hits,
        "evidence_hygiene_hits": evidence_hygiene_hits,
        "warnings": warnings,
    }


def quality_html(quality: dict[str, Any]) -> str:
    warnings = quality.get("warnings", [])
    warning_text = "；".join(html.escape(w) for w in warnings) if warnings else "未发现明显结构缺口。"
    return (
        f"<strong>质量检查</strong>："
        f"可点击引用 {quality.get('links', 0)} 个，"
        f"不可点击脚注 {quality.get('non_clickable_refs', 0)} 个，"
        f"API 来源 {quality.get('api_sources', 0)} 个，"
        f"标题层级 {quality.get('headings', 0)} 个。"
        f"<br><strong>提示</strong>：{warning_text}"
    )


def render_report_html(markdown_text: str, req: ReportRequest, sources: list[dict[str, str]] | None = None) -> str:
    markdown_text = ensure_clickable_source_section(markdown_text, sources)
    body_html = render_markdown_to_html(markdown_text)
    quality = report_quality(markdown_text, sources)
    meta_items = [
        {"label": "行业", "value": req.industry},
        {"label": "研究范围", "value": req.region},
        {"label": "覆盖视角", "value": req.stance},
        {"label": "报告深度", "value": req.depth},
        {"label": "模型", "value": req.model},
        {"label": "生成时间", "value": datetime.now().strftime("%Y-%m-%d %H:%M")},
    ]
    template = Template(HTML_TEMPLATE)
    return template.render(
        title=f"{req.industry}深度研究报告",
        generated_at=datetime.now().strftime("%Y-%m-%d %H:%M"),
        meta_items=meta_items,
        body_html=body_html,
        quality_html=quality_html(quality),
    )


def filename_safe(value: str) -> str:
    safe = re.sub(r"[^\w\u4e00-\u9fff.-]+", "_", value.strip())
    return safe.strip("_") or "industry_report"


def sample_report(req: ReportRequest) -> tuple[str, list[dict[str, str]]]:
    title = req.industry or "具身智能"
    links = [
        ("OpenAI Web Search docs", "https://platform.openai.com/docs/guides/tools-web-search?api-mode=responses"),
        ("Figure AI Series C", "https://www.figure.ai/news/series-c?_bhlid=98989db8374486c797bc6890cb968ab23be5a6e9"),
        ("FIA 2025 Global Fusion Industry Report", "https://www.fusionindustryassociation.org/wp-content/uploads/2025/07/2025-Global-Fusion-Industry-Report.pdf"),
        ("Omdia Market Radar PDF", "https://www.zhiyuan-robot.com/public/uploads/file/Omdia%20Market%20Radar%20-%20General-purpose%20Embodied%20Intelligent%20Robot%20-%202026.pdf"),
    ]
    markdown_text = f"""# {title}深度研究报告

## 封面与元信息
- 报告日期：{datetime.now().strftime("%Y-%m-%d")}
- 数据截止：示例模式，未进行实时完整检索
- 研究范围：{req.region}
- 覆盖视角：{req.stance}
- 报告版本：demo
- 免责声明：本示例用于预览工具结构，不构成投资建议。

## 研究边界与多立场框架
- 核心定义：围绕“{title}”的技术、产品、产业链、竞争格局与投资价值进行分析。
- 包含范围：核心技术路线、关键公司、上游资源、下游应用、政策与资本市场信号。
- 排除范围：与该行业弱相关的泛概念、无法核验的数据和无来源预测。
- 本报告要回答的核心问题：行业处于什么阶段，瓶颈在哪里，谁掌握利润池，未来 6-24 个月看什么指标。
- 多立场覆盖：PE/VC 看投资窗口和退出，产业方看供应链和客户导入，二级市场看财务弹性，战略咨询看竞争位置，技术评估看材料/工艺/良率，客户/采购方看可靠性和认证，怀疑者看反证。

## 深信号与隐藏拐点
| 深信号候选 | 所属层级 | 为什么重要 | 当前证据 | 下一步核验 |
|---|---|---|---|---|
| 关键材料/界面 | 材料/工艺 | 可能决定性能、寿命、良率和客户认证 | 示例模式不检索 | 正式模式会强制检索材料、专利、论文、拆解和供应链 |
| 客户认证周期 | 商业化 | 决定收入兑现节奏 | 示例模式不检索 | 正式模式会检索公告、访谈、招聘、招标和客户导入 |

## 最新动作与信息时效
示例模式不进行实时检索。正式模式会优先检查最近 180/90/30 天的新动作，并以实时来源覆盖模型内置知识。

## 执行摘要
1. **结论**：优秀行研的第一步不是写观点，而是先锁定边界与口径。**证据**：OpenAI 的 web search 工具要求最终展示给用户的 URL citation 清晰可见且可点击，见 [OpenAI Web Search docs]({links[0][1]})。**置信度**：高。**失败条件**：如果目标部署环境不能访问实时网页，需要改用私有资料库或文件上传。
2. **结论**：融资和估值类结论应优先引用公司公告。**证据**：Figure AI 官方公告披露 Series C 超 10 亿美元、投后估值 390 亿美元，见 [Figure AI Series C]({links[1][1]})。**置信度**：高。**失败条件**：若公司公告只披露定性信息，需用监管文件或可信媒体补充。
3. **结论**：行业协会报告适合做行业总量和融资趋势基准。**证据**：FIA 发布的 2025 全球聚变产业报告提供私人投资累计、年度新增投资等指标，见 [FIA PDF]({links[2][1]})。**置信度**：中高。**失败条件**：协会样本口径可能偏向会员或披露公司。

## 技术/产业历史沿革
| 时间 | 里程碑事件 | 关键机构/人物 | 突破 | 来源 |
|---|---|---|---|---|
| 早期 | 概念验证 | 学术界/产业先行者 | 确认技术可行性 | 用户需实时生成 |
| 成长期 | 产品化探索 | 创业公司/产业资本 | 成本下降、应用场景扩大 | 用户需实时生成 |
| 当前 | 商业化分化 | 头部公司/供应链 | 从“能做”转向“能规模赚钱” | 用户需实时生成 |

## 现状与核心瓶颈
| 瓶颈 | 当前水平 | 目标水平 | 突破难度 | 成因 | 来源 |
|---|---|---|---|---|---|
| 数据口径 | 多来源混杂 | 统一时间、地区、单位 | 中 | 机构统计口径不同 | [OpenAI Web Search docs]({links[0][1]}) |
| 证据强度 | 二手转述较多 | 一手来源优先 | 中 | 官方数据披露滞后 | [FIA PDF]({links[2][1]}) |
| 商业化 | 预测多、兑现少 | 量产/收入/毛利验证 | 高 | 技术成熟度与需求不匹配 | 用户需实时生成 |

## 市场规模与需求驱动
示例模式不直接编写未经检索的市场规模数字。正式生成时，工具会要求模型分别给出 TAM/SAM/SOM、历史规模、预测规模、CAGR、来源口径和冲突口径。

## 技术路线或产品路线对比
| 路线 | 成熟度 | 优势 | 短板 | 代表公司 | 二阶问题 |
|---|---|---|---|---|---|
| 路线 A | 中 | 成本可控 | 性能瓶颈 | 待检索 | 是否能跨越主流客户认证 |
| 路线 B | 低-中 | 性能上限高 | 供应链不稳 | 待检索 | 是否只是实验室领先 |
| 路线 C | 高 | 产业链成熟 | 利润率下降 | 待检索 | 是否会被新路线替代 |

## 竞争格局与代表企业
正式模式会对全球、中国、上游、中游、下游分别列出代表企业，并尽量使用公司公告、年报、招股书和权威数据库。

## 产业链图谱与价值分配
上游关注资源、材料、设备和核心零部件；中游关注产品集成、软件、平台与工程化；下游关注客户预算、采购周期、认证壁垒与替代方案。

## 成本结构与经济性
正式报告会拆解 BOM、单位经济模型、价格趋势和敏感性变量，并标注哪些数据是公司披露、哪些是券商估算。

## 政策、监管与地缘因素
正式报告会列出相关政策、标准、出口管制、准入许可、补贴和合规风险。

## 风险、机会与领先指标
- 技术风险：关键性能无法达到主流客户要求。
- 需求风险：下游预算周期低于市场预期。
- 供给风险：核心设备、材料或人才受限。
- 资本风险：估值提前透支商业化。
- 领先指标：订单、认证、良率、毛利率、客户复购、政策审批、头部客户导入。

## 投资/战略结论
| 情景 | 判断 | 触发条件 |
|---|---|---|
| bull | 技术和需求同时兑现，利润池向关键瓶颈环节集中 | 头部客户批量采购，毛利率稳定 |
| base | 行业保持增长，但公司分化明显 | 订单增长但价格竞争加剧 |
| bear | 技术落地慢于预期或供给快速过剩 | 产能释放快于需求，库存上升 |

## 引用审计表
| Source ID | 来源标题 | URL | 来源类型 | 支持的关键结论 | 时间 | 证据强度 | 风险 |
|---|---|---|---|---|---|---|---|
| S1 | OpenAI Web Search docs | [链接]({links[0][1]}) | 官方文档 | 工具接口和引用要求 | 访问于 {datetime.now().strftime("%Y-%m-%d")} | 高 | 产品文档会更新 |
| S2 | Figure AI Series C | [链接]({links[1][1]}) | 公司公告 | 融资与估值信息优先用一手公告 | 2025 | 高 | 公司披露可能选择性 |
| S3 | FIA 2025 Global Fusion Industry Report | [链接]({links[2][1]}) | 行业协会 | 行业融资统计适合做基准 | 2025 | 中高 | 样本口径需复核 |
| S4 | Omdia Market Radar PDF | [链接]({links[3][1]}) | 数据机构/报告 | 机构口径需要单独标注 | 2026 | 中高 | 与其他机构口径可能不同 |
"""
    sources = [{"title": title, "url": url, "type": "demo"} for title, url in links]
    return markdown_text, sources


def request_from_session(data: dict[str, Any]) -> ReportRequest:
    data = dict(data)
    data.pop("stance", None)
    return ReportRequest(**data)


def request_to_json(req: ReportRequest) -> str:
    return json.dumps(asdict(req), ensure_ascii=False, indent=2)


def config_value(name: str, default: str = "") -> str:
    if st is not None:
        try:
            value = st.secrets.get(name, "")
            if value:
                return str(value).strip()
        except Exception:
            pass
    return os.getenv(name, default).strip()


def get_api_key(manual_key: str | None = None) -> str:
    return (manual_key or "").strip() or config_value("OPENAI_API_KEY")


def get_provider_api_key(provider: str, manual_key: str | None = None) -> str:
    if provider in {"Anthropic", "qweapi"}:
        return (
            (manual_key or "").strip()
            or config_value("QWEAPI_AUTH_TOKEN")
            or config_value("ANTHROPIC_AUTH_TOKEN")
        )
    env_map = {
        "OpenAI Responses": "OPENAI_API_KEY",
        "DeepSeek": "DEEPSEEK_API_KEY",
        "OpenAI兼容": "OPENAI_COMPAT_API_KEY",
    }
    env_name = env_map.get(provider, "OPENAI_API_KEY")
    return (manual_key or "").strip() or config_value(env_name)
