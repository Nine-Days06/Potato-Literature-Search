#!/usr/bin/env python3
# scripts/incremental_update.py
"""
增量更新流水线
用于搜索词/年份范围变更后的增量处理

用法：
    python scripts/incremental_update.py                          # 用配置文件里的查询词/年份
    python scripts/incremental_update.py --query "new query"      # 指定新搜索词
    python scripts/incremental_update.py --year-min 2020          # 扩大年份范围
    python scripts/incremental_update.py --skip-download          # 跳过下载（仅跑后续）
    python scripts/incremental_update.py --skip-llm               # 跳过 LLM 验证
    python scripts/incremental_update.py --batch                  # LLM 用 Batch API
    python scripts/incremental_update.py --export-since "2025-08-01T00:00:00"  # 增量导出
    python scripts/incremental_update.py --mark-removed           # 标记被查询词排除的旧 PMID
"""

import argparse
import sys
from pathlib import Path

# 确保项目根目录在 Python 路径中
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from downloader.incremental_downloader import run_incremental_download, get_removed_pmids
from parser.xml_parser import run_parse
from cleaner.hard_filter import run_incremental_hard_filter, mark_removed_pmids
from cleaner.llm_validator import run_validation, export_incremental_raw_csv
from config.settings import RAW_XML_DIR, DB_PATH
from utils.db import init_db
from utils.logger import get_logger

logger = get_logger("incremental_update")


def main():
    parser = argparse.ArgumentParser(
        description="马铃薯文献增量更新流水线",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例：
  # 完整增量更新（搜索词/年份用配置文件默认值）
  python scripts/incremental_update.py

  # 搜索词优化了，年份范围扩大了
  python scripts/incremental_update.py --query "NEW_QUERY" --year-min 2015

  # 仅年份扩大（如 2020-2026 -> 2015-2026）
  python scripts/incremental_update.py --year-min 2015

  # 下载解析已手动跑过，只跑后续
  python scripts/incremental_update.py --skip-download

  # 使用智谱 Batch API 加速新增文献验证
  python scripts/incremental_update.py --batch

  # 仅增量导出（指定时间戳后新增的复核通过文献）
  python scripts/incremental_update.py --skip-download --skip-llm --export-since "2025-08-01T00:00:00"

  # 标记因查询词变化不再匹配的旧 PMID（可选，不删除数据）
  python scripts/incremental_update.py --mark-removed
        """
    )
    
    # 查询参数
    parser.add_argument(
        "--query", default=None,
        help="新搜索词（默认用 settings.PUBMED_QUERY）"
    )
    parser.add_argument(
        "--year-min", type=int, default=None,
        help="新最小年份（默认用 settings.SEARCH_YEAR_MIN）"
    )
    parser.add_argument(
        "--year-max", type=int, default=None,
        help="新最大年份（默认用 settings.SEARCH_YEAR_MAX）"
    )
    
    # 阶段控制
    parser.add_argument(
        "--skip-download", action="store_true",
        help="跳过下载阶段（假设 XML 已就绪）"
    )
    parser.add_argument(
        "--skip-parse", action="store_true",
        help="跳过解析阶段"
    )
    parser.add_argument(
        "--skip-filter", action="store_true",
        help="跳过硬过滤阶段"
    )
    parser.add_argument(
        "--skip-llm", action="store_true",
        help="跳过 LLM 验证阶段"
    )
    parser.add_argument(
        "--batch", action="store_true",
        help="LLM 验证使用智谱 Batch API（仅 zhipu provider 有效）"
    )
    
    # 导出控制
    parser.add_argument(
        "--export-since", default=None,
        help="增量导出起始时间 (ISO 格式，如 2025-08-01T00:00:00)"
    )
    
    # 可选：标记被排除的 PMID
    parser.add_argument(
        "--mark-removed", action="store_true",
        help="标记因查询词/年份变化而不再匹配的旧 PMID（写入 filter_log，stage=query_removed）"
    )
    
    args = parser.parse_args()

    # 初始化数据库
    init_db(DB_PATH)

    logger.info("=" * 60)
    logger.info("增量更新流水线启动")
    logger.info(f"数据库: {DB_PATH}")
    logger.info(f"参数: query={args.query}, year_min={args.year_min}, year_max={args.year_max}")
    logger.info(f"跳过: download={args.skip_download}, parse={args.skip_parse}, "
                f"filter={args.skip_filter}, llm={args.skip_llm}")
    logger.info("=" * 60)

    # 记录开始时间（用于增量导出）
    from utils import now_iso
    start_time = now_iso()

    # 1. 增量下载
    new_xml_files = []
    if not args.skip_download:
        new_xml_files = run_incremental_download(
            new_query=args.query,
            new_year_min=args.year_min,
            new_year_max=args.year_max
        )
        if not new_xml_files:
            logger.info("无新增 PMID 下载，检查后续是否有增量数据...")
    else:
        logger.info("跳过下载阶段")

    # 2. 解析 XML -> SQLite
    if not args.skip_parse:
        logger.info("开始解析 XML...")
        run_parse(xml_dir=RAW_XML_DIR, db_path=DB_PATH)
        logger.info("解析完成")
    else:
        logger.info("跳过解析阶段")

    # 3. 增量硬过滤
    if not args.skip_filter:
        logger.info("开始增量硬过滤...")
        run_incremental_hard_filter(DB_PATH)
        logger.info("硬过滤完成")
    else:
        logger.info("跳过硬过滤阶段")

    # 4. 可选：标记被查询词排除的旧 PMID
    if args.mark_removed:
        logger.info("检测因查询词变化不再匹配的旧 PMID...")
        removed = get_removed_pmids(
            new_query=args.query,
            new_year_min=args.year_min,
            new_year_max=args.year_max
        )
        if removed:
            mark_removed_pmids(DB_PATH, removed)
            logger.info(f"已标记 {len(removed)} 篇旧 PMID 为 query_removed")
        else:
            logger.info("无需标记移除的 PMID")

    # 5. LLM 验证（自动增量：跳过已验证/已过滤）
    if not args.skip_llm:
        logger.info("开始 LLM 验证...")
        run_validation(batch_mode=args.batch)
        logger.info("LLM 验证完成")
    else:
        logger.info("跳过 LLM 验证阶段")

    # 6. 增量导出
    if args.export_since:
        logger.info(f"开始增量导出 (since={args.export_since})...")
        path = export_incremental_raw_csv(args.export_since)
        if path:
            logger.info(f"增量导出完成: {path}")
        else:
            logger.info("增量导出: 无新增数据")
    elif not args.skip_download and new_xml_files:
        # 如果有新下载文献且未指定导出时间，默认导出本次运行新增的
        logger.info("检测到新增下载，执行默认增量导出（本次运行后验证的记录）...")
        path = export_incremental_raw_csv(start_time)
        if path:
            logger.info(f"增量导出完成: {path}")

    logger.info("=" * 60)
    logger.info("✅ 增量更新流水线完成")
    logger.info(f"输出目录: {RAW_XML_DIR.parent / 'output'}")
    logger.info("=" * 60)


if __name__ == "__main__":
    main()