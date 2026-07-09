#!/usr/bin/env python3
# main.py
"""
马铃薯文献批量下载与清洗系统 — 一键运行入口

用法：
  python main.py                   # 完整流程（下载 → 解析 → 硬过滤 → 评分 → 导出）
  python main.py --step download   # 仅下载
  python main.py --step parse      # 仅解析 XML → SQLite
  python main.py --step clean      # 仅清洗（硬过滤 + 评分 + 导出）
   python main.py --step export     # 仅导出 CSV（数据库已有评分时使用）
   python main.py --step pdf        # 仅 PDF 下载（基于数据库 PMC ID）
   python main.py --step validate   # LLM 二次验证（需设置 LLM_API_KEY）
   python main.py --step import-review  # 导入人工复核结果
   python main.py --query "potato AND drought"  # 自定义搜索词
"""

import argparse
import sys
from pathlib import Path

# 确保项目根目录在 Python 路径中
sys.path.insert(0, str(Path(__file__).parent))

from config.settings import (
    DB_PATH, RAW_XML_DIR, OUTPUT_DIR, LOG_DIR,
    PDF_HIGH_DIR, PDF_MID_DIR, PDF_LOW_DIR
)
from utils.db import init_db
from utils.logger import get_logger

logger = get_logger("main", log_dir=LOG_DIR)


def step_download(query: str = None):
    from downloader.pubmed_downloader import run_download
    from config.settings import PUBMED_QUERY
    return run_download(query or PUBMED_QUERY)


def step_parse(xml_dir: Path = None):
    from parser.xml_parser import run_parse
    run_parse(xml_dir=xml_dir or RAW_XML_DIR, db_path=DB_PATH)


def step_clean():
    from cleaner.hard_filter import run_hard_filter
    from cleaner.relevance_scorer import run_relevance_scoring, export_csv, write_clean_report

    hard_stats  = run_hard_filter(db_path=DB_PATH)
    score_stats = run_relevance_scoring(db_path=DB_PATH)
    export_csv(db_path=DB_PATH, out_dir=OUTPUT_DIR)
    write_clean_report(hard_stats, score_stats, out_dir=OUTPUT_DIR)


def step_export():
    from cleaner.relevance_scorer import export_csv, write_clean_report
    export_csv(db_path=DB_PATH, out_dir=OUTPUT_DIR)


def step_pdf(prefer_format: str = "pdf"):
    from downloader.pdf_downloader import run_pdf_download
    run_pdf_download(db_path=DB_PATH, prefer_format=prefer_format)


def step_validate():
    from cleaner.llm_validator import run_validation
    run_validation()


def step_import_review(csv_path: str = None):
    from cleaner.llm_validator import import_human_review
    import_human_review(csv_path)


def main():
    parser = argparse.ArgumentParser(
        description="马铃薯 PubMed 文献批量下载与清洗系统"
    )
    parser.add_argument(
        "--step",
        choices=["download", "parse", "clean", "export", "pdf", "validate", "import-review", "all"],
        default="all",
        help="运行指定阶段（默认 all）",
    )
    parser.add_argument(
        "--query",
        default=None,
        help="自定义 PubMed 搜索词（仅在 download / all 阶段生效）",
    )
    parser.add_argument(
        "--xml-dir",
        default=None,
        help="XML 文件目录（仅在 parse / all 阶段生效）",
    )
    parser.add_argument(
        "--prefer-format",
        choices=["pdf", "txt"],
        default="pdf",
        help="pdf 阶段优先下载格式（默认 pdf，失败时自动回退）",
    )
    parser.add_argument(
        "--csv",
        default=None,
        help="import-review 阶段的 CSV 文件路径（默认自动查找最新的复核文件）",
    )
    args = parser.parse_args()

    # 初始化目录和数据库
    for d in [RAW_XML_DIR, OUTPUT_DIR, LOG_DIR, PDF_HIGH_DIR, PDF_MID_DIR, PDF_LOW_DIR, DB_PATH.parent]:
        Path(d).mkdir(parents=True, exist_ok=True)
    init_db(DB_PATH)

    logger.info("▶  马铃薯文献清洗系统启动")
    logger.info(f"   运行阶段: {args.step}")
    logger.info(f"   数据库:   {DB_PATH}")

    step = args.step

    if step in ("download", "all"):
        step_download(args.query)

    if step in ("parse", "all"):
        step_parse(Path(args.xml_dir) if args.xml_dir else None)

    if step in ("clean", "all"):
        step_clean()

    if step == "pdf":
        step_pdf(args.prefer_format)

    if step == "export":
        step_export()

    if step == "validate":
        step_validate()

    if step == "import-review":
        step_import_review(args.csv)

    logger.info("✔  全部流程完成")
    logger.info(f"   输出目录: {OUTPUT_DIR}")


if __name__ == "__main__":
    main()
