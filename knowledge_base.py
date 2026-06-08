from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
from typing import Any

from bs4 import BeautifulSoup

try:
    import fitz  # PyMuPDF
except ImportError:
    fitz = None

try:
    import docx
except ImportError:
    docx = None

try:
    import pandas as pd
except ImportError:
    pd = None


KB_ROOT = Path(os.getenv("INDUSTRYSCOPE_KB_DIR", "data/knowledge_base"))
KB_FILES_DIR = KB_ROOT / "files"
KB_INDEX_DIR = KB_ROOT / "index"
KB_CHUNKS_PATH = KB_INDEX_DIR / "chunks.jsonl"
KB_DOCS_PATH = KB_INDEX_DIR / "documents.json"


SOURCE_TYPE_TIERS = {
    "公司公告/年报/招股书": "T0",
    "专利报告/专利地图": "T1",
    "论文/会议": "T0",
    "专家纪要/调研纪要": "T1",
    "券商/投行/咨询研报": "T1",
    "行业白皮书/协会报告": "T1",
    "内部笔记": "T2",
    "公众号/媒体转载": "T3",
    "其他": "T2",
}


@dataclass
class KBDocument:
    doc_id: str
    title: str
    filename: str
    stored_path: str
    source_type: str
    source_tier: str
    source_org: str
    publish_date: str
    industry_tags: str
    company_tags: str
    technology_tags: str
    created_at: str
    chunk_count: int


@dataclass
class KBChunk:
    chunk_id: str
    doc_id: str
    title: str
    filename: str
    source_type: str
    source_tier: str
    source_org: str
    publish_date: str
    industry_tags: str
    company_tags: str
    technology_tags: str
    page: str
    section: str
    text: str
    token_hint: int


def ensure_kb_dirs() -> None:
    KB_FILES_DIR.mkdir(parents=True, exist_ok=True)
    KB_INDEX_DIR.mkdir(parents=True, exist_ok=True)
    if not KB_DOCS_PATH.exists():
        KB_DOCS_PATH.write_text("{}", encoding="utf-8")
    if not KB_CHUNKS_PATH.exists():
        KB_CHUNKS_PATH.write_text("", encoding="utf-8")


def filename_safe(value: str) -> str:
    safe = re.sub(r"[^\w\u4e00-\u9fff.-]+", "_", value.strip())
    return safe.strip("_") or "document"


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def normalize_text(text: str) -> str:
    text = (text or "").replace("\xa0", " ")
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    lines: list[str] = []
    prev = ""
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line == prev:
            continue
        lines.append(line)
        prev = line
    return "\n".join(lines).strip()


def extract_pdf(path: Path) -> list[dict[str, str]]:
    if fitz is None:
        raise RuntimeError("PyMuPDF 未安装，无法解析 PDF。")
    pages: list[dict[str, str]] = []
    with fitz.open(path) as doc:
        for index, page in enumerate(doc, start=1):
            text = normalize_text(page.get_text("text"))
            if text:
                pages.append({"page": str(index), "section": "", "text": text})
    return pages


def extract_docx(path: Path) -> list[dict[str, str]]:
    if docx is None:
        raise RuntimeError("python-docx 未安装，无法解析 DOCX。")
    document = docx.Document(str(path))
    parts: list[str] = []
    current_heading = ""
    sections: list[dict[str, str]] = []
    for para in document.paragraphs:
        text = para.text.strip()
        if not text:
            continue
        style = (para.style.name or "").lower() if para.style else ""
        if "heading" in style and parts:
            sections.append({"page": "", "section": current_heading, "text": normalize_text("\n".join(parts))})
            parts = []
            current_heading = text
        elif "heading" in style:
            current_heading = text
        else:
            parts.append(text)
    if parts:
        sections.append({"page": "", "section": current_heading, "text": normalize_text("\n".join(parts))})
    return sections or [{"page": "", "section": "", "text": normalize_text("\n".join(p.text for p in document.paragraphs))}]


def extract_spreadsheet(path: Path) -> list[dict[str, str]]:
    if pd is None:
        raise RuntimeError("pandas/openpyxl 未安装，无法解析表格。")
    sheets = pd.read_excel(path, sheet_name=None) if path.suffix.lower() in {".xlsx", ".xls"} else {"csv": pd.read_csv(path)}
    sections: list[dict[str, str]] = []
    for sheet_name, frame in sheets.items():
        text = frame.fillna("").astype(str).to_csv(index=False)
        text = normalize_text(text)
        if text:
            sections.append({"page": "", "section": str(sheet_name), "text": text})
    return sections


def extract_html(path: Path) -> list[dict[str, str]]:
    soup = BeautifulSoup(path.read_text(encoding="utf-8", errors="ignore"), "html.parser")
    for tag in soup(["script", "style", "nav", "footer", "header", "aside", "form"]):
        tag.decompose()
    return [{"page": "", "section": "", "text": normalize_text(soup.get_text("\n", strip=True))}]


def extract_plain(path: Path) -> list[dict[str, str]]:
    return [{"page": "", "section": "", "text": normalize_text(path.read_text(encoding="utf-8", errors="ignore"))}]


def extract_document(path: Path) -> list[dict[str, str]]:
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return extract_pdf(path)
    if suffix == ".docx":
        return extract_docx(path)
    if suffix in {".xlsx", ".xls", ".csv"}:
        return extract_spreadsheet(path)
    if suffix in {".html", ".htm"}:
        return extract_html(path)
    if suffix in {".md", ".txt"}:
        return extract_plain(path)
    raise RuntimeError(f"暂不支持的文件类型：{suffix}")


def split_chunk_text(text: str, target_chars: int = 1100, overlap_chars: int = 160) -> list[str]:
    text = normalize_text(text)
    if not text:
        return []
    paragraphs = re.split(r"\n{2,}", text)
    chunks: list[str] = []
    current = ""
    for para in paragraphs:
        para = para.strip()
        if not para:
            continue
        if len(current) + len(para) + 2 <= target_chars:
            current = f"{current}\n\n{para}".strip()
        else:
            if current:
                chunks.append(current)
            if len(para) <= target_chars:
                current = para
            else:
                start = 0
                while start < len(para):
                    chunks.append(para[start:start + target_chars])
                    start += max(1, target_chars - overlap_chars)
                current = ""
    if current:
        chunks.append(current)
    if overlap_chars > 0 and len(chunks) > 1:
        overlapped: list[str] = []
        previous_tail = ""
        for chunk in chunks:
            combined = f"{previous_tail}\n{chunk}".strip() if previous_tail else chunk
            overlapped.append(combined[: target_chars + overlap_chars])
            previous_tail = chunk[-overlap_chars:]
        chunks = overlapped
    return chunks


def load_documents() -> dict[str, dict[str, Any]]:
    ensure_kb_dirs()
    try:
        return json.loads(KB_DOCS_PATH.read_text(encoding="utf-8") or "{}")
    except json.JSONDecodeError:
        return {}


def save_documents(docs: dict[str, dict[str, Any]]) -> None:
    ensure_kb_dirs()
    KB_DOCS_PATH.write_text(json.dumps(docs, ensure_ascii=False, indent=2), encoding="utf-8")


def load_chunks() -> list[dict[str, Any]]:
    ensure_kb_dirs()
    chunks: list[dict[str, Any]] = []
    for line in KB_CHUNKS_PATH.read_text(encoding="utf-8").splitlines():
        if line.strip():
            try:
                chunks.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return chunks


def save_chunks(chunks: list[dict[str, Any]]) -> None:
    ensure_kb_dirs()
    KB_CHUNKS_PATH.write_text("\n".join(json.dumps(chunk, ensure_ascii=False) for chunk in chunks), encoding="utf-8")


def add_document(
    source_path: Path,
    title: str = "",
    source_type: str = "其他",
    source_org: str = "",
    publish_date: str = "",
    industry_tags: str = "",
    company_tags: str = "",
    technology_tags: str = "",
) -> KBDocument:
    ensure_kb_dirs()
    digest = file_sha256(source_path)
    doc_id = digest[:16]
    suffix = source_path.suffix.lower()
    stored_name = f"{doc_id}_{filename_safe(source_path.stem)}{suffix}"
    stored_path = KB_FILES_DIR / stored_name
    if source_path.resolve() != stored_path.resolve():
        shutil.copyfile(source_path, stored_path)

    docs = load_documents()
    existing_chunks = [chunk for chunk in load_chunks() if chunk.get("doc_id") != doc_id]
    sections = extract_document(stored_path)
    source_tier = SOURCE_TYPE_TIERS.get(source_type, "T2")
    doc_title = title.strip() or source_path.stem
    chunks: list[dict[str, Any]] = []
    chunk_index = 0
    for section in sections:
        for text in split_chunk_text(section.get("text", "")):
            chunk_index += 1
            chunk_id = f"{doc_id}-{chunk_index:04d}"
            chunk = KBChunk(
                chunk_id=chunk_id,
                doc_id=doc_id,
                title=doc_title,
                filename=stored_name,
                source_type=source_type,
                source_tier=source_tier,
                source_org=source_org,
                publish_date=publish_date,
                industry_tags=industry_tags,
                company_tags=company_tags,
                technology_tags=technology_tags,
                page=section.get("page", ""),
                section=section.get("section", ""),
                text=text,
                token_hint=max(1, len(text) // 2),
            )
            chunks.append(asdict(chunk))

    document = KBDocument(
        doc_id=doc_id,
        title=doc_title,
        filename=stored_name,
        stored_path=str(stored_path),
        source_type=source_type,
        source_tier=source_tier,
        source_org=source_org,
        publish_date=publish_date,
        industry_tags=industry_tags,
        company_tags=company_tags,
        technology_tags=technology_tags,
        created_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        chunk_count=len(chunks),
    )
    docs[doc_id] = asdict(document)
    save_documents(docs)
    save_chunks(existing_chunks + chunks)
    return document


def delete_document(doc_id: str) -> None:
    docs = load_documents()
    doc = docs.pop(doc_id, None)
    if doc:
        try:
            Path(doc.get("stored_path", "")).unlink(missing_ok=True)
        except Exception:
            pass
    save_documents(docs)
    save_chunks([chunk for chunk in load_chunks() if chunk.get("doc_id") != doc_id])


def tokenize_query(text: str) -> list[str]:
    text = (text or "").lower()
    tokens = re.findall(r"[\u4e00-\u9fff]{2,}|[a-zA-Z][a-zA-Z0-9\-]{2,}", text)
    expanded: list[str] = []
    for token in tokens:
        expanded.append(token)
        if re.fullmatch(r"[\u4e00-\u9fff]{4,}", token):
            expanded.extend(token[i:i + 2] for i in range(len(token) - 1))
    stop = {"行业", "研究", "报告", "市场", "分析", "the", "and", "for", "with"}
    return [token for token in expanded if token not in stop]


def date_score(value: str) -> float:
    match = re.search(r"(20\d{2})", value or "")
    if not match:
        return 0.0
    year = int(match.group(1))
    return max(0.0, min(6.0, (year - 2020) * 1.2))


def tier_score(tier: str) -> float:
    return {"T0": 16.0, "T1": 12.0, "T2": 7.0, "T3": 2.0}.get(tier, 4.0)


def search_knowledge_base(query: str, top_k: int = 12, filters: dict[str, str] | None = None) -> list[dict[str, Any]]:
    filters = filters or {}
    query_tokens = tokenize_query(query)
    if not query_tokens:
        return []
    query_terms = set(query_tokens)
    chunks = load_chunks()
    scored: list[dict[str, Any]] = []
    for chunk in chunks:
        haystack = " ".join(
            str(chunk.get(key, ""))
            for key in ["title", "source_org", "industry_tags", "company_tags", "technology_tags", "section", "text"]
        ).lower()
        if filters.get("source_tier") and chunk.get("source_tier") != filters["source_tier"]:
            continue
        if filters.get("source_type") and chunk.get("source_type") != filters["source_type"]:
            continue
        hits = 0
        dense_hits = 0
        for term in query_terms:
            count = haystack.count(term.lower())
            if count:
                hits += 1
                dense_hits += min(count, 5)
        if hits == 0:
            continue
        coverage = hits / max(1, len(query_terms))
        phrase_bonus = 10.0 if query.lower() in haystack else 0.0
        title_bonus = 5.0 if any(term in str(chunk.get("title", "")).lower() for term in query_terms) else 0.0
        tag_bonus = 4.0 if any(term in f"{chunk.get('industry_tags', '')} {chunk.get('company_tags', '')} {chunk.get('technology_tags', '')}".lower() for term in query_terms) else 0.0
        score = coverage * 45 + math.log1p(dense_hits) * 10 + tier_score(str(chunk.get("source_tier", ""))) + date_score(str(chunk.get("publish_date", ""))) + phrase_bonus + title_bonus + tag_bonus
        result = dict(chunk)
        result["score"] = round(score, 2)
        result["match_coverage"] = round(coverage, 2)
        result["matched_terms"] = ", ".join(sorted(term for term in query_terms if term in haystack)[:12])
        scored.append(result)
    scored.sort(key=lambda item: item["score"], reverse=True)
    return scored[:top_k]


def kb_stats() -> dict[str, Any]:
    docs = load_documents()
    chunks = load_chunks()
    type_counts: dict[str, int] = {}
    tier_counts: dict[str, int] = {}
    for doc in docs.values():
        type_counts[doc.get("source_type", "其他")] = type_counts.get(doc.get("source_type", "其他"), 0) + 1
        tier_counts[doc.get("source_tier", "T2")] = tier_counts.get(doc.get("source_tier", "T2"), 0) + 1
    return {
        "documents": len(docs),
        "chunks": len(chunks),
        "type_counts": type_counts,
        "tier_counts": tier_counts,
        "root": str(KB_ROOT),
    }


def export_kb_json() -> str:
    return json.dumps({"documents": load_documents(), "chunks": load_chunks()}, ensure_ascii=False, indent=2)


def kb_results_to_sources(results: list[dict[str, Any]]) -> list[dict[str, str]]:
    sources: list[dict[str, str]] = []
    for idx, item in enumerate(results, start=1):
        page = f"p.{item.get('page')}" if item.get("page") else item.get("section", "")
        title = item.get("title") or item.get("filename") or f"KB Source {idx}"
        label = f"{title} {page}".strip()
        chunk_id = item.get("chunk_id", "")
        sources.append({
            "title": label,
            "url": f"kb://{chunk_id}",
            "snippet": str(item.get("text", ""))[:500],
            "type": "knowledge_base",
            "source_tier": item.get("source_tier", "T2"),
            "source_channel": "专属知识库",
            "density_band": "高" if float(item.get("score", 0)) >= 55 else "中",
            "evidence_density": str(int(float(item.get("score", 0)))),
            "relevance": str(int(float(item.get("score", 0)) // 10)),
            "use_policy": "可作为私有知识库证据；重大强结论仍需与一手公开来源或其他知识库文件交叉验证。",
            "kb_chunk_id": chunk_id,
            "kb_doc_id": item.get("doc_id", ""),
            "kb_filename": item.get("filename", ""),
            "kb_page": item.get("page", ""),
            "kb_section": item.get("section", ""),
        })
    return sources
