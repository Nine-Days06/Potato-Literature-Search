# downloader/incremental_downloader.py
"""
增量下载模块
功能：
1. 读取上次下载的 PMID 列表（含查询词）
2. 用新查询词/年份范围获取最新 PMID 列表
3. 计算差集，仅下载新增 PMID 的 XML
4. 合并更新 pmid_list.json
"""

import json
from pathlib import Path
from typing import Optional, List

from config.settings import (
    RAW_XML_DIR, PUBMED_QUERY, SEARCH_YEAR_MIN, SEARCH_YEAR_MAX, LOG_DIR
)
from downloader.pubmed_downloader import (
    fetch_pmid_list, download_xml_batches
)
from utils.logger import get_logger

logger = get_logger("incremental_downloader", log_dir=LOG_DIR)


def _load_old_pmid_list(pmid_file: Path) -> tuple[set[str], Optional[str]]:
    """读取旧 PMID 列表和查询词"""
    if not pmid_file.exists():
        return set(), None
    try:
        with open(pmid_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        pmids = set(data.get("pmids", []))
        query = data.get("query")
        logger.info(f"加载旧 PMID 列表: {len(pmids)} 篇, 查询词: {query[:80] if query else 'N/A'}...")
        return pmids, query
    except Exception as e:
        logger.warning(f"读取旧 PMID 列表失败: {e}")
        return set(), None


def _save_pmid_list(pmid_file: Path, pmids: List[str], query: str):
    """保存合并后的 PMID 列表"""
    pmid_file.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "query": query,
        "total": len(pmids),
        "pmids": sorted(pmids)
    }
    with open(pmid_file, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    logger.info(f"PMID 列表已更新: {pmid_file} (共 {len(pmids)} 篇)")


def fetch_all_pmids_for_query(
    query: str,
    year_min: int,
    year_max: int,
    slice_years: int = None
) -> List[str]:
    """
    获取指定查询词在年份范围内的所有 PMID（自适应日期切分，规避 10k 截断）
    slice_years 已废弃，保留参数仅为兼容旧签名。
    """
    return fetch_pmid_list(query, mindate=year_min, maxdate=year_max)


def run_incremental_download(
    new_query: Optional[str] = None,
    new_year_min: Optional[int] = None,
    new_year_max: Optional[int] = None,
    slice_years: int = None
) -> List[Path]:
    """
    执行增量下载
    
    Args:
        new_query: 新搜索词，None 则使用配置文件默认值
        new_year_min: 新最小年份，None 则使用配置文件默认值
        new_year_max: 新最大年份，None 则使用配置文件默认值
        slice_years: 已废弃，保留仅为兼容旧签名
    
    Returns:
        新下载的 XML 批次文件路径列表
    """
    pmid_file = RAW_XML_DIR / "pmid_list.json"
    
    # 1. 加载旧数据
    old_pmids, old_query = _load_old_pmid_list(pmid_file)
    
    # 2. 确定新参数
    query = new_query or PUBMED_QUERY
    year_min = new_year_min or SEARCH_YEAR_MIN
    year_max = new_year_max or SEARCH_YEAR_MAX
    
    logger.info("=" * 60)
    logger.info("增量下载开始")
    logger.info(f"新查询词: {query[:100]}...")
    logger.info(f"新年份范围: {year_min}-{year_max}")
    logger.info("=" * 60)
    
    # 3. 获取新查询词下的所有 PMID
    new_pmids = fetch_all_pmids_for_query(query, year_min, year_max, slice_years)
    new_pmids_set = set(new_pmids)
    
    # 4. 计算差集
    added_pmids = new_pmids_set - old_pmids
    removed_pmids = old_pmids - new_pmids_set
    retained_pmids = old_pmids & new_pmids_set
    
    logger.info("PMID 对比结果:")
    logger.info(f"  保留: {len(retained_pmids)} 篇")
    logger.info(f"  新增: {len(added_pmids)} 篇")
    logger.info(f"  移除: {len(removed_pmids)} 篇 (查询词/年份变化导致不再匹配)")
    
    if not added_pmids:
        logger.info("无新增 PMID，跳过 XML 下载")
        # 仍然更新查询词记录（便于追踪）
        merged_pmids = sorted(new_pmids_set)
        _save_pmid_list(pmid_file, merged_pmids, query)
        return []
    
    # 5. 仅下载新增 PMID 的 XML（把 query 写进 pmid_list.json，保证来源可追溯）
    logger.info(f"开始下载 {len(added_pmids)} 篇新文献的 XML...")
    xml_files = download_xml_batches(list(added_pmids), query=query)
    
    # 6. 合并保存 PMID 列表（全量覆盖，包含旧+新）
    merged_pmids = sorted(old_pmids | new_pmids_set)
    _save_pmid_list(pmid_file, merged_pmids, query)
    
    logger.info(f"增量下载完成，新增 {len(xml_files)} 个批次文件")
    return xml_files


def get_removed_pmids(
    new_query: Optional[str] = None,
    new_year_min: Optional[int] = None,
    new_year_max: Optional[int] = None
) -> set[str]:
    """
    获取因查询词/年份变化而不再匹配的 PMID（用于可选的标记/删除）
    """
    pmid_file = RAW_XML_DIR / "pmid_list.json"
    old_pmids, _ = _load_old_pmid_list(pmid_file)
    
    if not old_pmids:
        return set()
    
    query = new_query or PUBMED_QUERY
    year_min = new_year_min or SEARCH_YEAR_MIN
    year_max = new_year_max or SEARCH_YEAR_MAX
    
    new_pmids = fetch_all_pmids_for_query(query, year_min, year_max)
    new_pmids_set = set(new_pmids)
    
    return old_pmids - new_pmids_set


if __name__ == "__main__":
    # 简单测试
    from utils.db import init_db
    from config.settings import DB_PATH
    
    init_db(DB_PATH)
    
    # 模拟：只跑增量下载，不改查询词
    files = run_incremental_download()
    logger.info(f"Downloaded {len(files)} batch files")