# downloader/pdf_downloader.py
"""
PMC Open Access PDF 下载器
流程：
  1. 从数据库读取高/中相关且带有 PMC ID 的文献。
  2. 调用 PMC OA API 获取这些 PMC ID 对应的 PDF 下载链接。
  3. 转换为 HTTPS 链接并下载到指定目录。
  4. 支持断点续传（跳过已存在文件）。
"""

import time
import re
import tarfile
import io
import csv
import subprocess
import requests
from pathlib import Path
from datetime import datetime
from lxml import etree
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

from config.settings import (
    DB_PATH, PMC_OA_API, 
    PDF_HIGH_DIR, PDF_MID_DIR, PDF_LOW_DIR,
    REQUEST_INTERVAL, OUTPUT_DIR
)
from utils.db import get_conn
from utils.logger import get_logger

logger = get_logger("pdf_downloader")

OA_FETCH_MAX_WORKERS = 8
DOWNLOAD_MAX_WORKERS = 8
OA_LINKS_CACHE_GLOB = "oa_download_links_*.csv"
ARIA2C_CONNECT_PER_SERVER = "8"
ARIA2C_SPLIT = "8"
ARIA2C_MIN_SPLIT_SIZE = "1M"

# ── API 调用与解析 ───────────────────────────────────────────

def normalize_pmc_asset_url(url: str) -> str:
    """
    将 OA API 返回的资源链接标准化为当前可访问的 HTTPS 路径。

    背景：PMC 在 2026-04 调整了 FTP/Cloud 目录结构，旧路径
    /pub/pmc/... 需迁移到 /pub/pmc/deprecated/...。
    """
    if not url:
        return ""

    normalized = url.strip()
    if normalized.startswith("ftp://"):
        normalized = "https://" + normalized[len("ftp://"):]

    old_prefix = "https://ftp.ncbi.nlm.nih.gov/pub/pmc/"
    new_prefix = "https://ftp.ncbi.nlm.nih.gov/pub/pmc/deprecated/"
    if normalized.startswith(old_prefix) and not normalized.startswith(new_prefix):
        normalized = normalized.replace(old_prefix, new_prefix, 1)

    return normalized


def normalize_pmc_id(value: str) -> str | None:
    """
    规范化并校验 PMC ID。
    - 纯数字：补齐为 `PMC{digits}`
    - `PMC` 前缀：统一转大写
    - 仅接受 `PMC` + 数字格式
    """
    if not value:
        return None

    normalized = value.strip().upper()
    if not normalized:
        return None

    if normalized.isdigit():
        normalized = f"PMC{normalized}"

    if not re.fullmatch(r"PMC\d+", normalized):
        return None

    return normalized

def _request_oa_with_retry(
    url: str, params: dict | list, max_retries: int = 3, timeout: int = 45
) -> requests.Response | None:
    """带指数退避重试的 OA API GET 请求。429/5xx 可重试，其他 4xx 不重试。"""
    for attempt in range(1, max_retries + 1):
        try:
            r = requests.get(url, params=params, timeout=timeout)
            if r.status_code == 429:
                wait = 2 ** attempt
                logger.warning(f"OA API rate limited (429), waiting {wait}s (attempt {attempt}/{max_retries})")
                time.sleep(wait)
                continue
            if 500 <= r.status_code < 600:
                wait = 2 ** attempt
                logger.warning(f"OA API server error ({r.status_code}), waiting {wait}s (attempt {attempt}/{max_retries})")
                time.sleep(wait)
                continue
            r.raise_for_status()
            return r
        except requests.Timeout:
            if attempt == max_retries:
                logger.warning(f"OA API timeout after {max_retries} retries: {url[:60]}...")
                return None
            wait = 2 ** attempt
            logger.warning(f"OA API timeout, waiting {wait}s (attempt {attempt}/{max_retries})")
            time.sleep(wait)
        except requests.ConnectionError as e:
            if attempt == max_retries:
                logger.warning(f"OA API connection error after {max_retries} retries: {e}")
                return None
            wait = 2 ** attempt
            logger.warning(f"OA API connection error, waiting {wait}s (attempt {attempt}/{max_retries}): {e}")
            time.sleep(wait)
        except requests.RequestException as e:
            logger.warning(f"OA API request failed: {e}")
            return None
    return None


def _fetch_single_oa_link(pmc_id: str) -> tuple[str, dict[str, str] | None]:
    """
    查询单个 PMCID 的 OA 资源链接。
    返回 (pmc_id, {"pdf": ..., "tgz": ...} | None)
    """
    parser = etree.XMLParser(recover=True)

    r = _request_oa_with_retry(PMC_OA_API, params={"id": pmc_id}, timeout=30)
    if r is None:
        logger.warning(f"  单条查询失败: {pmc_id}（API 请求失败）")
        return pmc_id, None

    try:
        root = etree.fromstring(r.content, parser=parser)
    except Exception as e:
        logger.warning(f"  单条查询 XML 解析失败: {pmc_id} -> {e}")
        return pmc_id, None

    record = root.find(".//record")
    if record is None:
        return pmc_id, None

    links: dict[str, str] = {}
    pdf_link_node = record.find(".//link[@format='pdf']")
    if pdf_link_node is not None and pdf_link_node.get("href"):
        links["pdf"] = normalize_pmc_asset_url(pdf_link_node.get("href"))

    tgz_link_node = record.find(".//link[@format='tgz']")
    if tgz_link_node is not None and tgz_link_node.get("href"):
        links["tgz"] = normalize_pmc_asset_url(tgz_link_node.get("href"))

    if not links:
        return pmc_id, None

    return pmc_id, links


def _extract_links_from_record(record) -> tuple[str | None, dict[str, str] | None]:
    record_id = record.get("id") or record.get("pmcid") or record.get("pmc_id")
    normalized_id = normalize_pmc_id(record_id) if record_id else None
    if not normalized_id:
        return None, None

    links: dict[str, str] = {}
    pdf_link_node = record.find(".//link[@format='pdf']")
    if pdf_link_node is not None and pdf_link_node.get("href"):
        links["pdf"] = normalize_pmc_asset_url(pdf_link_node.get("href"))

    tgz_link_node = record.find(".//link[@format='tgz']")
    if tgz_link_node is not None and tgz_link_node.get("href"):
        links["tgz"] = normalize_pmc_asset_url(tgz_link_node.get("href"))

    if not links:
        return normalized_id, None

    return normalized_id, links


def _extract_error_pmc_ids(root) -> set[str]:
    error_ids: set[str] = set()
    for error in root.findall(".//error"):
        raw_candidates = []
        if error.get("id"):
            raw_candidates.append(error.get("id"))
        if error.text:
            raw_candidates.extend(re.findall(r"PMC\d+", error.text.upper()))

        for candidate in raw_candidates:
            normalized = normalize_pmc_id(candidate)
            if normalized:
                error_ids.add(normalized)

    return error_ids


def load_cached_oa_links(
    pmc_ids: list[str],
    out_dir: Path = OUTPUT_DIR,
) -> dict[str, dict[str, str]]:
    """
    从历史导出的 OA 链接清单中加载可复用链接。
    只返回当前 `pmc_ids` 范围内的记录。
    """
    if not pmc_ids:
        return {}

    target_ids = set(pmc_ids)
    cached_links: dict[str, dict[str, str]] = {}
    csv_files = sorted(out_dir.glob(OA_LINKS_CACHE_GLOB), reverse=True)

    for csv_file in csv_files:
        if len(cached_links) >= len(target_ids):
            break

        try:
            with open(csv_file, "r", encoding="utf-8-sig", newline="") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    pmc_id = normalize_pmc_id(row.get("pmc_id", ""))
                    if not pmc_id or pmc_id not in target_ids or pmc_id in cached_links:
                        continue

                    links: dict[str, str] = {}
                    pdf_url = normalize_pmc_asset_url(row.get("pdf_url", "")) if row.get("pdf_url") else ""
                    tgz_url = normalize_pmc_asset_url(row.get("tgz_url", "")) if row.get("tgz_url") else ""
                    if pdf_url:
                        links["pdf"] = pdf_url
                    if tgz_url:
                        links["tgz"] = tgz_url

                    if links:
                        cached_links[pmc_id] = links
        except Exception as e:
            logger.warning(f"读取历史链接清单失败，已跳过: {csv_file} -> {e}")

    return cached_links


def fetch_oa_links(
    pmc_ids: list[str],
    cached_links: dict[str, dict[str, str]] | None = None,
) -> dict[str, dict[str, str]]:
    """
    获取 PMCID 对应的 OA 资源链接（pdf/tgz）。
    `oa.fcgi` 仅支持单 ID 查询，这里用并发 + 速率控制逐条查询。
    """
    if not pmc_ids:
        return {}

    uniq_pmc_ids = list(dict.fromkeys(pmc_ids))
    cached_links = cached_links or {}
    oa_map: dict[str, dict[str, str]] = {
        pmc_id: cached_links[pmc_id]
        for pmc_id in uniq_pmc_ids
        if pmc_id in cached_links
    }

    missing = [pmc_id for pmc_id in uniq_pmc_ids if pmc_id not in oa_map]
    if not missing:
        return oa_map

    logger.info(f"需要远程查询 {len(missing)} 个 PMCID（并发 {OA_FETCH_MAX_WORKERS} 线程，请求间隔 {REQUEST_INTERVAL}s）...")

    rate_lock = threading.Lock()
    last_ts = [0.0]

    def _rate_limited_fetch(pmc_id: str):
        with rate_lock:
            now = time.time()
            gap = REQUEST_INTERVAL - (now - last_ts[0])
            if gap > 0:
                time.sleep(gap)
            last_ts[0] = time.time()
        return _fetch_single_oa_link(pmc_id)

    with ThreadPoolExecutor(max_workers=OA_FETCH_MAX_WORKERS) as executor:
        future_to_pmc = {
            executor.submit(_rate_limited_fetch, pid): pid
            for pid in missing
        }

        for i, future in enumerate(as_completed(future_to_pmc), 1):
            pid, links = future.result()
            if links:
                oa_map[pid] = links
            if i % 20 == 0 or i == len(missing):
                logger.info(f"  获取 OA 链接进度: {i}/{len(missing)}")

    return oa_map


def fetch_pdf_urls(pmc_ids: list[str]) -> dict[str, str]:
    """
    分批调用 PMC OA API 获取 PDF 下载链接。
    返回 {pmc_id: pdf_url} 字典。
    """
    cached_links = load_cached_oa_links(pmc_ids)
    oa_links = fetch_oa_links(pmc_ids, cached_links=cached_links)
    return {pmc_id: links["pdf"] for pmc_id, links in oa_links.items() if "pdf" in links}


def export_oa_links_csv(
    oa_links: dict[str, dict[str, str]],
    pmc_to_info: dict[str, dict[str, str]],
    out_dir: Path = OUTPUT_DIR,
) -> Path:
    """
    导出本次获取到的 OA 下载链接清单（CSV）。
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    csv_path = out_dir / f"oa_download_links_{timestamp}.csv"

    with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(["pmid", "pmc_id", "label", "pdf_url", "tgz_url"])

        for pmc_id in sorted(oa_links.keys()):
            info = pmc_to_info.get(pmc_id, {})
            links = oa_links[pmc_id]
            writer.writerow([
                info.get("pmid", ""),
                pmc_id,
                info.get("label", ""),
                links.get("pdf", ""),
                links.get("tgz", ""),
            ])

    return csv_path

def export_failed_links_csv(
    failed_items: list[dict],
    out_dir: Path = OUTPUT_DIR,
) -> Path:
    """
    导出下载失败的链接清单（CSV），方便人工核查或补下载。
    每行包含 pmid、pmc_id、label、pdf_url、tgz_url、error 类型。
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    csv_path = out_dir / f"failed_downloads_{timestamp}.csv"

    with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(["pmid", "pmc_id", "label", "pdf_url", "tgz_url"])
        for item in failed_items:
            links = item["links"]
            writer.writerow([
                item.get("pmid", ""),
                item.get("pmc_id", ""),
                item.get("label", ""),
                links.get("pdf", "") if links else "",
                links.get("tgz", "") if links else "",
            ])

    return csv_path


# ── 下载核心 ──────────────────────────────────────────────────

def _run_aria2c_download(url: str, output_path: Path, timeout_sec: int = 300) -> bool:
    output_path.parent.mkdir(parents=True, exist_ok=True)

    cmd = [
        "aria2c",
        "--allow-overwrite=true",
        "--auto-file-renaming=false",
        "--file-allocation=none",
        "--summary-interval=0",
        "--console-log-level=warn",
        "--max-tries=3",
        "--retry-wait=2",
        "--connect-timeout=30",
        "--timeout=60",
        "-x", ARIA2C_CONNECT_PER_SERVER,
        "-s", ARIA2C_SPLIT,
        "-k", ARIA2C_MIN_SPLIT_SIZE,
        "-d", str(output_path.parent),
        "-o", output_path.name,
        url,
    ]

    try:
        completed = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_sec,
            check=False,
        )
    except FileNotFoundError:
        logger.error("未检测到 aria2c，请先安装并确保其在系统 PATH 中。")
        return False
    except subprocess.TimeoutExpired:
        logger.error(f"aria2c 下载超时: {url}")
        return False
    except Exception as e:
        logger.error(f"aria2c 执行异常: {url} -> {e}")
        return False

    if completed.returncode != 0:
        err_msg = (completed.stderr or completed.stdout or "").strip()
        if len(err_msg) > 300:
            err_msg = err_msg[-300:]
        logger.error(f"aria2c 下载失败: {url} -> {err_msg}")
        return False

    return output_path.exists()

def download_pdf_file(url: str, dest_path: Path) -> bool:
    """
    下载单个 PDF 文件，支持断点续传（检查是否存在）。
    成功返回 True，失败返回 False。
    """
    if dest_path.exists() and dest_path.stat().st_size > 1024: 
        return True

    dest_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = dest_path.with_suffix(".pdf.part")

    try:
        if not _run_aria2c_download(url, temp_path):
            raise RuntimeError("aria2c 执行失败")

        if temp_path.stat().st_size <= 1024:
            raise RuntimeError("下载文件过小，可能不是有效 PDF")

        temp_path.replace(dest_path)
        return True

    except Exception as e:
        logger.error(f"  下载失败: {url} -> {e}")

        if temp_path.exists():
            temp_path.unlink()
        if dest_path.exists() and dest_path.stat().st_size <= 1024:
            dest_path.unlink()

        return False


def download_txt_from_tgz(url: str, dest_path: Path) -> bool:
    """
    从 OA tgz 包中提取正文并保存为 txt。
    优先使用包内现成 txt；否则从 nxml/xml 提取纯文本。
    """
    if dest_path.exists() and dest_path.stat().st_size > 1024:
        return True

    dest_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = dest_path.with_suffix(".txt.part")
    tgz_temp_path = dest_path.with_suffix(".tgz.part")

    try:
        text_content = ""
        if not _run_aria2c_download(url, tgz_temp_path):
            raise RuntimeError("aria2c 下载 tgz 失败")

        with tarfile.open(tgz_temp_path, mode="r:gz") as tar:
            txt_member = None
            xml_member = None
            for member in tar.getmembers():
                lower_name = member.name.lower()
                if member.isfile() and lower_name.endswith(".txt"):
                    txt_member = member
                    break
                if member.isfile() and (lower_name.endswith(".nxml") or lower_name.endswith(".xml")):
                    xml_member = xml_member or member

            if txt_member is not None:
                extracted = tar.extractfile(txt_member)
                if extracted is not None:
                    text_content = extracted.read().decode("utf-8", errors="ignore")
            elif xml_member is not None:
                extracted = tar.extractfile(xml_member)
                if extracted is not None:
                    xml_bytes = extracted.read()
                    parser = etree.XMLParser(recover=True)
                    root = etree.fromstring(xml_bytes, parser=parser)
                    chunks = [s.strip() for s in root.itertext() if s and s.strip()]
                    text_content = "\n".join(chunks)

        if len(text_content) <= 200:
            raise RuntimeError("提取到的正文过短，疑似无效内容")

        with open(temp_path, "w", encoding="utf-8") as f:
            f.write(text_content)

        # 对文本文件按字符长度判定有效性，避免英文内容被字节阈值误杀
        if len(text_content.strip()) <= 200:
            raise RuntimeError("TXT 内容过短，可能提取失败")

        temp_path.replace(dest_path)
        return True

    except Exception as e:
        logger.error(f"  TXT 提取失败: {url} -> {e}")
        if temp_path.exists():
            temp_path.unlink()
        if dest_path.exists() and dest_path.stat().st_size <= 1024:
            dest_path.unlink()
        return False
    finally:
        if tgz_temp_path.exists():
            tgz_temp_path.unlink()


def download_pdf_from_tgz(url: str, dest_path: Path) -> bool:
    """
    从 OA tgz 包中提取 PDF，并保存为 .pdf。
    当 `oa.fcgi` 未返回 pdf 直链但包内包含 PDF 时可作为回退策略。
    """
    if dest_path.exists() and dest_path.stat().st_size > 1024:
        return True

    dest_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = dest_path.with_suffix(".pdf.part")
    tgz_temp_path = dest_path.with_suffix(".tgz.part")

    try:
        pdf_bytes = None
        if not _run_aria2c_download(url, tgz_temp_path):
            raise RuntimeError("aria2c 下载 tgz 失败")

        with tarfile.open(tgz_temp_path, mode="r:gz") as tar:
            pdf_member = None
            for member in tar.getmembers():
                if member.isfile() and member.name.lower().endswith(".pdf"):
                    pdf_member = member
                    break

            if pdf_member is None:
                raise RuntimeError("tgz 包内未找到 PDF 文件")

            extracted = tar.extractfile(pdf_member)
            if extracted is None:
                raise RuntimeError("无法读取 tgz 包内 PDF 文件")
            pdf_bytes = extracted.read()

        if not pdf_bytes or len(pdf_bytes) <= 1024:
            raise RuntimeError("提取到的 PDF 过小，可能无效")

        with open(temp_path, "wb") as f:
            f.write(pdf_bytes)

        if temp_path.stat().st_size <= 1024:
            raise RuntimeError("PDF 文件过小，可能提取失败")

        temp_path.replace(dest_path)
        return True
    except Exception as e:
        logger.error(f"  TGZ->PDF 提取失败: {url} -> {e}")
        if temp_path.exists():
            temp_path.unlink()
        if dest_path.exists() and dest_path.stat().st_size <= 1024:
            dest_path.unlink()
        return False
    finally:
        if tgz_temp_path.exists():
            tgz_temp_path.unlink()


def download_with_fallback(
    links: dict[str, str],
    pdf_path: Path,
    txt_path: Path,
    prefer_format: str = "pdf",
) -> str | None:
    """
    按用户指定优先格式下载；优先格式失败或缺失时回退另一种。
    返回 "pdf" / "txt" / None。
    """
    pdf_url = links.get("pdf")
    tgz_url = links.get("tgz")

    ordered_formats = ["pdf", "txt"] if prefer_format != "txt" else ["txt", "pdf"]

    for fmt in ordered_formats:
        if fmt == "pdf":
            if pdf_url and download_pdf_file(pdf_url, pdf_path):
                return "pdf"
            if (not pdf_url) and tgz_url and download_pdf_from_tgz(tgz_url, pdf_path):
                return "pdf"
        else:
            if tgz_url and download_txt_from_tgz(tgz_url, txt_path):
                return "txt"

    return None

# ── 业务流程 ──────────────────────────────────────────────────

def run_pdf_download(db_path: Path = DB_PATH, prefer_format: str = "pdf"):
    """
    执行 PDF 下载主流程。
    """
    logger.info("=" * 60)
    logger.info("阶段四：下载 OA 全文（PDF/TXT）")
    logger.info("=" * 60)

    # 1. 查找高/中相关且有 PMC ID 的文献
    query = """
    SELECT a.pmid, a.pmc_id, s.label
    FROM articles a
    JOIN relevance_scores s ON a.pmid = s.pmid
    WHERE (s.label = '高相关' OR s.label = '中相关' OR s.label = '低相关')
      AND a.pmc_id IS NOT NULL AND a.pmc_id != ''
    """
    
    with get_conn(db_path) as conn:
        records = conn.execute(query).fetchall()
        
    if not records:
        logger.info("未发现符合下载条件（有 PMC ID 且已评分）的文献。")
        return

    prefer_format = prefer_format.lower().strip()
    if prefer_format not in {"pdf", "txt"}:
        prefer_format = "pdf"
    logger.info(f"下载优先格式: {prefer_format.upper()}")

    logger.info(f"符合条件的文献共 {len(records)} 篇，开始获取下载链接...")

    # 2. 批量获取 PDF 链接
    pmc_to_info = {}
    invalid_pmc_count = 0
    for r in records:
        pid = normalize_pmc_id(r["pmc_id"])
        if not pid:
            invalid_pmc_count += 1
            continue
        pmc_to_info[pid] = {"pmid": r["pmid"], "label": r["label"]}

    if invalid_pmc_count:
        logger.warning(f"已跳过 {invalid_pmc_count} 条格式无效的 PMC ID。")
    
    pmc_ids = list(pmc_to_info.keys())
    cached_oa_links = load_cached_oa_links(pmc_ids)
    if cached_oa_links:
        logger.info(f"复用历史已获取链接 {len(cached_oa_links)} 条。")

    oa_links = fetch_oa_links(pmc_ids, cached_links=cached_oa_links)
    pdf_link_count = sum(1 for links in oa_links.values() if "pdf" in links)
    tgz_link_count = sum(1 for links in oa_links.values() if "tgz" in links)

    logger.info(
        f"成功获取 {len(oa_links)} 条 OA 资源（PDF: {pdf_link_count}, TGZ: {tgz_link_count}）。"
    )

    links_csv = export_oa_links_csv(oa_links=oa_links, pmc_to_info=pmc_to_info)
    logger.info(f"已导出下载链接清单: {links_csv}")

    # 3. 执行下载
    pdf_success_count = 0
    txt_success_count = 0
    failed_count = 0
    skip_count = 0
    failed_items: list[dict] = []

    # 使用线程池并发下载，提高效率
    with ThreadPoolExecutor(max_workers=DOWNLOAD_MAX_WORKERS) as executor:
        future_to_meta: dict = {}
        for pmc_id, links in oa_links.items():
            info = pmc_to_info[pmc_id]
            dest_dir = {"高相关": PDF_HIGH_DIR, "中相关": PDF_MID_DIR, "低相关": PDF_LOW_DIR}[info["label"]]
            pdf_path = dest_dir / f"{info['pmid']}.pdf"
            txt_path = dest_dir / f"{info['pmid']}.txt"

            if pdf_path.exists() or txt_path.exists():
                skip_count += 1
                continue

            meta = {
                "pmc_id": pmc_id,
                "links": links,
                "pdf_path": pdf_path,
                "txt_path": txt_path,
                "prefer_format": prefer_format,
                "pmid": info["pmid"],
                "label": info["label"],
            }
            future = executor.submit(
                download_with_fallback,
                links,
                pdf_path,
                txt_path,
                prefer_format,
            )
            future_to_meta[future] = meta

        for future in as_completed(future_to_meta):
            result = future.result()
            meta = future_to_meta[future]
            if result == "pdf":
                pdf_success_count += 1
            elif result == "txt":
                txt_success_count += 1
            else:
                failed_count += 1
                failed_items.append(meta)

            total_success = pdf_success_count + txt_success_count
            if total_success % 10 == 0 and total_success > 0:
                logger.info(f"  已下载 {total_success} 篇（PDF: {pdf_success_count}, TXT: {txt_success_count}）...")

    logger.info(
        f"首次下载完成：PDF {pdf_success_count} 篇，TXT {txt_success_count} 篇，"
        f"失败 {failed_count} 篇，跳过 {skip_count} 篇。"
    )

    # 4. 重试下载失败的链接（最多 2 次）
    MAX_RETRIES = 2
    retry_round = 0
    while failed_items and retry_round < MAX_RETRIES:
        retry_round += 1
        retry_success_pdf = 0
        retry_success_txt = 0
        retry_fail: list[dict] = []

        logger.info(
            f"重试第 {retry_round}/{MAX_RETRIES} 轮，剩余 {len(failed_items)} 篇待重试..."
        )

        with ThreadPoolExecutor(max_workers=DOWNLOAD_MAX_WORKERS) as executor:
            retry_future_to_meta = {
                executor.submit(
                    download_with_fallback,
                    item["links"],
                    item["pdf_path"],
                    item["txt_path"],
                    item["prefer_format"],
                ): item
                for item in failed_items
            }

            for future in as_completed(retry_future_to_meta):
                result = future.result()
                if result == "pdf":
                    retry_success_pdf += 1
                elif result == "txt":
                    retry_success_txt += 1
                else:
                    retry_fail.append(retry_future_to_meta[future])

        failed_items = retry_fail
        logger.info(
            f"重试第 {retry_round} 轮完成：PDF {retry_success_pdf} 篇，"
            f"TXT {retry_success_txt} 篇，仍失败 {len(failed_items)} 篇。"
        )

    # 5. 导出最终失败链接列表
    if failed_items:
        failed_csv = export_failed_links_csv(failed_items, out_dir=OUTPUT_DIR)
        logger.info(f"仍有 {len(failed_items)} 篇下载失败，失败链接清单: {failed_csv}")
    else:
        logger.info("所有下载任务均已成功完成。")

    logger.info(f"存储位置: {PDF_HIGH_DIR} 和 {PDF_MID_DIR}")
