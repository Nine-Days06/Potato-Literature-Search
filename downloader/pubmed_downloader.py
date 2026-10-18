# downloader/pubmed_downloader.py
"""
NCBI E-utilities 批量下载器
流程：
  1. esearch  — 按日期切片探数，命中超阈值时对半递归细分（年→月→日）
  2. efetch   — 叶子切片内翻页取全 PMID，规避单查询 10k retstart 硬上限
  3. efetch   — 按批次下载 XML，保存到 raw_xml 目录
  4. 断点续传 — 已下载的批次自动跳过
"""

import re
import time
import json
import threading
import requests
from datetime import date, timedelta
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

from config.settings import (
    NCBI_API_KEY, NCBI_EMAIL,
    REQUEST_INTERVAL, EFETCH_BATCH_SIZE,
    PUBMED_QUERY, RAW_XML_DIR,
    SEARCH_YEAR_MIN, SEARCH_YEAR_MAX, SEARCH_SLICE_YEARS,
    LOG_DIR,
)
from utils.logger import get_logger

logger = get_logger("downloader", log_dir=LOG_DIR)

# 全局速率限制器，保证并发请求不超出 NCBI API 频率限制
class _RateLimiter:
    def __init__(self, interval: float):
        self.interval = interval
        self._lock = threading.Lock()
        self._last = 0.0

    def wait(self):
        with self._lock:
            now = time.time()
            elapsed = now - self._last
            if elapsed < self.interval:
                time.sleep(self.interval - elapsed)
            self._last = time.time()

_rate_limiter = _RateLimiter(REQUEST_INTERVAL)

EUTILS_BASE = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"


# ── 内部工具函数 ──────────────────────────────────────────────

def _base_params() -> dict:
    p = {"email": NCBI_EMAIL, "tool": "potato_lit_pipeline"}
    if NCBI_API_KEY:
        p["api_key"] = NCBI_API_KEY
    return p


def _request_with_retry(send, url: str, params: dict, retries: int = 5) -> requests.Response:
    """带重试的 HTTP 请求（处理 429 / 5xx）

    send: 接收 (url, params) 并返回 Response 的可调用对象
    """
    for attempt in range(1, retries + 1):
        try:
            r = send(url, params)
            if r.status_code == 429:
                wait = 2 ** attempt
                logger.warning(f"Rate limited, waiting {wait}s (attempt {attempt})")
                time.sleep(wait)
                continue
            r.raise_for_status()
            return r
        except requests.RequestException as e:
            if attempt == retries:
                raise
            logger.warning(f"Request failed ({e}), retrying {attempt}/{retries}")
            time.sleep(2 ** attempt)


def _get(url: str, params: dict, retries: int = 5) -> requests.Response:
    """带重试的 GET 请求（处理 429 / 5xx）"""
    return _request_with_retry(
        lambda u, p: requests.get(u, params=p, timeout=60), url, params, retries
    )


def _post(url: str, params: dict, retries: int = 5) -> requests.Response:
    """带重试的 POST 请求（避免 URL 过长 414 错误）"""
    return _request_with_retry(
        lambda u, p: requests.post(u, data=p, timeout=60), url, params, retries
    )


def _safe_json(r: requests.Response) -> dict:
    """处理 NCBI 可能返回的带非法控制字符的 JSON"""
    try:
        return r.json()
    except (json.JSONDecodeError, requests.exceptions.JSONDecodeError) as e:
        logger.warning(f"检测到非法 JSON 响应，尝试修复... ({e})")
        # 常见问题：JSON 字符串中包含未转义的换行符
        # 将原始控制字符替换为转义后的（特别是换行符）
        # 这里简单起见，先把 \n \r 替换掉，因为它们最常导致解析失败
        text = r.text.replace('\n', '\\n').replace('\r', '\\r')
        # 同时也移除其他不可见控制字符
        text = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', '', text)
        try:
            return json.loads(text)
        except Exception as e2:
            logger.error(f"修复后仍无法解析 JSON。原始片段: {r.text[:500]}")
            raise e2


# ── Step 1 & 2：esearch 获取 PMID 列表 ───────────────────────

# NCBI retstart 硬上限 10000；叶子阈值留余量防切分期间新文献入库顶破
SAFE_LEAF_COUNT = 9500
PMID_PAGE_SIZE = 5000
FETCH_HARD_LIMIT = 10000


def _generate_year_slices(slice_size: int = 5) -> list[tuple[int, int]]:
    """将全局年份范围按 slice_size 切分为多个子区间"""
    slices = []
    start = SEARCH_YEAR_MIN
    while start <= SEARCH_YEAR_MAX:
        end = min(start + slice_size - 1, SEARCH_YEAR_MAX)
        slices.append((start, end))
        start = end + 1
    return slices


def _year_range_to_dates(lo: int, hi: int) -> tuple[date, date]:
    return date(lo, 1, 1), date(hi, 12, 31)


def _split_date_range(lo: date, hi: date) -> tuple[tuple[date, date], tuple[date, date]] | None:
    """对半拆分为互斥相邻两段；已细到单日返回 None 终止递归"""
    if lo >= hi:
        return None
    mid = lo + (hi - lo) // 2
    return (lo, mid), (mid + timedelta(days=1), hi)


def _esearch_count(query: str, mindate: str, maxdate: str) -> int:
    """只取命中总数（retmax=0，POST），供切分决策"""
    params = {
        **_base_params(),
        "db": "pubmed",
        "term": query,
        "datetype": "pdat",
        "mindate": mindate,
        "maxdate": maxdate,
        "retmax": 0,
        "retmode": "json",
    }
    _rate_limiter.wait()
    r = _post(f"{EUTILS_BASE}/esearch.fcgi", params)
    result = _safe_json(r)["esearchresult"]
    if "ERROR" in result:
        logger.error(f"NCBI Search Error: {result['ERROR']}")
        raise RuntimeError(result["ERROR"])
    return int(result["count"])


def _fetch_page_by_date(query: str, mindate: str, maxdate: str) -> list[str]:
    """叶子切片内翻页取全 PMID（usehistory + efetch uilist，GET 不带 term）"""
    params = {
        **_base_params(),
        "db": "pubmed",
        "term": query,
        "datetype": "pdat",
        "mindate": mindate,
        "maxdate": maxdate,
        "usehistory": "y",
        "retmax": 0,
        "retmode": "json",
    }
    _rate_limiter.wait()
    r = _post(f"{EUTILS_BASE}/esearch.fcgi", params)
    result = _safe_json(r)["esearchresult"]

    if "ERROR" in result:
        logger.error(f"NCBI Search Error: {result['ERROR']}")
        raise RuntimeError(result["ERROR"])

    total = int(result["count"])
    webenv = result["webenv"]
    querykey = result["querykey"]
    if total == 0:
        return []

    pmids = []
    for start in range(0, total, PMID_PAGE_SIZE):
        if start >= FETCH_HARD_LIMIT:
            logger.error(
                f"触及 NCBI {FETCH_HARD_LIMIT} 条 retstart 硬限制 "
                f"(mindate={mindate}, maxdate={maxdate}, total={total})，"
                "切片未被正确细分，请检查递归切分逻辑"
            )
            break
        p = {
            **_base_params(),
            "db": "pubmed",
            "webenv": webenv,
            "query_key": querykey,
            "retstart": start,
            "retmax": PMID_PAGE_SIZE,
            "rettype": "uilist",
            "retmode": "text",
        }
        try:
            _rate_limiter.wait()
            batch_r = _get(f"{EUTILS_BASE}/efetch.fcgi", p)
            batch_ids = [line.strip() for line in batch_r.text.splitlines() if line.strip()]
            pmids.extend(batch_ids)
            logger.info(f"  PMID 翻页进度: {len(pmids)}/{total} ({mindate}~{maxdate})")
        except requests.exceptions.HTTPError as e:
            if e.response is not None and e.response.status_code == 400:
                logger.warning(
                    f"分页请求失败 (retstart={start})，可能触发 10k 限制，停止本切片翻页"
                )
                break
            raise
        time.sleep(REQUEST_INTERVAL)

    return pmids


def _collect_range(query: str, lo: date, hi: date, depth: int = 0) -> list[str]:
    """日期区间内取全 PMID：≤阈值翻页取全，超阈值对半递归细分"""
    count = _esearch_count(query, lo.isoformat(), hi.isoformat())
    label = f"{lo.isoformat()}~{hi.isoformat()}"
    indent = "  " * depth

    if count <= SAFE_LEAF_COUNT:
        logger.info(f"{indent}切片 {label}: {count} 篇（≤{SAFE_LEAF_COUNT}，直接取全）")
        return _fetch_page_by_date(query, lo.isoformat(), hi.isoformat())

    parts = _split_date_range(lo, hi)
    if parts is None:
        logger.warning(
            f"{indent}切片 {label}: 单日命中 {count} 篇仍超 {SAFE_LEAF_COUNT}，"
            "降级截断取前 10,000 条"
        )
        return _fetch_page_by_date(query, lo.isoformat(), hi.isoformat())

    logger.info(f"{indent}切片 {label}: {count} 篇超阈值，对半细分")
    left, right = parts
    return (
        _collect_range(query, *left, depth + 1)
        + _collect_range(query, *right, depth + 1)
    )


def fetch_pmid_list(query: str = PUBMED_QUERY,
                    mindate: int | None = None,
                    maxdate: int | None = None) -> list[str]:
    """
    获取日期范围内匹配查询的全部 PMID。
    5 年初分段 → 每段 retmax=0 探数 → 超 9500 对半递归 → 叶子内翻页取全，
    规避 NCBI retstart 10000 硬上限导致的静默截断。
    """
    lo_year = mindate if mindate is not None else SEARCH_YEAR_MIN
    hi_year = maxdate if maxdate is not None else SEARCH_YEAR_MAX
    logger.info(f"开始获取 PMID（{lo_year}-{hi_year}，自适应日期切分）...")

    slices = _generate_year_slices(SEARCH_SLICE_YEARS)
    slices = [(lo, hi) for lo, hi in slices if not (hi < lo_year or lo > hi_year)]

    all_pmids = []
    for idx, (lo, hi) in enumerate(slices, 1):
        logger.info(f"--- 年度切片 {idx}/{len(slices)}: {lo}-{hi} ---")
        d_lo, d_hi = _year_range_to_dates(
            max(lo, lo_year), min(hi, hi_year)
        )
        all_pmids.extend(_collect_range(query, d_lo, d_hi))

    all_pmids = list(dict.fromkeys(all_pmids))
    logger.info(f"PMID 列表获取完毕，去重后共 {len(all_pmids)} 条")
    return all_pmids


# ── Step 3：efetch 批量下载 XML ───────────────────────────────

def _download_single_batch(
    batch_idx: int,
    chunk: list[str],
    out_dir: Path,
    batch_size: int,
    total_batches: int,
) -> Path | None:
    """下载单个批次，由线程池调用，受全局速率限制器控制"""
    batch_file = out_dir / f"batch_{batch_idx:05d}.xml"

    if batch_file.exists() and batch_file.stat().st_size > 0:
        logger.info(f"  [批次 {batch_idx+1}/{total_batches}] 已存在，跳过")
        return batch_file

    params = {
        **_base_params(),
        "db": "pubmed",
        "id": ",".join(chunk),
        "rettype": "xml",
        "retmode": "xml",
    }

    _rate_limiter.wait()
    try:
        r = _get(f"{EUTILS_BASE}/efetch.fcgi", params)
        with open(batch_file, "wb") as f:
            f.write(r.content)
        logger.info(
            f"  [批次 {batch_idx+1}/{total_batches}] "
            f"下载 {len(chunk)} 篇 → {batch_file.name} "
            f"({batch_file.stat().st_size / 1024:.1f} KB)"
        )
        return batch_file
    except Exception as e:
        logger.error(f"  [批次 {batch_idx+1}] 下载失败: {e}")
        return None


def download_xml_batches(
    pmids: list[str],
    out_dir: Path = RAW_XML_DIR,
    batch_size: int = EFETCH_BATCH_SIZE,
) -> list[Path]:
    """
    将 PMID 列表分批，通过 efetch 并发下载 PubmedArticleSet XML。
    已存在的批次文件自动跳过（断点续传）。
    返回所有成功下载的批次文件路径。
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    # 保存 PMID 列表备用（便于后续复查）
    pmid_list_path = out_dir / "pmid_list.json"
    with open(pmid_list_path, "w") as f:
        json.dump({"query": PUBMED_QUERY, "total": len(pmids), "pmids": pmids}, f)
    logger.info(f"PMID 列表已保存至 {pmid_list_path}")

    total_batches = (len(pmids) + batch_size - 1) // batch_size
    max_workers = 3 if not NCBI_API_KEY else 8

    chunks = [
        pmids[i * batch_size : (i + 1) * batch_size]
        for i in range(total_batches)
    ]

    xml_files = []
    futures = {}
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        for batch_idx, chunk in enumerate(chunks):
            future = executor.submit(
                _download_single_batch, batch_idx, chunk,
                out_dir, batch_size, total_batches
            )
            futures[future] = batch_idx

        for future in as_completed(futures):
            result = future.result()
            if result is not None:
                xml_files.append(result)

    xml_files.sort()
    logger.info(f"下载完成，共 {len(xml_files)} 个批次文件")
    return xml_files


# ── 公开入口 ──────────────────────────────────────────────────

def run_download(query: str = PUBMED_QUERY) -> list[Path]:
    """完整下载流程入口，返回所有 XML 文件路径"""
    logger.info("=" * 60)
    logger.info("阶段一：批量下载 PubMed 文献")
    logger.info(f"搜索词: {query[:80]}...")
    logger.info("=" * 60)

    all_pmids = fetch_pmid_list(query)
    logger.info(f"共 {len(all_pmids)} 篇待下载")

    xml_files = download_xml_batches(all_pmids)
    return xml_files
