# cleaner/llm_validator.py
"""
LLM 文献验证模块
使用 OpenAI 兼容 API（DeepSeek 等）对文献进行二次相关性验证。
"""

import json
import re
import time
import csv
import os
from pathlib import Path
from datetime import datetime

from openai import OpenAI

from config.settings import (
    DB_PATH, OUTPUT_DIR, LOG_DIR,
    LLM_BATCH_SIZE, LLM_MAX_TOKENS, LLM_MAX_RETRIES, LLM_MAX_ROUNDS,
    LLM_PROVIDER,
    DEEPSEEK_API_KEY, DEEPSEEK_BASE_URL, DEEPSEEK_MODEL,
    ZHIPU_API_KEY, ZHIPU_MODEL,
    OPENAI_API_KEY, OPENAI_BASE_URL, OPENAI_MODEL,
)
from utils.db import get_conn
from utils.logger import get_logger

logger = get_logger("llm_validator", log_dir=LOG_DIR)

CHECKPOINT_FILENAME = "llm_validation_progress.json"


def _checkpoint_path() -> Path:
    return Path(OUTPUT_DIR) / CHECKPOINT_FILENAME


def _save_checkpoint(round_num: int, failed_rows: list):
    """保存轮次检查点，崩溃后可恢复"""
    data = {
        "round": round_num,
        "failed": [dict(r) for r in failed_rows],
        "updated_at": datetime.now().isoformat(),
    }
    path = _checkpoint_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    logger.info(f"检查点已保存: 第 {round_num} 轮, 待重试 {len(failed_rows)} 篇")


def _load_checkpoint() -> tuple[int | None, list]:
    """加载检查点，返回 (round_num, failed_rows) 或 (None, [])"""
    path = _checkpoint_path()
    if not path.exists():
        return None, []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data["round"], data["failed"]
    except Exception as e:
        logger.warning(f"检查点读取失败，将从头开始: {e}")
        return None, []


def _clear_checkpoint():
    """验证全部完成后删除检查点"""
    path = _checkpoint_path()
    if path.exists():
        path.unlink()
        logger.info("检查点已清除（全部验证完成）")


def _extract_json(text: str, fix_glm_multi_array: bool = False) -> list | None:
    """多策略从 LLM 响应中提取 JSON 数组，返回 None 表示全部失败"""
    # 1) 剥离 markdown 代码块标记
    s = text.strip()
    if s.startswith("```"):
        s = s.split("\n", 1)[-1]
        s = s.rsplit("```", 1)[0] if "```" in s else s
        s = s.strip()

    # 2) 快速路径: 解析完整字符串
    try:
        data = json.loads(s)
        if isinstance(data, list):
            return data
    except json.JSONDecodeError:
        pass

    # 3) 从第一个 [ 开始
    start = s.find("[")
    if start == -1:
        return None
    json_str = s[start:]

    if fix_glm_multi_array:
        logger.debug(f"s前50字符: {s[:50]!r}")
        logger.debug(f"s后50字符: {s[-50:]!r}")
        logger.debug(f"json_str前300字符: {json_str[:300]!r}")
        logger.debug(f"json_str末100字符: {json_str[-100:]!r}")
        # 4) 修复多数组 + 尾逗号（先跑，保留所有记录）
        normalized = json_str.replace('\r\n', '\n').replace('\n', '')
        fixed = re.sub(r',\s*\]', ']', re.sub(r'\],\s*\[', ',', normalized))
        if fixed != normalized:
            try:
                data = json.loads(fixed)
                if isinstance(data, list):
                    return data
            except json.JSONDecodeError:
                pass
        # 5) 尝试补全被截断的 JSON
        if json_str.startswith('[') and not json_str.rstrip().endswith(']'):
            fixed2 = json_str.rstrip().rstrip(',') + '\n]'
            if fixed2 != json_str:
                try:
                    data = json.loads(fixed2)
                    if isinstance(data, list):
                        return data
                except json.JSONDecodeError:
                    pass

    # 6) raw_decode 总回退（跳过 JSON 后的垃圾内容，只返回第一个数组）
    try:
        decoder = json.JSONDecoder()
        data, idx = decoder.raw_decode(json_str)
        if isinstance(data, list):
            return data
    except (json.JSONDecodeError, ValueError):
        pass

    return None


SYSTEM_PROMPT = (
    "你是一个马铃薯（Solanum tuberosum）研究领域的专家。\n"
    "判断以下每篇文献是否与马铃薯的至少一个研究方向相关：\n"
    "1. 胁迫生物学（干旱、冷害、热害、盐害、病虫害、氧化胁迫、抗性等）\n"
    "2. 生长发育（块茎发育、形态建成、开花、衰老、生长等）\n"
    "3. 基因调控与分子生物学（基因表达、转录因子、调控网络、基因功能等）\n"
    "4. 多组学（基因组、转录组、蛋白质组、代谢组、表观组、miRNA等）\n"
    "5. 功能遗传学（QTL、GWAS、基因定位、分子标记、等位基因等）\n\n"
     "判断标准：\n"
    "- 如果文献主要研究马铃薯本身，且涉及上述任一方向 → RELEVANT\n"
    "- 如果文献仅在其他物种中研究上述方向，不涉及马铃薯 → NOT_RELEVANT\n"
    "- 如果文献涉及马铃薯但仅为实验材料背景，不作核心研究内容 → NOT_RELEVANT\n\n"
     "请以 JSON 对象格式逐条回答，不要包含其他内容：\n"
     'reason 请用中文简要说明判断依据。\n'
     '{"results":[{"pmid":"...","verdict":"RELEVANT 或 NOT_RELEVANT","reason":"判断理由"}]}'
)

INSERT_SQL = """
INSERT OR REPLACE INTO llm_validation
    (pmid, relevance_label, llm_verdict, reason, validated_at)
VALUES (?, ?, ?, ?, ?)
"""

UPDATE_HUMAN_REVIEW_SQL = """
UPDATE llm_validation SET human_review = ? WHERE pmid = ?
"""


def _build_batch_prompt(rows: list) -> str:
    """为一批文献构建 prompt 正文"""
    parts = [
        "以下是需要你根据上述标准判断的马铃薯研究方向文献列表，请逐条判断：\n\n"
    ]
    for i, row in enumerate(rows, 1):
        title = (row["title"] or "").strip()
        abstract = (row["abstract"] or "").strip()
        parts.append(
            f"## 文献 {i}\nPMID: {row['pmid']}\nTitle: {title}\n"
            f"Abstract: {abstract}\n"
        )
    return "\n".join(parts)


def _call_llm(rows: list) -> tuple[list[dict], list[str]]:
    """调用 LLM API 验证一批文献，返回 (成功结果列表, 失败 PMID 列表)"""
    prompt = _build_batch_prompt(rows)
    last_exception = None
    failed_pmids = [row["pmid"] for row in rows]

    if LLM_PROVIDER == "zhipu":
        from zhipuai import ZhipuAI
        api_key = os.environ.get("ZHIPU_API_KEY") or ZHIPU_API_KEY
        if not api_key:
            logger.error("未设置 ZHIPU_API_KEY（环境变量或 config/settings.py）")
            return [], failed_pmids
        client = ZhipuAI(api_key=api_key)
        model = ZHIPU_MODEL
        create_kwargs = {"model": model, "temperature": 0, "max_tokens": LLM_MAX_TOKENS}
        response_attr = "choices"
    elif LLM_PROVIDER == "deepseek":
        api_key = os.environ.get("DEEPSEEK_API_KEY") or DEEPSEEK_API_KEY
        if not api_key:
            logger.error("未设置 DEEPSEEK_API_KEY（环境变量或 config/settings.py）")
            return [], failed_pmids
        client = OpenAI(api_key=api_key, base_url=DEEPSEEK_BASE_URL)
        model = DEEPSEEK_MODEL
        create_kwargs = {"model": model, "temperature": 0, "max_tokens": LLM_MAX_TOKENS, "timeout": 120, "response_format": {"type": "json_object"}}
        response_attr = "choices"
    elif LLM_PROVIDER == "openai":
        api_key = os.environ.get("OPENAI_API_KEY") or OPENAI_API_KEY
        if not api_key:
            logger.error("未设置 OPENAI_API_KEY（环境变量或 config/settings.py）")
            return [], failed_pmids
        client = OpenAI(api_key=api_key, base_url=OPENAI_BASE_URL)
        model = OPENAI_MODEL
        create_kwargs = {"model": model, "temperature": 0, "max_tokens": LLM_MAX_TOKENS, "timeout": 120}
        response_attr = "choices"
    else:
        logger.error(f"未知的 LLM_PROVIDER: {LLM_PROVIDER}")
        return [], failed_pmids

    for attempt in range(1, LLM_MAX_RETRIES + 1):
        try:
            resp = client.chat.completions.create(
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": prompt},
                ],
                **create_kwargs,
            )
            choices = getattr(resp, response_attr)
            content = choices[0].message.content.strip()

            result = None
            try:
                data = json.loads(content)
                if isinstance(data, list):
                    result = data
                elif isinstance(data, dict) and "results" in data and isinstance(data["results"], list):
                    result = data["results"]
            except (json.JSONDecodeError, ValueError):
                pass

            if result is None:
                result = _extract_json(content, fix_glm_multi_array=(LLM_PROVIDER == "zhipu"))
            if result is None:
                raise ValueError(f"无法从 LLM 响应中提取有效 JSON: {content[:200]}")
            return result, []
        except Exception as e:
            last_exception = e
            logger.warning(f"LLM API 调用失败（attempt {attempt}/{LLM_MAX_RETRIES}）: {e}")
            if attempt < LLM_MAX_RETRIES:
                time.sleep(2 ** attempt)

    logger.error(f"LLM API 调用全部失败，跳过该批次: {last_exception}")
    return [], failed_pmids


def run_validation():
    """
    对已评分文献进行 LLM 二次验证，支持多轮重试 + 检查点恢复。
    跳过已有验证结果的 PMID，输出待人工复核的 CSV。
    """
    logger.info("=" * 60)
    logger.info("阶段五：LLM 文献验证")
    logger.info("=" * 60)

    # ── 检查点恢复 ──
    start_round, remaining_rows = _load_checkpoint()
    if remaining_rows:
        with get_conn(DB_PATH) as conn:
            done = set(row["pmid"] for row in
                       conn.execute("SELECT pmid FROM llm_validation").fetchall())
        remaining_rows = [r for r in remaining_rows if r["pmid"] not in done]
        logger.info(f"从检查点恢复: 第 {start_round} 轮, "
                    f"待验证 {len(remaining_rows)} 篇")
        if not remaining_rows:
            _clear_checkpoint()
            logger.info("检查点中的文献均已验证，无需继续")
            csv_path = _export_review_csv()
            if csv_path:
                logger.info(f"待复核清单: {csv_path}")
            return
    else:
        with get_conn(DB_PATH) as conn:
            rows = conn.execute("""
                SELECT a.pmid, a.title, a.abstract, s.label
                FROM articles a
                JOIN relevance_scores s ON a.pmid = s.pmid
                WHERE a.abstract IS NOT NULL AND a.abstract != ''
                  AND a.pmid NOT IN (SELECT pmid FROM llm_validation)
                ORDER BY s.total_score DESC
            """).fetchall()

        if not rows:
            logger.info("没有待验证的文献（所有已评分文献均已验证）")
            return

        start_round = 1
        remaining_rows = rows
        logger.info(f"待验证文献: {len(rows)} 篇, "
                    f"{LLM_MAX_ROUNDS + 1} 轮重试机制")

    # ── 多轮重试循环 ──
    all_validated = []
    for round_num in range(start_round, LLM_MAX_ROUNDS + 2):
        if not remaining_rows:
            break

        round_batches = (len(remaining_rows) + LLM_BATCH_SIZE - 1) // LLM_BATCH_SIZE
        logger.info(f"--- 第 {round_num} 轮: {len(remaining_rows)} 篇, "
                    f"{round_batches} 批 ---")

        round_failed = []

        for start in range(0, len(remaining_rows), LLM_BATCH_SIZE):
            batch = remaining_rows[start:start + LLM_BATCH_SIZE]
            batch_num = start // LLM_BATCH_SIZE + 1
            logger.info(f"  批次 {batch_num}/{round_batches} "
                        f"({len(batch)} 篇)...")

            results, failed_pmids = _call_llm(batch)
            failed_set = set(failed_pmids)

            if results:
                now = datetime.utcnow().isoformat()
                pmid_to_result = {r["pmid"]: r for r in results}
                log_rows = []
                for row in batch:
                    pmid = row["pmid"]
                    if pmid in failed_set:
                        round_failed.append(row)
                        continue
                    r = pmid_to_result.get(pmid, {})
                    log_rows.append((pmid, row["label"],
                                     r.get("verdict", "UNKNOWN"),
                                     r.get("reason", ""), now))

                with get_conn(DB_PATH) as conn:
                    conn.executemany(INSERT_SQL, log_rows)
                all_validated.extend(log_rows)
            else:
                round_failed.extend(batch)

            if batch_num < round_batches:
                time.sleep(1)

        remaining_rows = round_failed
        logger.info(f"  第 {round_num} 轮完成: 累计成功 {len(all_validated)} 篇, "
                    f"仍失败 {len(remaining_rows)} 篇")

        # 每轮结束保存检查点
        _save_checkpoint(round_num + 1, remaining_rows)

    # ── 收尾 ──
    _clear_checkpoint()

    if remaining_rows:
        _export_failed_pmids_csv(remaining_rows)
        logger.warning(f"仍有 {len(remaining_rows)} 篇验证失败, 请检查日志")
    else:
        logger.info("所有文献验证成功")

    if not all_validated:
        logger.warning("所有批次均验证失败，请检查 API 配置")
        return

    verdicts = {}
    for _, _, v, _, _ in all_validated:
        verdicts[v] = verdicts.get(v, 0) + 1
    logger.info("LLM 验证统计:")
    for v, c in sorted(verdicts.items()):
        pct = c / len(all_validated) * 100
        logger.info(f"  {v}: {c} 篇 ({pct:.1f}%)")

    csv_path = _export_review_csv()
    logger.info(f"LLM 验证完成，待复核清单: {csv_path}")


def _export_failed_pmids_csv(failed_rows: list) -> Path | None:
    """导出最终验证失败的 PMID 列表"""
    if not failed_rows:
        return None
    out_dir = Path(OUTPUT_DIR)
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    csv_path = out_dir / f"llm_validation_failed_{ts}.csv"
    with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(["pmid", "title", "abstract_preview"])
        for row in failed_rows:
            writer.writerow([row["pmid"], row["title"], (row["abstract"] or "")[:200]])
    logger.warning(f"仍有 {len(failed_rows)} 篇验证失败: {csv_path}")
    return csv_path


def _export_review_csv() -> Path | None:
    """导出所有待人工复核的记录为 CSV"""
    with get_conn(DB_PATH) as conn:
        rows = conn.execute("""
            SELECT v.pmid, a.title, a.abstract,
                   v.relevance_label, v.llm_verdict, v.reason
            FROM llm_validation v
            JOIN articles a ON a.pmid = v.pmid
            WHERE v.human_review IS NULL
            ORDER BY v.llm_verdict, a.pmid
        """).fetchall()

    if not rows:
        logger.info("没有待复核的记录")
        return None

    out_dir = Path(OUTPUT_DIR)
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    csv_path = out_dir / f"llm_review_pending_{ts}.csv"

    with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow([
            "pmid", "title", "abstract_preview",
            "relevance_label", "llm_verdict", "reason", "human_review",
        ])
        for row in rows:
            writer.writerow([
                row["pmid"],
                row["title"],
                (row["abstract"] or "")[:200],
                row["relevance_label"],
                row["llm_verdict"],
                row["reason"],
                "",
            ])

    logger.info(f"待复核清单已导出: {csv_path}")
    logger.info("请人工标注 human_review 列为 Y 或 N（通过/驳回），然后运行 --step import-review")
    return csv_path


def import_human_review(csv_path: str | None = None):
    """
    导入人工复核结果 CSV，更新审核意见并导出最终过滤结果。
    未指定路径时自动使用最新的 llm_review_pending_*.csv。
    """
    out_dir = Path(OUTPUT_DIR)
    if csv_path:
        review_file = Path(csv_path)
    else:
        candidates = sorted(out_dir.glob("llm_review_pending_*.csv"), reverse=True)
        if not candidates:
            logger.error("未找到 llm_review_pending_*.csv 文件")
            return
        review_file = candidates[0]

    logger.info(f"导入复核文件: {review_file}")

    passed = 0
    rejected = 0
    skipped = 0

    with open(review_file, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        with get_conn(DB_PATH) as conn:
            for row in reader:
                pmid = row.get("pmid", "").strip()
                review = row.get("human_review", "").strip().upper()
                if not pmid:
                    skipped += 1
                    continue
                if review not in ("Y", "N"):
                    skipped += 1
                    continue
                conn.execute(UPDATE_HUMAN_REVIEW_SQL, (review, pmid))
                if review == "Y":
                    passed += 1
                else:
                    rejected += 1

    logger.info(f"导入完成: Y {passed} 篇, N {rejected} 篇, 跳过 {skipped} 行")

    filtered_csv = _export_filtered_csv()
    if filtered_csv:
        logger.info(f"最终过滤结果已导出: {filtered_csv}")

    return {"passed": passed, "rejected": rejected, "skipped": skipped}


def _export_filtered_csv() -> Path | None:
    """导出 LLM 验证 + 人工复核后的最终结果"""
    with get_conn(DB_PATH) as conn:
        rows = conn.execute("""
            SELECT a.pmid, a.title, a.abstract, a.keywords, a.mesh_terms,
                   a.pub_year, a.journal, a.doi, a.pmc_id,
                   a.article_types, a.authors, a.affiliation, a.language,
                   s.gene_hits, s.function_hits, s.trait_hits,
                   s.total_score, s.has_all_three, s.label AS relevance_label,
                   v.llm_verdict, v.reason, v.human_review
            FROM articles a
            JOIN relevance_scores s ON a.pmid = s.pmid
            JOIN llm_validation v ON a.pmid = v.pmid
            WHERE v.human_review = 'Y'
               OR (v.human_review IS NULL AND v.llm_verdict = 'RELEVANT')
            ORDER BY s.total_score DESC
        """).fetchall()

    if not rows:
        logger.info("没有符合条件的最终结果")
        return None

    out_dir = Path(OUTPUT_DIR)
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    csv_path = out_dir / f"llm_filtered_{ts}.csv"

    fieldnames = [
        "pmid", "title", "abstract", "keywords", "mesh_terms",
        "pub_year", "journal", "doi", "pmc_id",
        "article_types", "authors", "affiliation", "language",
        "gene_hits", "function_hits", "trait_hits",
        "total_score", "has_all_three", "relevance_label",
        "llm_verdict", "llm_reason", "human_review",
    ]

    with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            d = dict(row)
            d["llm_reason"] = d.pop("reason", "")
            writer.writerow(d)

    logger.info(f"最终过滤结果: {csv_path} ({len(rows)} 篇)")
    return csv_path
