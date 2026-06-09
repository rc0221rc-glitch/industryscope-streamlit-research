from __future__ import annotations

import os
import re
import tempfile
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import requests

from knowledge_base import add_document

try:
    import streamlit as st
except ImportError:
    st = None


QUARK_BASE = "https://drive-pc.quark.cn/1/clouddrive"
SUPPORTED_SUFFIXES = {".pdf", ".docx", ".md", ".txt", ".html", ".htm", ".xlsx", ".xls", ".csv"}


@dataclass
class QuarkShareInfo:
    pwd_id: str
    passcode: str
    stoken: str
    title: str
    size: int
    file_count: int


@dataclass
class QuarkFile:
    fid: str
    token: str
    name: str
    path: str
    size: int
    suffix: str
    format_type: str
    updated_at: str
    is_dir: bool = False


def config_value(name: str, default: str = "") -> str:
    if st is not None:
        try:
            value = st.secrets.get(name, "")
            if value:
                return str(value).strip()
        except Exception:
            pass
    return os.getenv(name, default).strip()


def parse_quark_share(text: str, passcode: str = "") -> tuple[str, str]:
    url_match = re.search(r"https://pan\.quark\.cn/s/([A-Za-z0-9]+)", text or "")
    pwd_id = url_match.group(1) if url_match else (text or "").strip()
    code = passcode.strip()
    if not code:
        code_match = re.search(r"(?:提取码|pwd|passcode|密码)[:：= ]+([A-Za-z0-9]+)", text or "", flags=re.I)
        if code_match:
            code = code_match.group(1)
        else:
            parsed = urlparse(text or "")
            query_pwd = parse_qs(parsed.query).get("pwd", [""])[0]
            code = query_pwd.strip()
    return pwd_id, code


def make_session(cookie: str = "") -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0 Safari/537.36",
            "Accept": "application/json, text/plain, */*",
            "Origin": "https://pan.quark.cn",
        }
    )
    cookie = cookie or config_value("QUARK_COOKIE")
    if cookie:
        session.headers["Cookie"] = cookie
    return session


def quark_params() -> dict[str, str]:
    return {"pr": "ucpro", "fr": "pc", "uc_param_str": ""}


def quark_json(response: requests.Response) -> dict[str, Any]:
    response.encoding = "utf-8"
    return response.json()


def get_share_info(share_text: str, passcode: str = "", cookie: str = "") -> QuarkShareInfo:
    pwd_id, code = parse_quark_share(share_text, passcode)
    if not pwd_id:
        raise RuntimeError("未识别夸克分享 ID。")
    session = make_session(cookie)
    session.headers["Referer"] = f"https://pan.quark.cn/s/{pwd_id}"
    response = session.post(
        f"{QUARK_BASE}/share/sharepage/token",
        params=quark_params(),
        json={"pwd_id": pwd_id, "passcode": code},
        timeout=20,
    )
    response.raise_for_status()
    data = quark_json(response)
    if data.get("code") != 0:
        raise RuntimeError(data.get("message") or "获取夸克分享 token 失败。")
    payload = data.get("data", {})
    return QuarkShareInfo(
        pwd_id=pwd_id,
        passcode=code,
        stoken=payload.get("stoken", ""),
        title=payload.get("title", ""),
        size=0,
        file_count=0,
    )


def list_share_dir(session: requests.Session, info: QuarkShareInfo, pdir_fid: str, page_size: int = 100) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    all_items: list[dict[str, Any]] = []
    metadata: dict[str, Any] = {}
    page = 1
    while True:
        response = session.get(
            f"{QUARK_BASE}/share/sharepage/detail",
            params={
                **quark_params(),
                "pwd_id": info.pwd_id,
                "stoken": info.stoken,
                "pdir_fid": pdir_fid,
                "_page": str(page),
                "_size": str(page_size),
                "_fetch_total": "1",
                "_sort": "file_type:asc,updated_at:desc",
            },
            timeout=25,
        )
        response.raise_for_status()
        data = quark_json(response)
        if data.get("code") != 0:
            raise RuntimeError(data.get("message") or "读取夸克目录失败。")
        metadata = data.get("metadata", {}) or {}
        items = data.get("data", {}).get("list", []) or []
        all_items.extend(items)
        total = int(metadata.get("_total", len(all_items)) or len(all_items))
        if len(all_items) >= total or not items:
            break
        page += 1
    return all_items, metadata


def scan_quark_share(share_text: str, passcode: str = "", cookie: str = "", max_files: int = 10000) -> tuple[QuarkShareInfo, list[QuarkFile]]:
    info = get_share_info(share_text, passcode, cookie)
    session = make_session(cookie)
    session.headers["Referer"] = f"https://pan.quark.cn/s/{info.pwd_id}"
    root_items, _ = list_share_dir(session, info, "0", page_size=100)
    queue: list[tuple[str, str]] = []
    files: list[QuarkFile] = []
    def add_file(item: dict[str, Any], path: str) -> None:
        name = item.get("file_name", "")
        suffix = Path(name).suffix.lower()
        files.append(
            QuarkFile(
                fid=item.get("fid", ""),
                token=item.get("share_fid_token", ""),
                name=name,
                path=path,
                size=int(item.get("size", 0) or 0),
                suffix=suffix,
                format_type=item.get("format_type", ""),
                updated_at=str(item.get("updated_at") or item.get("l_updated_at") or ""),
            )
        )

    for item in root_items:
        root_path = item.get("file_name", "")
        if item.get("dir"):
            queue.append((item.get("fid", ""), root_path))
        else:
            add_file(item, root_path)
    while queue and len(files) < max_files:
        fid, path = queue.pop(0)
        if not fid:
            continue
        children, _ = list_share_dir(session, info, fid, page_size=100)
        if not children:
            continue
        for item in children:
            name = item.get("file_name", "")
            child_path = f"{path}/{name}".strip("/")
            if item.get("dir"):
                queue.append((item.get("fid", ""), child_path))
                continue
            add_file(item, child_path)
            if len(files) >= max_files:
                break
    info.file_count = len(files)
    info.size = sum(file.size for file in files)
    return info, files


def get_download_url(session: requests.Session, file: QuarkFile) -> str:
    response = session.post(
        f"{QUARK_BASE}/file/download",
        params=quark_params(),
        json={"fids": [file.fid]},
        timeout=25,
    )
    response.raise_for_status()
    data = quark_json(response)
    if data.get("code") == 31001:
        raise RuntimeError("夸克下载需要登录态。请在 Streamlit Secrets 或页面里配置 QUARK_COOKIE。")
    if data.get("code") != 0:
        raise RuntimeError(data.get("message") or "获取夸克下载链接失败。")
    payload = data.get("data", {})
    if isinstance(payload, list) and payload:
        item = payload[0]
    elif isinstance(payload, dict):
        item = (payload.get("list") or payload.get("file_list") or [payload])[0]
    else:
        item = {}
    return item.get("download_url") or item.get("download_url_backup") or item.get("url") or ""


def download_quark_file(session: requests.Session, file: QuarkFile, target: Path) -> None:
    url = get_download_url(session, file)
    if not url:
        raise RuntimeError("夸克未返回下载链接。该分享可能需要先转存到登录账号。")
    with session.get(url, stream=True, timeout=60) as response:
        response.raise_for_status()
        with target.open("wb") as handle:
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    handle.write(chunk)


def ingest_quark_files(
    share_text: str,
    passcode: str,
    selected_files: list[dict[str, Any]],
    cookie: str = "",
    source_type: str = "内部笔记",
    source_org: str = "夸克网盘",
    industry_tags: str = "",
    company_tags: str = "",
    technology_tags: str = "",
    size_limit_mb: int = 80,
) -> tuple[int, list[str]]:
    info = get_share_info(share_text, passcode, cookie)
    session = make_session(cookie)
    session.headers["Referer"] = f"https://pan.quark.cn/s/{info.pwd_id}"
    ok = 0
    errors: list[str] = []
    for raw in selected_files:
        file = QuarkFile(**{key: raw.get(key, "") for key in QuarkFile.__dataclass_fields__.keys()})
        try:
            if file.suffix not in SUPPORTED_SUFFIXES:
                raise RuntimeError(f"暂不支持的文件类型：{file.suffix or '无后缀'}")
            if size_limit_mb > 0 and file.size > size_limit_mb * 1024 * 1024:
                raise RuntimeError(f"文件超过当前单文件下载上限 {size_limit_mb}MB。")
            with tempfile.NamedTemporaryFile(delete=False, suffix=file.suffix) as tmp:
                tmp_path = Path(tmp.name)
            try:
                download_quark_file(session, file, tmp_path)
                add_document(
                    tmp_path,
                    title=Path(file.name).stem,
                    source_type=source_type,
                    source_org=source_org,
                    publish_date="",
                    industry_tags=industry_tags,
                    company_tags=company_tags,
                    technology_tags=technology_tags,
                    source_url=f"https://pan.quark.cn/s/{info.pwd_id}#{file.fid}",
                )
                ok += 1
            finally:
                tmp_path.unlink(missing_ok=True)
        except Exception as exc:
            errors.append(f"{file.path or file.name}: {exc}")
    return ok, errors


def quark_files_to_rows(files: list[QuarkFile]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for idx, file in enumerate(files, start=1):
        rows.append(
            {
                "选择": file.suffix in SUPPORTED_SUFFIXES,
                "序号": idx,
                "文件名": file.name,
                "路径": file.path,
                "大小MB": round(file.size / 1024 / 1024, 2),
                "类型": file.suffix,
                "fid": file.fid,
                "token": file.token,
                "size": file.size,
                "suffix": file.suffix,
                "format_type": file.format_type,
                "updated_at": file.updated_at,
                "is_dir": False,
                "name": file.name,
                "path": file.path,
            }
        )
    return rows
