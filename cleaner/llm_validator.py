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
from concurrent.futures import ThreadPoolExecutor, as_completed

from openai import OpenAI

from config.settings import (
    DB_PATH, OUTPUT_DIR, LOG_DIR,
    LLM_BATCH_SIZE, LLM_CONCURRENCY, LLM_MAX_TOKENS, LLM_MAX_RETRIES, LLM_MAX_ROUNDS,
    LLM_PROVIDER, LLM_PROVIDER_CONFIGS,
    LLM_BATCH_POLL_INTERVAL, LLM_BATCH_TIMEOUT, LLM_BATCH_AUTO_DELETE,
    ZHIPU_API_KEY, ZHIPU_BATCH_MODEL,
    RESEARCH_FIELDS, DEFAULT_RESEARCH_FIELD,
)
from utils import now_iso
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
    "你是一个马铃薯（Solanum tuberosum）文献数据库建设的专家。\n"
    "本项目的目标是收录所有与马铃薯相关的文献，不限研究方向：\n"
    "分子生物学、遗传育种、栽培生理、植物保护、加工储藏、品质营养、\n"
    "产后处理、产业经济、综述评论等均在收录范围内。\n\n"
    # ── 选型依据（见 bench_prompts.py 的 V3_CHECK）──
    # 300 篇基准集、每版本 3 次运行多数票、双模型配对口径：
    #   glm-4-Flash 金标准 168/170 = 0.9882，deepseek-v4.1-flash 179/180 = 0.9944，
    #   跨模型平均 0.9912，为 7 个候选版本中最高。
    # 曾试过加「物种甄别」块（V5/V6/V7）：glm 上甘薯误收从 13% 降到 3%，
    # 但 deepseek 上反而多误排一篇正例（31277248，标题明确 "in Solanum tuberosum"），
    # 且提示词长 500 字符。V7 试图删掉「其他 Solanum 属一律排除」那句以消除规则
    # 冲突，结果 glm 上甘薯误收反弹到 4/30——那句是物种甄别的严格性锚点，删不得。
    # 结论：当前生产跑 deepseek，取 V3 的零真错 + 更短提示词。
    "判断标准只有一个核心问题：这篇文献是否以马铃薯（或其近缘种、\n"
    "其块茎/植株/种质资源、其病虫害互作、其加工产品）为研究对象\n"
    "或重要研究材料。\n\n"
    "判断标准：\n"
    "- 以马铃薯的任何方面为研究对象：基因功能、性状、育种、栽培、\n"
    "  病虫害、生理、加工、储藏、品质、营养、经济等 → RELEVANT\n"
    "- 以马铃薯近缘种/野生种（Solanum sect. Petota 等）为研究对象 → RELEVANT\n"
    "- 以马铃薯病原物（晚疫病菌、青枯菌、线虫、病毒等）与马铃薯的\n"
    "  互作，甚至病原物本身的研究（植保背景）→ RELEVANT\n"
    "- 以马铃薯块茎或加工产品（薯片、薯条、淀粉等）为材料的\n"
    " 食品科学研究 → RELEVANT\n"
    "- 马铃薯相关综述、方法学论文（以马铃薯为研究对象或材料）→ RELEVANT\n"
    "- 甘薯/红薯（sweet potato, Ipomoea batatas）为主要研究对象，\n"
    "    马铃薯仅作对比 → NOT_RELEVANT\n"
    "- 马铃薯仅在摘要中顺带提及（如列举多种作物之一），\n"
    "   研究对象与马铃薯无实质关联 → NOT_RELEVANT\n"
    "- 研究对象与马铃薯完全无关 → NOT_RELEVANT\n"
    # ── 「顺带提及」的可执行判定 ──
    # 经基准测试：全样本特异度 0.35 → 0.55（glm），主要是把「通用方法在多
    # 物种上验证」「多物种并列筛选」这两类误收压下去。
    "\n## 「顺带提及」的判定（务必严格）\n"
    "以下情形属于「马铃薯不是研究对象」，一律判 NOT_RELEVANT：\n"
    "1. 通用方法/通用材料研究：研究对象是某种通用技术、试剂盒、\n"
    "   提取流程、测序方法或通用仪器，马铃薯只是被验证的物种之一。\n"
    "   例：某 DNA 提取法在 45 种植物（含马铃薯）上验证 → NOT_RELEVANT\n"
    "2. 多物种并列筛选：马铃薯与水稻、小麦、番茄等并列出现在\n"
    "   物种列表、比较矩阵或统计样本中，没有针对马铃薯的单独结论。\n"
    "3. 马铃薯仅作对照/背景：以其他作物（尤其甘薯 Ipomoea batatas）\n"
    "   或非植物对象（真菌、细菌、动物）为主体，马铃薯只出现在\n"
    "   对比组、培养基成分（如马铃薯葡萄糖琼脂 PDA）、\n"
    "   背景介绍或参考文献里。\n"
    "4. 名称陷阱：sweet potato / 红薯 / 木薯 / 马铃薯淀粉仅作为\n"
    "   通用商品名出现时，需判断主体物种；主体是甘薯等非马铃薯\n"
    "   作物时判 NOT_RELEVANT。\n"
    "\n判定问句（必须能对第一条回答「是」才判 RELEVANT）：\n"
    "- 若把马铃薯从这篇文献里去掉，文献的核心结论是否还能成立？\n"
    "  能成立 → 马铃薯不是研究对象 → NOT_RELEVANT\n"
    "- 文献的标题或摘要主体句，是否以马铃薯（或其产品/病原互作）\n"
    "  为主语或核心实验材料？\n"
    # ── 输出前自检 ──
    # 经基准测试：加上这块后，模型「理由里写着顺带提及、结论却判 RELEVANT」
    # 的自相矛盾基本消失，是 V1→V3 特异度提升的主要来源。
    "\n## 输出前自检\n"
    "逐条判定后，先在心里回答这两个问题，再决定 verdict：\n"
    "  Q1 马铃薯是否是核心研究对象或关键实验材料？（是/否）\n"
    "  Q2 马铃薯是否只出现在物种列表、对比组、通用方法或背景中？（是/否）\n"
    "Q1=否 或 Q2=是 → NOT_RELEVANT；否则 RELEVANT。\n"
    "若理由中出现了「仅作对比」「顺带提及」「验证物种之一」「通用方法」\n"
    "等措辞，则 verdict 必须是 NOT_RELEVANT，两者不得矛盾。\n"
    "\n## 研究方向分类\n"
    "对判为 RELEVANT 的文献，还必须从下列枚举中选择一个最贴合的\n"
    "研究方向，写入 research_field 字段（只能选一个，不得自造标签）：\n"
    + "".join(f"  - {f}\n" for f in RESEARCH_FIELDS) +
    "判为 NOT_RELEVANT 时 research_field 填空字符串 \"\"。\n\n"
    "请以 JSON 对象格式逐条回答，不要包含其他内容：\n"
    '{"results":[{"pmid":"...","verdict":"RELEVANT 或 NOT_RELEVANT",'
    '"reason":"请用中文简要说明判断依据，指出核心研究对象是否为马铃薯",'
    '"research_field":"研究方向枚举之一，NOT_RELEVANT 时留空"}]}'
)

INSERT_SQL = """
INSERT INTO llm_validation
    (pmid, llm_verdict, reason, validated_at, research_field, human_review)
VALUES (?, ?, ?, ?, ?, NULL)
ON CONFLICT(pmid) DO UPDATE SET
    llm_verdict = excluded.llm_verdict,
    reason = excluded.reason,
    validated_at = excluded.validated_at,
    research_field = excluded.research_field
"""

UPDATE_HUMAN_REVIEW_SQL = """
UPDATE llm_validation SET human_review = ? WHERE pmid = ?
"""

# 摘要为空转人工的文献没有 llm_validation 行，人工判完必须建行并直接带 human_review。
# 不能复用 INSERT_SQL：它的 human_review 是字面量 NULL（LLM 验证路径要求重置为待复核），
# 用它会让人工结论被写丢，文献又回到待复核队列。
INSERT_MANUAL_REVIEW_SQL = """
INSERT INTO llm_validation
    (pmid, llm_verdict, reason, validated_at, research_field, human_review)
VALUES (?, ?, ?, ?, ?, ?)
ON CONFLICT(pmid) DO UPDATE SET
    llm_verdict = excluded.llm_verdict,
    reason = excluded.reason,
    validated_at = excluded.validated_at,
    research_field = excluded.research_field,
    human_review = excluded.human_review
"""

# 摘要为空、跳过 LLM 直接转人工的文献在复核清单里的 verdict 占位值。
# 刻意不用 RELEVANT / NOT_RELEVANT：那会让"模型没看过"伪装成"模型判过"，
# 人工据此照抄就会把未验证的结论当成已验证的结论。
NEEDS_REVIEW_VERDICT = "NEEDS_REVIEW"

# 人工复核清单列：比 llm_validation 侧多出 pub_year / journal / article_types，
# 专为"无摘要"文献服务——人工只能靠标题+期刊+年份+文章类型判断相关性。
REVIEW_PENDING_FIELDS = [
    "pmid", "title", "abstract", "pub_year", "journal", "article_types",
    "llm_verdict", "reason", "research_field", "human_review",
]


def _build_batch_prompt(rows: list) -> str:
    """为一批文献构建 prompt 正文"""
    parts = [
        "以下是需要你根据上述标准判断的文献列表，请逐条判断其核心研究对象是否为马铃薯：\n\n"
    ]
    for i, row in enumerate(rows, 1):
        title = (row["title"] or "").strip()
        abstract = (row["abstract"] or "").strip()
        parts.append(
            f"## 文献 {i}\nPMID: {row['pmid']}\nTitle: {title}\n"
            f"Abstract: {abstract}\n"
        )
    return "\n".join(parts)


def _build_client() -> tuple:
    """
    根据 LLM_PROVIDER 配置创建 client，返回 (client, model, extra_kwargs, fix_multi_array)。
    失败时返回 (None, None, None, None)。
    """
    cfg = LLM_PROVIDER_CONFIGS.get(LLM_PROVIDER)
    if not cfg:
        logger.error(f"未知的 LLM_PROVIDER: {LLM_PROVIDER}")
        return None, None, None, None

    api_key = os.environ.get(cfg["api_key_env"]) or cfg["api_key_fallback"]
    if not api_key:
        logger.error(f"未设置 {cfg['api_key_env']}（环境变量或 config/settings.py）")
        return None, None, None, None

    if cfg["client_type"] == "zhipuai":
        from zhipuai import ZhipuAI
        client = ZhipuAI(api_key=api_key)
    else:
        client = OpenAI(api_key=api_key, base_url=cfg["base_url"])

    extra_kwargs = {k: v for k, v in cfg["extra_kwargs"].items()}
    extra_kwargs["model"] = cfg["model"]
    extra_body = cfg.get("extra_body")
    if extra_body:
        extra_kwargs["extra_body"] = extra_body

    return client, cfg["model"], extra_kwargs, cfg["fix_multi_array"]


def _call_llm(rows: list) -> tuple[list[dict], list[str]]:
    """调用 LLM API 验证一批文献，返回 (成功结果列表, 失败 PMID 列表)"""
    prompt = _build_batch_prompt(rows)
    last_exception = None
    failed_pmids = [row["pmid"] for row in rows]

    client, model, create_kwargs, fix_multi_array = _build_client()
    if client is None:
        return [], failed_pmids

    response_attr = "choices"

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
                result = _extract_json(content, fix_glm_multi_array=fix_multi_array)
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


def run_validation(batch_mode: bool = False):
    """
    LLM 验证路由入口。

    batch_mode=True 且 LLM_PROVIDER=zhipu → 走智谱 Batch API。
    batch_mode=True 但 provider 非 zhipu → 警告后降级同步模式。
    batch_mode=False → 走同步模式（现有逻辑）。
    """
    logger.info("=" * 60)
    logger.info("阶段五：LLM 文献验证")
    logger.info("=" * 60)

    if batch_mode:
        if LLM_PROVIDER != "zhipu":
            logger.warning(
                "Batch API 仅支持智谱提供者（当前 provider=%s），"
                "已自动降级为同步模式", LLM_PROVIDER
            )
            _run_sync_validation()
            return
        _run_zhipu_batch()
        return

    _run_sync_validation()


def _normalize_verdict(value) -> str | None:
    """
    将 LLM 返回的 verdict 归一化为标准值（RELEVANT / NOT_RELEVANT）。
    无法识别（缺失、截断残缺、其他取值）返回 None，由调用方判为失败重试。
    """
    if not value:
        return None
    compact = str(value).strip().upper().replace(" ", "").replace("-", "").replace("_", "")
    if compact == "RELEVANT":
        return "RELEVANT"
    if compact in ("NOTRELEVANT", "IRRELEVANT"):
        return "NOT_RELEVANT"
    return None


def _normalize_research_field(value, verdict: str) -> str:
    """
    将 LLM 返回的 research_field 归一化到 RESEARCH_FIELDS 枚举。

    - NOT_RELEVANT：不分类，一律返回 ""（不相关文献没有研究方向）
    - 缺失 / 非字符串 / 不在枚举内：回退 DEFAULT_RESEARCH_FIELD（兜底，不判失败）
      —— 方向只是附加维度，不值得为它整篇文献回退重试；
      这也会把模型自造的标签（如 "分子生物学 " / "植物病理"）收敛到枚举。
    """
    if verdict != "RELEVANT":
        return ""
    if not isinstance(value, str):
        return DEFAULT_RESEARCH_FIELD
    field = value.strip()
    if field in RESEARCH_FIELDS:
        return field
    return DEFAULT_RESEARCH_FIELD


def _build_log_rows(batch, pmid_to_result, failed_set, now):
    """
    将一批 LLM 结果整理为待入库 log_rows
    （5 元组：pmid, verdict, reason, now, research_field）
    与失败列表。verdict 缺失或非标准视为解析失败（回退重试），不写 UNKNOWN。
    research_field 缺失/越界不判失败，回退 DEFAULT_RESEARCH_FIELD（见
    _normalize_research_field）。
    """
    log_rows = []
    round_failed = []
    for row in batch:
        pmid = row["pmid"]
        if pmid in failed_set:
            round_failed.append(row)
            continue
        r = pmid_to_result.get(pmid)
        if r is None:
            round_failed.append(row)
            continue
        verdict = _normalize_verdict(r.get("verdict"))
        if verdict is None:
            round_failed.append(row)
            continue
        research_field = _normalize_research_field(r.get("research_field"), verdict)
        log_rows.append((pmid, verdict, r.get("reason", ""), now, research_field))
    return log_rows, round_failed


def _count_verdicts(validated_rows: list) -> dict:
    """统计 log_rows（5 元组: pmid, verdict, reason, now, research_field）中各 verdict 数量"""
    verdicts = {}
    for _, v, _, _, _ in validated_rows:
        verdicts[v] = verdicts.get(v, 0) + 1
    return verdicts


def _count_research_fields(conn) -> dict:
    """统计各研究方向的文献数（仅含 RELEVANT）"""
    return {
        r[0]: r[1]
        for r in conn.execute(
            "SELECT research_field, COUNT(*) FROM llm_validation "
            "WHERE llm_verdict = 'RELEVANT' AND research_field IS NOT NULL "
            "AND research_field != '' GROUP BY research_field ORDER BY 2 DESC"
        )
    }


def _run_sync_validation():
    """
    同步多轮 LLM 验证（原 run_validation 逻辑）。
    支持多轮重试 + 检查点恢复。
    跳过已有验证结果的 PMID，输出待人工复核的 CSV。
    """
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
                SELECT a.pmid, a.title, a.abstract
                FROM articles a
                WHERE a.abstract IS NOT NULL AND a.abstract != ''
                  AND a.pmid NOT IN (SELECT pmid FROM llm_validation)
                  AND a.pmid NOT IN (
                      SELECT pmid FROM filter_log
                      WHERE stage IN ('hard_filter', 'needs_review')
                  )
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
        batches = []
        for start in range(0, len(remaining_rows), LLM_BATCH_SIZE):
            batches.append(remaining_rows[start:start + LLM_BATCH_SIZE])
        logger.info(f"  共 {round_batches} 批, 并发 {LLM_CONCURRENCY} 路")

        with ThreadPoolExecutor(max_workers=LLM_CONCURRENCY) as executor:
            future_to_batch = {
                executor.submit(_call_llm, batch): batch
                for batch in batches
            }
            completed = 0
            for future in as_completed(future_to_batch):
                batch = future_to_batch[future]
                batch_num = batches.index(batch) + 1
                try:
                    results, failed_pmids = future.result()
                except Exception as e:
                    logger.error(f"  批次 {batch_num}/{round_batches} 异常: {e}")
                    round_failed.extend(batch)
                    completed += 1
                    continue

                failed_set = set(failed_pmids)
                if results:
                    now = now_iso()
                    pmid_to_result = {r["pmid"]: r for r in results}
                    log_rows, batch_failed = _build_log_rows(
                        batch, pmid_to_result, failed_set, now
                    )
                    round_failed.extend(batch_failed)

                    if log_rows:
                        with get_conn(DB_PATH) as conn:
                            conn.executemany(INSERT_SQL, log_rows)
                        all_validated.extend(log_rows)
                else:
                    round_failed.extend(batch)

                completed += 1
                logger.info(f"  [{completed}/{round_batches}] "
                            f"批次 {batch_num} 完成"
                            f"({'成功' if results else '全部失败'})")

        remaining_rows = round_failed
        logger.info(f"  第 {round_num} 轮完成: 累计成功 {len(all_validated)} 篇, "
                    f"失败 {len(remaining_rows)} 篇")

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

    verdicts = _count_verdicts(all_validated)
    logger.info("LLM 验证统计:")
    for v, c in sorted(verdicts.items()):
        pct = c / len(all_validated) * 100
        logger.info(f"  {v}: {c} 篇 ({pct:.1f}%)")

    _log_research_field_stats()

    csv_path = _export_review_csv()
    logger.info(f"LLM 验证完成，待复核清单: {csv_path}")


def _run_sync_validation_for_pmids(pmid_list: list):
    """对指定的 PMID 列表执行同步多轮验证（用于 batch 降级）"""
    if not pmid_list:
        return
    with get_conn(DB_PATH) as conn:
        placeholders = ",".join("?" for _ in pmid_list)
        rows = conn.execute(f"""
            SELECT pmid, title, abstract
            FROM articles
            WHERE pmid IN ({placeholders})
              AND abstract IS NOT NULL AND abstract != ''
              AND pmid NOT IN (
                  SELECT pmid FROM filter_log
                  WHERE stage IN ('hard_filter', 'needs_review')
              )
        """, pmid_list).fetchall()
    if not rows:
        logger.info("降级的 PMID 列表中无限有效摘要的文献，跳过")
        return
    logger.info(f"同步降级: 对 {len(rows)} 篇文献执行同步验证")

    rows_list = [dict(r) for r in rows]
    remaining_rows = rows_list
    all_validated = []
    # ─────────────────────────────────────────────────────────────────
    # 注意：本轮循环与 _run_sync_validation() 的轮循环"刻意不同"，请勿直接合并：
    #   1. 不读写 llm_validation 检查点文件——若此处也写检查点，会覆盖主流程
    #      （--step validate）的断点状态，导致下次恢复时 start_round 错误，
    #      静默跳过或重复计费一批文献；
    #   2. 轮内不打任何日志、异常静默丢弃——batch 降级重试需保持零输出，
    #      改动会破坏该路径的日志契约，使真实失败被噪音淹没；
    #   3. 行来自调用方给定的 PMID 列表，且已转为 dict（主流程是 sqlite3.Row）。
    # 若确需去重，先补齐特征化测试（降级静默 / 降级重试 / 主流程日志序列 /
    # 主流程失败 / 检查点恢复共 5 例）确认日志序列可确定性断言，再抽取
    # _run_sync_round(remaining_rows, *, log_progress: bool) 一类的公共块。
    # ─────────────────────────────────────────────────────────────────
    for round_num in range(1, LLM_MAX_ROUNDS + 2):
        if not remaining_rows:
            break
        round_failed = []
        batches = []
        for start in range(0, len(remaining_rows), LLM_BATCH_SIZE):
            batches.append(remaining_rows[start:start + LLM_BATCH_SIZE])
        with ThreadPoolExecutor(max_workers=LLM_CONCURRENCY) as executor:
            future_to_batch = {
                executor.submit(_call_llm, batch): batch
                for batch in batches
            }
            for future in as_completed(future_to_batch):
                batch = future_to_batch[future]
                try:
                    results, failed_pmids = future.result()
                except Exception:
                    round_failed.extend(batch)
                    continue
                failed_set = set(failed_pmids)
                if results:
                    now = now_iso()
                    pmid_to_result = {r["pmid"]: r for r in results}
                    log_rows, batch_failed = _build_log_rows(
                        batch, pmid_to_result, failed_set, now
                    )
                    round_failed.extend(batch_failed)
                    if log_rows:
                        with get_conn(DB_PATH) as conn:
                            conn.executemany(INSERT_SQL, log_rows)
                        all_validated.extend(log_rows)
                else:
                    round_failed.extend(batch)
        remaining_rows = round_failed
    if all_validated:
        logger.info(f"同步降级完成: {len(all_validated)} 篇成功")
    if remaining_rows:
        logger.warning(f"同步降级仍有 {len(remaining_rows)} 篇失败")


def _log_research_field_stats() -> None:
    """输出研究方向分布（全库口径，仅 RELEVANT 且有方向的记录）"""
    with get_conn(DB_PATH) as conn:
        counts = _count_research_fields(conn)
    if not counts:
        return
    total = sum(counts.values())
    logger.info("研究方向分布（仅 RELEVANT）:")
    for field, cnt in counts.items():
        logger.info(f"  {field:<12} {cnt:>6} 篇 ({cnt / total * 100:.1f}%)")


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
            writer.writerow([row["pmid"], row["title"], row["abstract"] or ""])
    logger.warning(f"仍有 {len(failed_rows)} 篇验证失败: {csv_path}")
    return csv_path


def _query_review_pending(db_path: Path | None = None) -> list:
    """待人工复核清单：LLM 已验证未复核 + 摘要为空转人工（needs_review）。

    两部分合并顺序刻意让"转人工"排在前面——它们从没被模型看过，
    人工必须逐条读标题/期刊/年份才能判，先处理不易漏。

    转人工行的 llm_verdict 填 NEEDS_REVIEW，避免与 RELEVANT/NOT_RELEVANT
    混淆；reason 沿用硬过滤的 abstract_empty 便于追溯来源。
    """
    target_db = db_path or DB_PATH
    with get_conn(target_db) as conn:
        manual_rows = conn.execute("""
            SELECT a.pmid, a.title, a.abstract, a.pub_year, a.journal,
                   a.article_types, f.reason
            FROM filter_log f
            JOIN articles a ON a.pmid = f.pmid
            WHERE f.stage = 'needs_review'
              AND NOT EXISTS (
                  SELECT 1 FROM llm_validation v WHERE v.pmid = a.pmid
              )
            ORDER BY a.pmid
        """).fetchall()

        llm_rows = conn.execute("""
            SELECT v.pmid, a.title, a.abstract, a.pub_year, a.journal,
                   a.article_types, v.llm_verdict, v.reason, v.research_field
            FROM llm_validation v
            JOIN articles a ON a.pmid = v.pmid
            WHERE v.human_review IS NULL
            ORDER BY v.llm_verdict DESC, a.pmid
        """).fetchall()

    rows: list[dict] = []
    for r in manual_rows:
        rows.append({
            "pmid": r["pmid"],
            "title": r["title"],
            "abstract": r["abstract"],
            "pub_year": r["pub_year"],
            "journal": r["journal"],
            "article_types": r["article_types"],
            "llm_verdict": NEEDS_REVIEW_VERDICT,
            "reason": r["reason"],
            "research_field": "",
        })
    for r in llm_rows:
        rows.append({
            "pmid": r["pmid"],
            "title": r["title"],
            "abstract": r["abstract"],
            "pub_year": r["pub_year"],
            "journal": r["journal"],
            "article_types": r["article_types"],
            "llm_verdict": r["llm_verdict"],
            "reason": r["reason"],
            "research_field": r["research_field"] or "",
        })
    return rows


def _export_review_csv(db_path: Path | None = None) -> Path | None:
    """导出所有待人工复核的记录为 CSV（含摘要为空转人工的文献）"""
    rows = _query_review_pending(db_path)

    if not rows:
        logger.info("没有待复核的记录")
        return None

    out_dir = Path(OUTPUT_DIR)
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    csv_path = out_dir / f"llm_review_pending_{ts}.csv"

    with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(REVIEW_PENDING_FIELDS)
        for row in rows:
            writer.writerow([
                row["pmid"],
                row["title"],
                row["abstract"] or "",
                row["pub_year"] if row["pub_year"] is not None else "",
                row["journal"] or "",
                row["article_types"] or "",
                row["llm_verdict"],
                row["reason"],
                row["research_field"],
                "",
            ])

    logger.info(f"待复核清单已导出: {csv_path}（{len(rows)} 篇）")
    logger.info("请人工标注 human_review 列为 Y 或 N（通过/驳回），然后运行 --step import-review")
    return csv_path


# ═══════════════════════════════════════════════════════════════
# 智谱 Batch API（仅 zhipu provider）
# ═══════════════════════════════════════════════════════════════

BATCH_CHECKPOINT_FILE = "llm_batch_progress.json"

PROMPT_PREFIX = (
    "请根据上述标准判断以下文献是否以马铃薯（或其近缘种、块茎、病虫害、"
    "加工产品等）为研究对象或重要研究材料，并对相关文献指定研究方向。\n\n"
)

PROMPT_OUTPUT_FORMAT = (
    '# 输出格式（仅输出如下 JSON，不要其他文字）：\n'
    '{"pmid": "%s", "verdict": "RELEVANT 或 NOT_RELEVANT", '
    '"reason": "用中文简要说明判断依据", '
    '"research_field": "研究方向（仅当 verdict=RELEVANT 时填写，否则空字符串）"}'
)


def _batch_checkpoint_path() -> Path:
    return Path(OUTPUT_DIR) / BATCH_CHECKPOINT_FILE


def _save_batch_checkpoint(data: dict):
    path = _batch_checkpoint_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    logger.info(f"Batch 检查点已保存: batch_id={data.get('batch_id', 'N/A')}")


def _load_batch_checkpoint() -> dict | None:
    path = _batch_checkpoint_path()
    if not path.exists():
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.warning(f"Batch 检查点读取失败，将重新提交: {e}")
        return None


def _clear_batch_checkpoint():
    path = _batch_checkpoint_path()
    if path.exists():
        path.unlink()
        logger.info("Batch 检查点已清除")


def _build_per_article_prompt(pmid: str, title: str, abstract: str) -> str:
    return (
        f"{PROMPT_PREFIX}"
        f"PMID: {pmid}\n"
        f"Title: {title or ''}\n"
        f"Abstract: {abstract or ''}\n\n"
        f"{PROMPT_OUTPUT_FORMAT % pmid}"
    )


def _build_jsonl(rows: list) -> Path:
    """构建 batch JSONL 文件（每篇一行），返回文件路径"""
    out_dir = Path(OUTPUT_DIR)
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    jsonl_path = out_dir / f"batch_input_{ts}.jsonl"

    model = ZHIPU_BATCH_MODEL
    system_content = SYSTEM_PROMPT

    with open(jsonl_path, "w", encoding="utf-8") as f:
        for row in rows:
            pmid = row["pmid"]
            article_prompt = _build_per_article_prompt(
                pmid, row["title"] or "", row["abstract"] or ""
            )
            req = {
                "custom_id": pmid,
                "method": "POST",
                "url": "/v4/chat/completions",
                "body": {
                    "model": model,
                    "messages": [
                        {"role": "system", "content": system_content},
                        {"role": "user", "content": article_prompt},
                    ],
                    "temperature": 0,
                    "max_tokens": LLM_MAX_TOKENS,
                },
            }
            f.write(json.dumps(req, ensure_ascii=False) + "\n")

    file_size_mb = jsonl_path.stat().st_size / (1024 * 1024)
    logger.info(f"JSONL 已构建: {jsonl_path} ({len(rows)} 行, {file_size_mb:.2f} MB)")
    return jsonl_path


def _parse_batch_results(jsonl_path: str, db_path: str | None = None) -> tuple[int, list[str]]:
    """
    解析 batch 输出结果 JSONL，写入 llm_validation 表。
    返回 (成功数, 失败 PMID 列表)。

    db_path 仅供测试注入临时库；生产调用省略，走模块级 DB_PATH。
    """
    success_count = 0
    failed_pmids = []
    target_db = db_path or DB_PATH

    with open(jsonl_path, "r", encoding="utf-8") as f:
        for line_num, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                logger.warning(f"Batch 结果行 {line_num} JSON 解析失败")
                failed_pmids.append("")
                continue

            custom_id = item.get("custom_id", "")
            pmid = custom_id

            resp = item.get("response", {})
            if resp.get("status_code") != 200:
                logger.warning(f"Batch 请求失败: pmid={pmid}, status={resp.get('status_code')}")
                failed_pmids.append(pmid)
                continue

            body = resp.get("body", {})
            choices = body.get("choices", [])
            if not choices:
                failed_pmids.append(pmid)
                continue

            content = choices[0].get("message", {}).get("content", "").strip()
            if not content:
                failed_pmids.append(pmid)
                continue

            # 统一 strip markdown（glm-4-flash 常返回 ```json ... ```）
            clean = content.strip()
            if clean.startswith("```"):
                clean = clean.split("\n", 1)[-1]
                clean = clean.rsplit("```", 1)[0] if "```" in clean else clean
                clean = clean.strip()

            # 优先直接解析为单 JSON 对象（batch 每篇独立请求的预期格式）
            obj = None
            try:
                obj = json.loads(clean)
                if not isinstance(obj, dict):
                    obj = None
            except (json.JSONDecodeError, ValueError):
                pass

            # 若失败，回退 _extract_json 处理数组/results 格式
            if obj is None:
                parsed = _extract_json(content, fix_glm_multi_array=True)
                if parsed:
                    obj = parsed[0] if isinstance(parsed, list) else parsed

            if obj is None:
                logger.warning(f"Batch 结果解析失败: pmid={pmid}, content={content[:200]}")
                failed_pmids.append(pmid)
                continue

            verdict = _normalize_verdict(obj.get("verdict"))
            if verdict is None:
                logger.warning(
                    f"Batch 结果 verdict 无法识别（判为失败重试）: "
                    f"pmid={pmid}, verdict={obj.get('verdict')!r}"
                )
                failed_pmids.append(pmid)
                continue
            reason = obj.get("reason", "")
            research_field = _normalize_research_field(
                obj.get("research_field"), verdict
            )
            now = now_iso()

            with get_conn(target_db) as conn:
                conn.execute(INSERT_SQL,
                             (pmid, verdict, reason, now, research_field))
            success_count += 1

    return success_count, failed_pmids


def _run_zhipu_batch():
    """智谱 Batch API 主流程：JSONL 构建 → 上传 → 提交 → 轮询 → 下载 → 解析 → 降级"""
    api_key = os.environ.get("ZHIPU_API_KEY") or ZHIPU_API_KEY
    if not api_key:
        logger.error("未设置 ZHIPU_API_KEY，无法使用 Batch API")
        return

    from zhipuai import ZhipuAI
    client = ZhipuAI(api_key=api_key)

    # ── 步骤 1：检查 checkpoint 恢复 ──
    chk = _load_batch_checkpoint()
    if chk:
        batch_id = chk.get("batch_id")
        logger.info(f"发现 Batch 检查点: batch_id={batch_id}, status={chk.get('status')}")
        try:
            batch_status = client.batches.retrieve(batch_id)
            st = batch_status.status
            if st == "completed":
                logger.info(f"Batch 任务已完成，直接下载结果")
                _download_and_parse_batch(client, batch_status, chk)
                _finalize_batch(chk)
                return
            elif st in ("in_progress", "finalizing", "validating"):
                logger.info(f"恢复轮询 batch_id={batch_id}")
                _poll_until_done(client, batch_id, chk)
                _finalize_batch(chk)
                return
            else:
                logger.warning(f"Batch 任务已结束（status={st}），降级为同步模式")
                _clear_batch_checkpoint()
                _run_sync_validation_for_pmids(chk.get("pmid_list", []))
                return
        except Exception as e:
            logger.warning(f"查询 batch 状态失败: {e}，删除检查点并重新提交")
            _clear_batch_checkpoint()

    # ── 步骤 2：加载待验证文献 ──
    with get_conn(DB_PATH) as conn:
        rows = conn.execute("""
            SELECT pmid, title, abstract
            FROM articles
            WHERE abstract IS NOT NULL AND abstract != ''
              AND pmid NOT IN (SELECT pmid FROM llm_validation)
              AND pmid NOT IN (
                  SELECT pmid FROM filter_log
                  WHERE stage IN ('hard_filter', 'needs_review')
              )
        """).fetchall()

    if not rows:
        logger.info("没有待验证文献")
        return

    rows_list = [dict(r) for r in rows]
    all_pmids = [r["pmid"] for r in rows_list]
    logger.info(f"待验证文献: {len(rows_list)} 篇（Batch 模式）")

    # ── 步骤 3：构建 JSONL ──
    jsonl_path = _build_jsonl(rows_list)

    # ── 步骤 4：上传文件 ──
    logger.info("上传 Batch 文件...")
    try:
        file_obj = client.files.create(
            file=open(jsonl_path, "rb"),
            purpose="batch",
        )
        logger.info(f"文件已上传: {file_obj.id}")
    except Exception as e:
        logger.error(f"上传文件失败: {e}，降级为同步模式")
        _run_sync_validation_for_pmids(all_pmids)
        return

    # ── 步骤 5：创建 Batch 任务 ──
    logger.info("创建 Batch 任务...")
    try:
        batch = client.batches.create(
            input_file_id=file_obj.id,
            endpoint="/v4/chat/completions",
            auto_delete_input_file=LLM_BATCH_AUTO_DELETE,
            metadata={
                "description": "Potato literature LLM validation",
                "project": "potato-literature-search",
            },
        )
        logger.info(f"Batch 任务已提交: {batch.id}")
    except Exception as e:
        logger.error(f"创建 Batch 任务失败: {e}，降级为同步模式")
        _run_sync_validation_for_pmids(all_pmids)
        return

    _save_batch_checkpoint({
        "batch_id": batch.id,
        "input_file_id": file_obj.id,
        "pmid_list": all_pmids,
        "status": "active",
        "created_at": now_iso(),
        "updated_at": now_iso(),
    })

    # ── 步骤 6：轮询 ──
    _poll_until_done(client, batch.id, {
        "batch_id": batch.id,
        "input_file_id": file_obj.id,
        "pmid_list": all_pmids,
        "status": "active",
        "created_at": now_iso(),
        "updated_at": now_iso(),
    })

    _finalize_batch({
        "batch_id": batch.id,
        "pmid_list": all_pmids,
    })


def _poll_until_done(client, batch_id: str, chk: dict):
    """轮询 Batch 任务直到完成/失败/超时"""
    import sys

    start_time = time.time()
    while True:
        elapsed = int(time.time() - start_time)
        if elapsed >= LLM_BATCH_TIMEOUT:
            logger.error(f"Batch 任务超时（>{LLM_BATCH_TIMEOUT}s），取消任务并降级同步")
            try:
                client.batches.cancel(batch_id)
            except Exception:
                pass
            _clear_batch_checkpoint()
            _run_sync_validation_for_pmids(chk.get("pmid_list", []))
            return

        try:
            status = client.batches.retrieve(batch_id)
        except Exception as e:
            logger.error(f"查询 Batch 状态失败: {e}")
            time.sleep(LLM_BATCH_POLL_INTERVAL)
            continue

        st = status.status
        counts = status.request_counts
        total = getattr(counts, "total", 0) or 0
        completed = getattr(counts, "completed", 0) or 0
        failed = getattr(counts, "failed", 0) or 0

        progress = f"完成 {completed}/{total}" if total else "排队中"
        sys.stdout.write(
            f"\r  ⏳ 任务状态: {st} | {progress} | 失败: {failed} "
            f"| 用时: {elapsed}s  "
        )
        sys.stdout.flush()

        if st == "completed":
            sys.stdout.write("\n")
            sys.stdout.flush()
            logger.info("Batch 任务完成!")

            chk["status"] = "completed"
            chk["updated_at"] = now_iso()
            _save_batch_checkpoint(chk)

            _download_and_parse_batch(client, status, chk)
            return
        elif st in ("failed", "expired", "cancelled"):
            sys.stdout.write("\n")
            sys.stdout.flush()
            logger.error(f"Batch 任务失败（status={st}），降级为同步模式")
            _clear_batch_checkpoint()
            _run_sync_validation_for_pmids(chk.get("pmid_list", []))
            return

        time.sleep(LLM_BATCH_POLL_INTERVAL)


def _download_and_parse_batch(client, batch_status, chk: dict):
    """下载 Batch 结果并解析写入 DB，处理 error_file 降级"""
    all_pmids = chk.get("pmid_list", [])

    success_total = 0
    error_pmids = []

    output_id = getattr(batch_status, "output_file_id", None)
    error_id = getattr(batch_status, "error_file_id", None)

    if output_id:
        logger.info("下载结果文件...")
        try:
            content = client.files.content(output_id)
            tmp_path = Path(OUTPUT_DIR) / f"batch_output_{chk['batch_id']}.jsonl"
            tmp_path.parent.mkdir(parents=True, exist_ok=True)
            content.write_to_file(str(tmp_path))

            success_total, error_pmids = _parse_batch_results(str(tmp_path))
            logger.info(f"结果已写入 DB: {success_total} 篇成功, {len(error_pmids)} 篇解析失败")
        except Exception as e:
            logger.error(f"下载/解析输出文件失败: {e}")
            _clear_batch_checkpoint()
            _run_sync_validation_for_pmids(all_pmids)
            return

    if error_id:
        logger.info("下载错误文件...")
        try:
            error_content = client.files.content(error_id)
            error_path = Path(OUTPUT_DIR) / f"batch_errors_{chk['batch_id']}.jsonl"
            error_path.parent.mkdir(parents=True, exist_ok=True)
            error_content.write_to_file(str(error_path))

            with open(error_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        item = json.loads(line)
                        error_pmids.append(item.get("custom_id", ""))
                    except json.JSONDecodeError:
                        pass
        except Exception as e:
            logger.warning(f"下载错误文件失败: {e}")

    # 降级：对 error 中的 PMID 执行同步验证
    if error_pmids:
        logger.info(f"Batch 错误 {len(error_pmids)} 篇，降级为同步模式重试")
        _run_sync_validation_for_pmids(error_pmids)


def _finalize_batch(chk: dict):
    """收尾：清除检查点、输出统计、导出 CSV"""
    _clear_batch_checkpoint()

    with get_conn(DB_PATH) as conn:
        verdicts = conn.execute("""
            SELECT llm_verdict, COUNT(*) as cnt
            FROM llm_validation
            GROUP BY llm_verdict
        """).fetchall()

    logger.info("LLM 验证统计（Batch）:")
    total = 0
    for v, cnt in verdicts:
        total += cnt
    for v, cnt in verdicts:
        logger.info(f"  {v}: {cnt} 篇 ({cnt / total * 100:.1f}%)")

    _log_research_field_stats()

    csv_path_file = _export_review_csv()
    logger.info(f"LLM 验证完成，待复核清单: {csv_path_file}")


def import_human_review(csv_path: str | None = None):
    """
    导入人工复核结果 CSV，更新审核意见并导出最终过滤结果。
    未指定路径时自动使用最新的 llm_review_pending_*.csv。

    两类行分开处理：
    - 有 llm_validation 行（模型已判）：UPDATE human_review。
    - 无 llm_validation 行（摘要为空转人工，verdict 为 NEEDS_REVIEW）：
      人工结论是这篇文献唯一的判定，必须 INSERT 一行，否则
      `_REVIEW_PASS_PREDICATE` 永远匹配不到它，判 Y 也导不出去。
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

    with open(review_file, "r", encoding="utf-8-sig", newline="") as f_check:
        reader_check = csv.DictReader(f_check)
        required_cols = {"pmid", "human_review"}
        if not required_cols.issubset(reader_check.fieldnames or []):
            missing = required_cols - set(reader_check.fieldnames or [])
            logger.error(f"CSV 缺少必需列: {missing}")
            return

    passed = 0
    rejected = 0
    skipped = 0
    updates = []
    manual_rows = []
    now = now_iso()

    with open(review_file, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            pmid = row.get("pmid", "").strip()
            review = row.get("human_review", "").strip().upper()
            if not pmid:
                skipped += 1
                continue
            if review not in ("Y", "N"):
                skipped += 1
                continue
            is_manual = (row.get("llm_verdict", "").strip().upper()
                         == NEEDS_REVIEW_VERDICT)
            if is_manual:
                verdict = "RELEVANT" if review == "Y" else "NOT_RELEVANT"
                field = _normalize_research_field(
                    row.get("research_field", ""), verdict
                )
                manual_rows.append((pmid, verdict,
                                    "人工复核判定（摘要为空，未经 LLM）",
                                    now, field, review))
            else:
                updates.append((review, pmid))
            if review == "Y":
                passed += 1
            else:
                rejected += 1

    if updates:
        with get_conn(DB_PATH) as conn:
            conn.executemany(UPDATE_HUMAN_REVIEW_SQL, updates)
    if manual_rows:
        with get_conn(DB_PATH) as conn:
            conn.executemany(INSERT_MANUAL_REVIEW_SQL, manual_rows)

    logger.info(f"导入完成: Y {passed} 篇, N {rejected} 篇, 跳过 {skipped} 行")
    if manual_rows:
        logger.info(f"其中摘要为空转人工 {len(manual_rows)} 篇（新建 llm_validation 记录）")

    filtered_csv = _export_filtered_csv()
    if filtered_csv:
        logger.info(f"最终过滤结果已导出: {filtered_csv}")

    return {"passed": passed, "rejected": rejected, "skipped": skipped,
            "manual_inserted": len(manual_rows)}


# 复核通过谓词：human_review='Y' 或（未复核且 llm_verdict='RELEVANT'）。
# **必须保留外层括号**：增量路径会在其后追加 "AND v.validated_at > ?"，
# 若去掉括号 SQL 会解析成 "A OR (B AND C)"，人工通过的行每次增量都会被重复导出。
_REVIEW_PASS_PREDICATE = (
    "(v.human_review = 'Y'\n"
    "               OR (v.human_review IS NULL AND v.llm_verdict = 'RELEVANT'))"
)


FILTERED_EXPORT_FIELDS = [
    "pmid", "title", "abstract", "keywords", "mesh_terms",
    "pub_year", "journal", "doi", "pmc_id",
    "article_types", "authors", "affiliation", "language",
    "llm_verdict", "llm_reason", "research_field", "human_review",
]


def _query_filtered_export(db_path: Path | None = None,
                           since_timestamp: str | None = None,
                           *, order_by_validated_at: bool = False) -> list:
    """filtered 导出共用查询：列清单与复核谓词单一来源（与 raw 导出同谓词）。

    比 raw 导出多出 llm_verdict / reason / research_field / human_review 四列，
    研究方向分类只在这条路径上。

    db_path 默认 None 走模块级 DB_PATH（不在签名里绑定默认值，否则测试里
    patch mod.DB_PATH 会失效）。
    """
    target_db = db_path or DB_PATH
    sql = f"""
            SELECT a.pmid, a.title, a.abstract, a.keywords, a.mesh_terms,
                   a.pub_year, a.journal, a.doi, a.pmc_id,
                   a.article_types, a.authors, a.affiliation, a.language,
                   v.llm_verdict, v.reason, v.research_field, v.human_review
            FROM articles a
            JOIN llm_validation v ON a.pmid = v.pmid
            WHERE {_REVIEW_PASS_PREDICATE}
        """
    params: list = []
    if since_timestamp:
        sql += "\n              AND v.validated_at > ?"
        params.append(since_timestamp)
    if order_by_validated_at:
        sql += "\n            ORDER BY v.validated_at"
    with get_conn(target_db) as conn:
        return conn.execute(sql, params).fetchall()


def _export_filtered_csv(db_path: Path | None = None,
                         since_timestamp: str | None = None,
                         *, order_by_validated_at: bool = False) -> Path | None:
    """导出 LLM 验证 + 人工复核后的最终结果（含研究方向分类）

    db_path / since_timestamp 供测试与增量路径注入；生产全量导出省略参数。
    """
    rows = _query_filtered_export(db_path, since_timestamp, order_by_validated_at=order_by_validated_at)

    if not rows:
        logger.info("没有符合条件的最终结果")
        return None

    out_dir = Path(OUTPUT_DIR)
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    if since_timestamp:
        suffix = f"_since_{since_timestamp.replace(':', '-').replace('T', '_')}"
        csv_path = out_dir / f"llm_filtered_incremental{suffix}_{ts}.csv"
    else:
        csv_path = out_dir / f"llm_filtered_{ts}.csv"

    _write_dict_csv(csv_path, FILTERED_EXPORT_FIELDS, rows,
                    transform=_rename_reason_to_llm_reason)

    logger.info(f"最终过滤结果: {csv_path} ({len(rows)} 篇)")
    return csv_path


RAW_EXPORT_FIELDS = [
    "pmid", "title", "abstract", "keywords", "mesh_terms",
    "pub_year", "pub_month", "journal", "journal_abbr", "doi",
    "pmc_id", "article_types", "authors", "affiliation",
    "language",
]


def _query_raw_export(db_path: Path, since_timestamp: str | None = None,
                      *, order_by_validated_at: bool = False) -> list:
    """raw 导出共用查询：列清单与复核谓词单一来源。

    order_by_validated_at 仅增量路径启用（全量路径保持无 ORDER BY 的现状）。
    """
    sql = f"""
            SELECT a.pmid, a.title, a.abstract, a.keywords, a.mesh_terms,
                   a.pub_year, a.pub_month, a.journal, a.journal_abbr, a.doi,
                   a.pmc_id, a.article_types, a.authors, a.affiliation,
                   a.language
            FROM articles a
            JOIN llm_validation v ON a.pmid = v.pmid
            WHERE {_REVIEW_PASS_PREDICATE}
        """
    params: list = []
    if since_timestamp:
        sql += "\n              AND v.validated_at > ?"
        params.append(since_timestamp)
    if order_by_validated_at:
        sql += "\n            ORDER BY v.validated_at"
    with get_conn(db_path) as conn:
        return conn.execute(sql, params).fetchall()


def _write_dict_csv(csv_path: Path, fieldnames: list, rows, transform=None) -> None:
    """以 utf-8-sig 写 CSV（DictWriter + 可选行变换）"""
    with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            d = dict(row)
            if transform is not None:
                d = transform(d)
            writer.writerow(d)


def _rename_reason_to_llm_reason(d: dict) -> dict:
    """最终过滤导出：reason 列改名为 llm_reason"""
    d["llm_reason"] = d.pop("reason", "")
    return d


def _export_raw_csv(db_path: Path = DB_PATH) -> Path | None:
    """
    导出复核通过文献的原始信息 CSV（articles 表原始字段，不含 raw_xml_file、LLM/复核列）。
    筛选条件与 _export_filtered_csv 一致：
    human_review='Y' 或（未复核且 llm_verdict='RELEVANT'）。
    """
    rows = _query_raw_export(db_path)

    if not rows:
        logger.info("没有符合条件的原始文献记录")
        return None

    out_dir = Path(OUTPUT_DIR)
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    csv_path = out_dir / f"articles_raw_{ts}.csv"

    _write_dict_csv(csv_path, RAW_EXPORT_FIELDS, rows)

    logger.info(f"原始文献信息已导出: {csv_path} ({len(rows)} 篇)")
    return csv_path


def _count_exportable(db_path: Path = DB_PATH) -> int:
    """统计当前可导出的复核通过文献总数（用于识别"时间戳过早导致全量导出"）"""
    with get_conn(db_path) as conn:
        return conn.execute(f"""
            SELECT COUNT(1)
            FROM articles a
            JOIN llm_validation v ON a.pmid = v.pmid
            WHERE {_REVIEW_PASS_PREDICATE}
        """).fetchone()[0]


def export_incremental_raw_csv(since_timestamp: str = None, db_path: Path = DB_PATH) -> Path | None:
    """
    增量导出：导出指定时间后新增的、复核通过的文献原始信息。

    Args:
        since_timestamp: ISO 格式时间字符串，如 '2026-08-09T00:00:00'。
                         仅导出 validated_at > since_timestamp 的记录。
                         该时间戳必须晚于上一次验证时间，否则会导出全部历史文献。
        db_path: 数据库路径

    Returns:
        导出的 CSV 文件路径，无数据则返回 None
    """
    rows = _query_raw_export(db_path, since_timestamp, order_by_validated_at=True)

    if not rows:
        logger.info(f"增量导出: 无新增符合条件的记录 (since={since_timestamp})")
        return None

    # 告警：若时间戳早于全部验证时间，导出结果等于全量，并非真正的增量导出
    if since_timestamp:
        total = _count_exportable(db_path)
        if len(rows) >= total:
            logger.warning(
                f"增量导出条数 {len(rows)} 等于可导出总数 {total}："
                f"时间戳 {since_timestamp} 早于全部文献的验证时间，"
                f"本次并非真正的增量导出。请改用晚于上次验证时间的时间戳。"
            )

    out_dir = Path(OUTPUT_DIR)
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    suffix = f"_since_{since_timestamp.replace(':', '-').replace('T', '_')}" if since_timestamp else ""
    csv_path = out_dir / f"articles_raw_incremental{suffix}_{ts}.csv"

    _write_dict_csv(csv_path, RAW_EXPORT_FIELDS, rows)

    logger.info(f"增量导出原始文献信息: {csv_path} ({len(rows)} 篇, since={since_timestamp})")
    return csv_path


def export_incremental_filtered_csv(since_timestamp: str = None,
                                    db_path: Path | None = None) -> Path | None:
    """增量导出：带研究方向分类的最终结果（与 export_incremental_raw_csv 同时段）

    参数与告警语义同 export_incremental_raw_csv，只是列集换成
    FILTERED_EXPORT_FIELDS（多出 llm_verdict / llm_reason / research_field /
    human_review）。
    """
    rows = _query_filtered_export(db_path, since_timestamp, order_by_validated_at=True)

    if not rows:
        logger.info(f"增量导出（含研究方向）: 无新增符合条件的记录 (since={since_timestamp})")
        return None

    if since_timestamp:
        total = _count_exportable(db_path or DB_PATH)
        if len(rows) >= total:
            logger.warning(
                f"增量导出（含研究方向）条数 {len(rows)} 等于可导出总数 {total}："
                f"时间戳 {since_timestamp} 早于全部文献的验证时间，本次并非真正的增量导出。"
            )

    out_dir = Path(OUTPUT_DIR)
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    suffix = f"_since_{since_timestamp.replace(':', '-').replace('T', '_')}" if since_timestamp else ""
    csv_path = out_dir / f"llm_filtered_incremental{suffix}_{ts}.csv"

    _write_dict_csv(csv_path, FILTERED_EXPORT_FIELDS, rows,
                    transform=_rename_reason_to_llm_reason)

    logger.info(f"增量导出（含研究方向）: {csv_path} ({len(rows)} 篇, since={since_timestamp})")
    return csv_path
