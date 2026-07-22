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
    "你是一个马铃薯（Solanum tuberosum）知识图谱构建领域的专家。\n"
    "本项目的目标是从文献摘要中提取马铃薯相关的实体关系三元组，用于构建知识图谱。\n"
    "判断以下每篇文献的摘要是否包含可用于知识图谱构建的实体关系信息。\n\n"
     "可关注的实体类型：\n"
    "1. 基因/转录本/蛋白质（如 Gro1-4、StTCP23、invGE/GF 等具体基因名）\n"
    "2. 性状/表型（产量、块茎品质、抗病性、耐逆性、淀粉含量、休眠、植株形态等）\n"
    "3. 病虫害/病原（晚疫病 Phytophthora、青枯病、根结线虫、PVY 病毒、甲虫等）\n"
    "4. 胁迫条件（干旱、冷害、热害、盐害、氧化胁迫等）\n"
    "5. 代谢物/化合物（淀粉、花青素、龙葵素、还原糖、糖苷生物碱等）\n"
    "6. 品种/种质资源\n"
    "7. 微生物（根际细菌、真菌等与马铃薯互作的微生物）\n\n"
     "可关注的实体关系类型（不限于此）：\n"
    "- 基因 → 调控/影响/关联 → 性状\n"
    "- 病虫害/胁迫 → 影响 → 性状/产量\n"
    "- 基因 → 赋予 → 抗性（对某病虫害/胁迫）\n"
    "- 化合物 → 影响 → 表型/毒性\n"
    "- 品种/基因型 → 表现 → 性状/产量/抗性\n"
    "- 处理/条件 → 改变 → 基因表达/代谢物水平\n"
    "- 微生物 → 互作 → 马铃薯生长/健康\n"
    "- 生长发育阶段 → 影响 → 微生物群落/代谢\n\n"
     "判断步骤（请按以下顺序逐一分析后给出最终判定）：\n"
     "1. 核心研究对象：这篇文献主要研究什么？\n"
     "   - 马铃薯本身的生物学问题（基因功能、性状、抗病性、发育、"
     "代谢、胁迫响应、组学等）\n"
     "   - 马铃薯与其他生物的相互作用（抗病/感病机制、病原菌-"
     "马铃薯互作、共生等）\n"
     "   - 马铃薯品种选育/种质资源/农艺性状评价\n"
     "   - 上述以外的对象（如病原菌自身生命活动不涉及互作、"
     "其他物种、通用机制、加工储藏等）\n"
     "2. 关键实体：摘要中出现了哪些可关注的实体？（列出具体名称）\n"
     "3. 核心关系：实体之间的核心关系是什么？是否涉及马铃薯与"
     "实体的相互作用？\n"
     "4. 参照下方判断标准，得出最终 verdict\n\n"
     "判断标准：\n"
    "- 摘要中涉及至少一对实体及其关系（分子层面或农艺层面均可），"
     "且以马铃薯（Solanum tuberosum）为核心研究对象 → RELEVANT\n"
    "- 摘要仅泛泛讨论马铃薯，未涉及具体的实体-关系对 → NOT_RELEVANT\n"
    "- 摘要研究其他物种，不涉及马铃薯 → NOT_RELEVANT\n"
    "- 文献核心贡献是方法学开发，仅以马铃薯或马铃薯病原菌作为"
     "方法验证的模型系统，而非研究马铃薯本身的生物学问题 → NOT_RELEVANT\n"
    "- 摘要中的实体关系仅涉及病虫害/病原菌/病毒自身的生命活动"
     "（如病原菌自身的基因功能、发育、代谢、蛋白结构），"
     "不涉及这些实体与马铃薯的直接相互作用"
     "（如抗性、感病、对产量/品质的影响） → NOT_RELEVANT\n"
    "- 注意：甘薯/红薯（sweet potato, Ipomoea batatas）不是马铃薯，"
     "以甘薯为主要研究对象的文献 → NOT_RELEVANT\n\n"
     "示例：\n\n"
     "示例1 — RELEVANT（基因→抗性）\n"
    "PMID: 15078331\n"
    "Title: Molecular cloning of the potato Gro1-4 gene conferring "
    "resistance to pathotype Ro1 of the root cyst nematode Globodera "
    "rostochiensis, based on a candidate gene approach.\n"
    "Abstract: The endoparasitic root cyst nematode Globodera "
    "rostochiensis causes considerable damage in potato cultivation. "
    "In the past, major genes for nematode resistance have been "
    "introgressed from related potato species into cultivars. "
    "Elucidating the molecular basis of resistance will contribute to "
    "the understanding of nematode-plant interactions and assist in "
    "breeding nematode-resistant cultivars. The Gro1 resistance locus "
    "to G. rostochiensis on potato chromosome VII co-localized with a "
    "resistance-gene-like (RGL) DNA marker. This marker was used to "
    "isolate from genomic libraries 15 members of a closely related "
    "candidate gene family. Analysis of inheritance, linkage mapping, "
    "and sequencing reduced the number of candidate genes to three. "
    "Complementation analysis by stable potato transformation showed "
    "that the gene Gro1-4 conferred resistance to G. rostochiensis "
    "pathotype Ro1. Gro1-4 encodes a protein of 1136 amino acids that "
    "contains Toll-interleukin 1 receptor (TIR), nucleotide-binding "
    "(NB), leucine-rich repeat (LRR) homology domains and a C-terminal "
    "domain with unknown function. The deduced Gro1-4 protein differed "
    "by 29 amino acid changes from susceptible members of the Gro1 gene "
    "family. Sequence characterization of 13 members of the Gro1 gene "
    "family revealed putative regulatory elements and a variable "
    "microsatellite in the promoter region, insertion of a "
    "retrotransposon-like element in the first intron, and a stop codon "
    "in the NB coding region of some genes. Sequence analysis of RT-PCR "
    "products showed that Gro1-4 is expressed, among other members of "
    "the family including putative pseudogenes, in non-infected roots "
    "of nematode-resistant plants. RT-PCR also demonstrated that "
    "members of the Gro1 gene family are expressed in most potato "
    "tissues.\n\n"
     "示例2 — RELEVANT（基因→品质性状）\n"
    "PMID: 15802505\n"
    "Title: DNA variation at the invertase locus invGE/GF is "
    "associated with tuber quality traits in populations of potato "
    "breeding clones.\n"
    "Abstract: Starch and sugar content of potato tubers are "
    "quantitative traits, which are models for the candidate gene "
    "approach for identifying the molecular basis of quantitative "
    "trait loci (QTL) in noninbred plants. Starch and sugar content "
    "are also important for the quality of processed products such as "
    "potato chips and French fries. A high content of the reducing "
    "sugars glucose and fructose results in inferior chip quality. "
    "Tuber starch content affects nutritional quality. Functional and "
    "genetic models suggest that genes encoding invertases control, "
    "among other things, tuber sugar content. The invGE/GF locus on "
    "potato chromosome IX consists of duplicated invertase genes invGE "
    "and invGF and colocalizes with cold-sweetening QTL Sug9. DNA "
    "variation at invGE/GF was analyzed in 188 tetraploid potato "
    "cultivars, which have been assessed for chip quality and tuber "
    "starch content. Two closely correlated invertase alleles, "
    "invGE-f and invGF-d, were associated with better chip quality in "
    "three breeding populations. Allele invGF-b was associated with "
    "lower tuber starch content. The potato invertase gene invGE is "
    "orthologous to the tomato invertase gene Lin5, which is causal "
    "for the fruit-sugar-yield QTL Brix9-2-5, suggesting that natural "
    "variation of sugar yield in tomato fruits and sugar content of "
    "potato tubers is controlled by functional variants of orthologous "
    "invertase genes.\n\n"
     "示例3 — RELEVANT（品种→产量/抗性，农艺层面）\n"
    "PMID: 28742868\n"
    "Title: Combining ability of highland tropic adapted potato for "
    "tuber yield and yield components under drought.\n"
    "Abstract: Recurrent drought and late blight disease are the "
    "major factors limiting potato productivity in the northwest "
    "Ethiopian highlands. Incorporating drought tolerance and late "
    "blight resistance in the same genotypes will enable the "
    "development of cultivars with high and stable yield potential "
    "under erratic rainfall conditions. The objectives of this study "
    "were to assess combining ability effects and gene action for "
    "tuber yield and traits related to drought tolerance in the "
    "International Potato Centre's (CIP's) advanced clones from the "
    "late blight resistant breeding population B group 'B3C2' and to "
    "identify promising parents and families for cultivar development. "
    "Sixteen advanced clones from the late blight resistant breeding "
    "population were crossed in two sets using the North Carolina "
    "Design II. The resulting 32 families were evaluated together with "
    "five checks and 12 parental clones in a 7 x 7 lattice design with "
    "two water regimes and two replications. The experiment was "
    "carried out at Adet, in northwest Ethiopia under well-watered and "
    "water stressed conditions with terminal drought imposed from the "
    "tuber bulking stage. The results showed highly significant "
    "differences between families, checks, and parents for growth, "
    "physiological, and tuber yield related traits. Traits including "
    "marketable tuber yield, marketable tuber number, average tuber "
    "weight and groundcover were positively correlated with total "
    "tuber yield under both drought stressed and well-watered "
    "conditions. Plant height was correlated with yield only under "
    "drought stressed condition. GCA was more important than SCA for "
    "total tuber yield, marketable tuber yield, average tuber weight, "
    "plant height, groundcover, and chlorophyll content under stress. "
    "This study identified the parents with best GCA and the "
    "combinations with best SCA effects, for both tuber yield and "
    "drought tolerance related traits. The new population is shown to "
    "be a valuable genetic resource for variety selection and "
    "improvement of potato's adaptation to the drought prone areas in "
    "northwest Ethiopia and similar environments.\n\n"
     "示例4 — NOT_RELEVANT（方法学论文，仅以马铃薯病原菌为模型）\n"
    "PMID: 10658663\n"
    "Title: cDNA-AFLP analysis of differential gene expression in "
    "the prokaryotic plant pathogen Erwinia carotovora.\n"
    "Abstract: For studies of differential gene expression in "
    "prokaryotes, methods for synthesizing representative cDNA "
    "populations are required. Here, a technique is described for "
    "the synthesis of cDNA from the potato pathogens Erwinia "
    "carotovora subsp. atroseptica (Eca) and Erwinia carotovora "
    "subsp. carotovora (Ecc) using a combination of short "
    "oligonucleotide (11-mer) primers that were known to anneal to "
    "conserved sequences in the 3' regions of enterobacterial genes. "
    "Specific PCR amplifications with primers designed to anneal to "
    "14 known genes from either Eca or Ecc revealed the presence of "
    "the corresponding transcripts in cDNA, suggesting that the cDNA "
    "represented a broad genomic coverage. cDNA-amplified fragment "
    "length polymorphism (cDNA-AFLP) was used to identify "
    "differentially expressed genes in Eca, including one that shows "
    "significant similarity, at the protein level, to an avirulence "
    "gene from Xanthomonas campestris pv. raphani. Northern analysis "
    "was used to confirm that differentially amplified cDNA fragments "
    "were derived from differentially expressed genes. This is the "
    "first report of the use of cDNA-AFLP to study differential gene "
    "expression in prokaryotes.\n\n"
     "请以 JSON 对象格式逐条回答，不要包含其他内容：\n"
    '{"results":[{"pmid":"...","verdict":"RELEVANT 或 NOT_RELEVANT",'
    '"reason":"请用中文简要说明判断依据，指出摘要中出现的实体和关系"}]}'
)

INSERT_SQL = """
INSERT OR REPLACE INTO llm_validation
    (pmid, llm_verdict, reason, validated_at)
VALUES (?, ?, ?, ?)
"""

UPDATE_HUMAN_REVIEW_SQL = """
UPDATE llm_validation SET human_review = ? WHERE pmid = ?
"""


def _build_batch_prompt(rows: list) -> str:
    """为一批文献构建 prompt 正文"""
    parts = [
        "以下是需要你根据上述标准判断的文献列表，请逐条判断摘要中是否存在可提取的实体关系：\n\n"
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
                SELECT a.pmid, a.title, a.abstract
                FROM articles a
                WHERE a.abstract IS NOT NULL AND a.abstract != ''
                  AND a.pmid NOT IN (SELECT pmid FROM llm_validation)
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
            batch = remaining_rows[start:start + LLM_BATCH_SIZE]
            batches.append(batch)
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
                    log_rows = []
                    for row in batch:
                        pmid = row["pmid"]
                        if pmid in failed_set:
                            round_failed.append(row)
                            continue
                        r = pmid_to_result.get(pmid)
                        if r is None:
                            round_failed.append(row)
                            continue
                        log_rows.append((pmid, row["label"],
                                         r.get("verdict", "UNKNOWN"),
                                         r.get("reason", ""), now))

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
            writer.writerow([row["pmid"], row["title"], row["abstract"] or ""])
    logger.warning(f"仍有 {len(failed_rows)} 篇验证失败: {csv_path}")
    return csv_path


def _export_review_csv() -> Path | None:
    """导出所有待人工复核的记录为 CSV"""
    with get_conn(DB_PATH) as conn:
        rows = conn.execute("""
            SELECT v.pmid, a.title, a.abstract,
                   v.llm_verdict, v.reason
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
            "pmid", "title", "abstract",
            "relevance_label", "llm_verdict", "reason", "human_review",
        ])
        for row in rows:
            writer.writerow([
                row["pmid"],
                row["title"],
                row["abstract"] or "",
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
            updates.append((review, pmid))
            if review == "Y":
                passed += 1
            else:
                rejected += 1

    if updates:
        with get_conn(DB_PATH) as conn:
            conn.executemany(UPDATE_HUMAN_REVIEW_SQL, updates)

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
                   v.llm_verdict, v.reason, v.human_review
            FROM articles a
            JOIN llm_validation v ON a.pmid = v.pmid
            WHERE v.human_review = 'Y'
               OR (v.human_review IS NULL AND v.llm_verdict = 'RELEVANT')
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
