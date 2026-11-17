#!/usr/bin/env python3
# main.py
"""
马铃薯文献批量下载与清洗系统 — 一键运行入口

用法：
  python main.py                   # 完整流程（下载 → 解析 → 硬过滤）
  python main.py --step download   # 仅下载 XML
  python main.py --step parse      # 仅解析 XML → SQLite
  python main.py --step clean      # 仅硬过滤
  python main.py --step pdf        # 下载相关文献 OA 全文到 data/pdfs_relevant/
  python main.py --step pdf-retry  # 仅重试失败的 OA 全文下载
    python main.py --step tdm-plan   # 生成 TDM 下载计划（Wiley/Elsevier/Springer，不下载）
  python main.py --step tdm --yes  # 执行 TDM 下载（不加 --yes 只打印计划）
  python main.py --step validate   # LLM 二次验证（需配置 DEEPSEEK_API_KEY / ZHIPU_API_KEY / OPENAI_API_KEY）
python main.py --step import-review  # 导入人工复核结果
  python main.py --step export         # 导出复核通过文献的原始信息 CSV
  python main.py --query "potato AND drought"  # 自定义搜索词
"""

import argparse
import sys
from pathlib import Path

# 确保项目根目录在 Python 路径中
sys.path.insert(0, str(Path(__file__).parent))

from config.settings import (
    DB_PATH, RAW_XML_DIR, OUTPUT_DIR, OUTPUT_SUBDIRS, LOG_DIR
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
    run_hard_filter(db_path=DB_PATH)


def step_pdf():
    from downloader.pdf_downloader import run_pdf_download
    run_pdf_download(db_path=DB_PATH)


def step_pdf_retry():
    from downloader.pdf_downloader import run_pdf_retry
    run_pdf_retry(db_path=DB_PATH)


def step_tdm_plan():
    """只生成 TDM 下载计划（不下载），作为下载前确认材料"""
    from downloader.tdm_downloader import run_tdm_plan
    return run_tdm_plan(db_path=DB_PATH)


def step_tdm(confirm=False, plan_path=None, limit=None):
    """执行 TDM 下载。未传 --yes 时只打印计划，不发起任何请求。"""
    from downloader.tdm_downloader import run_tdm_download
    return run_tdm_download(
        db_path=DB_PATH,
        plan_path=Path(plan_path) if plan_path else None,
        confirm=confirm,
        limit=limit,
    )


def step_validate(batch=False):
    from cleaner.llm_validator import run_validation
    run_validation(batch_mode=batch)


def step_import_review(csv_path: str = None):
    from cleaner.llm_validator import import_human_review
    import_human_review(csv_path)


def step_export():
    """导出三份 CSV：

    1. articles_raw_*.csv —— 仅 articles 原生字段，供下游流程使用；
    2. llm_filtered_*.csv —— 附加 llm_verdict / llm_reason / research_field /
       human_review，研究方向分类在这一份里；
    3. by_field/<方向>/ —— 第 2 份按 research_field 拆成一方向一目录，每个
       目录放两份：llm_filtered_by_field_<方向>_*.csv（同 2 的 17 列）与
       articles_raw_by_field_<方向>_*.csv（同 1 的 15 列，不含 LLM 信息）；
       另附 llm_filtered_by_field_index_*.csv 记录各方向篇数、目录与两份文件名。

    三份的筛选条件一致（_REVIEW_PASS_PREDICATE）。
    """
    from cleaner.llm_validator import (
        _export_raw_csv, _export_filtered_csv, export_by_research_field,
    )

    raw_path = _export_raw_csv()
    if raw_path:
        logger.info(f"原始文献信息已导出: {raw_path}")

    filtered_path = _export_filtered_csv()
    if filtered_path:
        logger.info(f"带研究方向分类的导出: {filtered_path}")

    field_paths = export_by_research_field()
    if field_paths:
        logger.info(f"按研究方向拆分导出: {len(field_paths)} 个文件"
                    f"（每个方向目录内含 LLM 版 + raw 版）")

    # 删除判定翻转后残留在相关全文库的副本
    from downloader.fulltext_library import prune_relevant_fulltext
    prune_relevant_fulltext(db_path=DB_PATH)


def main():
    parser = argparse.ArgumentParser(
        description="马铃薯 PubMed 文献批量下载与清洗系统"
    )
    parser.add_argument(
        "--step",
        choices=["download", "parse", "clean", "pdf", "pdf-retry", "validate",
                 "import-review", "export", "tdm-plan", "tdm", "all"],
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
        "--csv",
        default=None,
        help="import-review 阶段的 CSV 文件路径（默认自动查找最新的复核文件）",
    )
    parser.add_argument(
        "--batch",
        action="store_true",
        help="使用智谱 Batch API 进行 LLM 验证（仅 zhipu provider 有效）",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="确认执行 TDM 下载（不加时 tdm 阶段只打印计划，不发请求）",
    )
    parser.add_argument(
        "--plan",
        default=None,
        help="TDM 计划清单 CSV 路径（默认按数据库实时重算）",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="TDM 本次最多处理多少篇（用于小批量试跑）",
    )
    args = parser.parse_args()

    # 初始化目录和数据库
    output_dirs = [RAW_XML_DIR, OUTPUT_DIR, LOG_DIR, DB_PATH.parent]
    output_dirs += [OUTPUT_DIR / sub for sub in OUTPUT_SUBDIRS]
    for d in output_dirs:
        d.mkdir(parents=True, exist_ok=True)
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
        step_pdf()

    if step == "pdf-retry":
        step_pdf_retry()

    if step == "tdm-plan":
        step_tdm_plan()

    if step == "tdm":
        step_tdm(confirm=args.yes, plan_path=args.plan, limit=args.limit)

    if step == "validate":
        step_validate(args.batch)

    if step == "import-review":
        step_import_review(args.csv)

    if step == "export":
        step_export()

    logger.info("✔  全部流程完成")
    logger.info(f"   输出目录: {OUTPUT_DIR}")


if __name__ == "__main__":
    main()
