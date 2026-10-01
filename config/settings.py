# config/settings.py
"""
全局配置文件
使用前请填入你的 NCBI API Key（免费申请：https://www.ncbi.nlm.nih.gov/account/）
"""

import os
from pathlib import Path
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

# ── 项目路径 ──────────────────────────────────────────────────
BASE_DIR    = Path(__file__).parent.parent
DATA_DIR    = BASE_DIR / "data"
RAW_XML_DIR = DATA_DIR / "raw_xml"
PROC_DIR    = DATA_DIR / "processed"
OUTPUT_DIR  = DATA_DIR / "output"
LOG_DIR     = BASE_DIR / "logs"

# PDF 存储路径
PDF_DIR = DATA_DIR / "pdfs"

DB_PATH     = PROC_DIR / "potato_lit.db"

# ── 网络代理配置 ────────────────────────────────────────────
# 可选：HTTP/HTTPS 代理，如 http://127.0.0.1:7890（走代理访问 NCBI 等外网）
# 留空则直连
PROXY = os.environ.get("PROXY", "") or None

# ── NCBI API 配置 ────────────────────────────────────────────
# 填入你的 API Key，可将速率从 3 次/秒 提升到 10 次/秒
# 留空也可运行，但会更慢
NCBI_API_KEY = os.environ.get("NCBI_API_KEY", "")
NCBI_EMAIL   = os.environ.get("NCBI_EMAIL", "")   # NCBI 要求提供联系邮箱，建议设置到 .env

# PMC 全文访问（2026-08 OA Web Service 下线，改用 PMC Cloud Service on AWS）
PMC_S3_URL = "https://pmc-oa-opendata.s3.amazonaws.com"

# API 请求间隔（秒）：有 Key 用 0.11，无 Key 用 0.34
REQUEST_INTERVAL = 0.11 if NCBI_API_KEY else 0.34

# 每批 efetch 的 PMID 数量（建议 200–500）
EFETCH_BATCH_SIZE = 300

# ── 搜索策略 ─────────────────────────────────────────────────
# 主搜索词：马铃薯 ×（基因调控 / 胁迫 / 发育 / 多组学 / 性状）
# 优化目标：向「含具体马铃薯内源基因名」的文献倾斜，远离「必无具体基因名」的噪音
# 结构：核心实体 + 高概率触发基因名的分子术语 + 实验手段 + 生物学过程 - 排除噪音

# 1. 核心实体（马铃薯）
POTATO_CORE = (
    'potato[Title/Abstract] OR "Solanum tuberosum"[Title/Abstract] '
    'OR "Solanum tuberosum"[MeSH Terms] OR "Solanum sect. Petota"[MeSH Terms]'
)

# 2. 高概率触发「具体基因名」的分子术语（核心扩展）
MOLECULAR_GENE_TERMS = (
    # 转录因子家族
    'transcription factor[Title/Abstract] OR "transcription factors"[Title/Abstract] '
    'OR MYB[Title/Abstract] OR bZIP[Title/Abstract] OR NAC[Title/Abstract] '
    'OR WRKY[Title/Abstract] OR ERF[Title/Abstract] OR AP2[Title/Abstract] '
    'OR bHLH[Title/Abstract] OR "HD-ZIP"[Title/Abstract] OR GRAS[Title/Abstract] '
    'OR TCP[Title/Abstract] OR BEL[Title/Abstract] OR KNOX[Title/Abstract] '
    'OR MADS[Title/Abstract] OR ARF[Title/Abstract] OR IAA[Title/Abstract] '
    # 信号通路核心
    'OR MAPK[Title/Abstract] OR CDPK[Title/Abstract] OR SnRK[Title/Abstract] '
    'OR TOR[Title/Abstract] OR ABI[Title/Abstract] OR DREB[Title/Abstract] '
    'OR CBF[Title/Abstract] OR AREB[Title/Abstract] OR ICE[Title/Abstract] '
    'OR HSF[Title/Abstract] OR HSP[Title/Abstract] '
    # 抗病 R 基因/效应子
    'OR "R gene"[Title/Abstract] OR "R genes"[Title/Abstract] '
    'OR NLR[Title/Abstract] OR "NB-LRR"[Title/Abstract] OR "NBS-LRR"[Title/Abstract] '
    'OR effector[Title/Abstract] OR effectors[Title/Abstract] '
    'OR avirulence[Title/Abstract] OR Avr[Title/Abstract] '
    'OR Rx[Title/Abstract] OR Ry[Title/Abstract] OR Rpi[Title/Abstract] '
    'OR Hero[Title/Abstract] OR Gro[Title/Abstract] OR Gpa[Title/Abstract] '
    # 代谢酶/转运体（淀粉/糖核心）
    'OR invertase[Title/Abstract] OR "sucrose synthase"[Title/Abstract] '
    'OR "ADP-glucose pyrophosphorylase"[Title/Abstract] OR AGPase[Title/Abstract] '
    'OR "starch synthase"[Title/Abstract] OR "granule-bound starch synthase"[Title/Abstract] '
    'OR GBSS[Title/Abstract] OR SSS[Title/Abstract] '
    'OR transporter[Title/Abstract] OR "sugar transporter"[Title/Abstract] '
    'OR SWEET[Title/Abstract] OR SUT[Title/Abstract] '
    # 激素合成/信号关键基因
    'OR NCED[Title/Abstract] OR AAO[Title/Abstract] OR ACS[Title/Abstract] '
    'OR ACO[Title/Abstract] OR LOX[Title/Abstract] OR AOS[Title/Abstract] '
    'OR OPR[Title/Abstract] OR ICS[Title/Abstract] OR PAL[Title/Abstract] '
    'OR TPS[Title/Abstract] OR GA20ox[Title/Abstract] OR GA3ox[Title/Abstract] '
    'OR GA2ox[Title/Abstract] OR PIN[Title/Abstract] OR BZR[Title/Abstract] '
    'OR BES[Title/Abstract] OR JAZ[Title/Abstract] OR MYC2[Title/Abstract] '
    'OR NPR[Title/Abstract] OR TGA[Title/Abstract] '
    # 表观/染色质
    'OR histone[Title/Abstract] OR methyltransferase[Title/Abstract] '
    'OR demethylase[Title/Abstract] OR acetyltransferase[Title/Abstract] '
    'OR deacetylase[Title/Abstract] OR HDAC[Title/Abstract] OR HAT[Title/Abstract] '
    'OR "SWI/SNF"[Title/Abstract] OR Polycomb[Title/Abstract] OR PRC2[Title/Abstract] '
)

# 3. 实验手段（这些论文必有具体基因名）
EXPERIMENTAL_TERMS = (
    'CRISPR[Title/Abstract] OR "gene editing"[Title/Abstract] '
    'OR "genome editing"[Title/Abstract] OR knockout[Title/Abstract] '
    'OR "knock-out"[Title/Abstract] OR overexpression[Title/Abstract] '
    'OR "over-expression"[Title/Abstract] OR RNAi[Title/Abstract] '
    'OR "RNA interference"[Title/Abstract] OR silencing[Title/Abstract] '
    'OR VIGS[Title/Abstract] OR "virus-induced gene silencing"[Title/Abstract] '
    'OR mutant[Title/Abstract] OR mutants[Title/Abstract] '
    'OR "T-DNA"[Title/Abstract] OR EMS[Title/Abstract] '
    'OR "insertional mutagenesis"[Title/Abstract] '
    'OR GWAS[Title/Abstract] OR "genome-wide association"[Title/Abstract] '
    'OR QTL[Title/Abstract] OR "quantitative trait locus"[Title/Abstract] '
    'OR "quantitative trait loci"[Title/Abstract] '
    'OR "marker-assisted"[Title/Abstract] OR "genomic selection"[Title/Abstract] '
)

# 4. 生物学过程（保留现有，精简通配符）
BIOLOGICAL_PROCESS = (
    'gene[Title/Abstract] OR genes[Title/Abstract] '
    'OR "gene expression"[Title/Abstract] OR regulatory[Title/Abstract] '
    'OR transcription[Title/Abstract] OR transcriptome[Title/Abstract] '
    'OR promoter[Title/Abstract] OR enhancer[Title/Abstract] '
    'OR stress[Title/Abstract] OR drought[Title/Abstract] OR cold[Title/Abstract] '
    'OR heat[Title/Abstract] OR salt[Title/Abstract] OR "abiotic stress"[Title/Abstract] '
    'OR "biotic stress"[Title/Abstract] OR resistance[Title/Abstract] '
    'OR tolerance[Title/Abstract] OR defense[Title/Abstract] '
    'OR development[Title/Abstract] OR growth[Title/Abstract] '
    'OR tuber[Title/Abstract] OR tuberization[Title/Abstract] '
    'OR morphogenesis[Title/Abstract] OR senescence[Title/Abstract] '
    'OR flowering[Title/Abstract] OR dormancy[Title/Abstract] '
    'OR sprouting[Title/Abstract] OR "apical dominance"[Title/Abstract] '
    'OR stolon[Title/Abstract] '
    'OR proteomics[Title/Abstract] OR metabolomics[Title/Abstract] '
    'OR genomics[Title/Abstract] OR epigenomics[Title/Abstract] '
    'OR transcriptomics[Title/Abstract] OR "multi-omics"[Title/Abstract] '
    'OR miRNA[Title/Abstract] OR lncRNA[Title/Abstract] OR circRNA[Title/Abstract] '
    'OR siRNA[Title/Abstract] OR methylation[Title/Abstract] '
    'OR trait[Title/Abstract] OR traits[Title/Abstract] '
    'OR allele[Title/Abstract] OR alleles[Title/Abstract] '
    'OR starch[Title/Abstract] OR sucrose[Title/Abstract] OR sugar[Title/Abstract] '
    'OR "cold-sweetening"[Title/Abstract] OR "chip quality"[Title/Abstract] '
    'OR yield[Title/Abstract] '
    'OR auxin[Title/Abstract] OR cytokinin[Title/Abstract] '
    'OR gibberellin[Title/Abstract] OR "abscisic acid"[Title/Abstract] '
    'OR ABA[Title/Abstract] OR ethylene[Title/Abstract] '
    'OR "jasmonic acid"[Title/Abstract] OR "salicylic acid"[Title/Abstract] '
)

# 5. 排除噪音（一定不含具体马铃薯内源基因名）
EXCLUSIONS = (
    'NOT ('
    '"sweet potato"[Title/Abstract] OR "Ipomoea batatas"[Title/Abstract] '
    'OR processing[Title/Abstract] OR storage[Title/Abstract] '
    'OR frying[Title/Abstract] OR "french fry"[Title/Abstract] OR chip[Title/Abstract] '
    'OR crisp[Title/Abstract] OR sensory[Title/Abstract] OR texture[Title/Abstract] '
    'OR acrylamide[Title/Abstract] OR Maillard[Title/Abstract] '
    'OR gelatinization[Title/Abstract] OR pasting[Title/Abstract] '
    'OR digestibility[Title/Abstract] OR glycemic[Title/Abstract] '
    'OR "field trial"[Title/Abstract] OR "multi-environment"[Title/Abstract] '
    'OR "combining ability"[Title/Abstract] OR GCA[Title/Abstract] OR SCA[Title/Abstract] '
    'OR heritability[Title/Abstract] OR heterosis[Title/Abstract] '
    'OR "yield trial"[Title/Abstract] OR "agronomic trait"[Title/Abstract] '
    'OR "breeding value"[Title/Abstract] '
    'OR "method development"[Title/Abstract] OR "new method"[Title/Abstract] '
    'OR "novel approach"[Title/Abstract] OR protocol[Title/Abstract] '
    'OR pipeline[Title/Abstract] OR workflow[Title/Abstract] OR benchmark[Title/Abstract] '
    'OR review[Publication Type] OR "systematic review"[Title/Abstract] '
    'OR "meta-analysis"[Publication Type] OR "meta analysis"[Title/Abstract]'
    ')'
)

# 最终组合查询
PUBMED_QUERY = f"({POTATO_CORE}) AND ({MOLECULAR_GENE_TERMS} OR {EXPERIMENTAL_TERMS} OR {BIOLOGICAL_PROCESS}) {EXCLUSIONS}"

# 文献时间范围
SEARCH_YEAR_MIN = 1800
SEARCH_YEAR_MAX = datetime.now().year

# 每次搜索覆盖的年数，防止单次搜索结果超过 10,000 条分页限制
SEARCH_SLICE_YEARS = 5

# 发表年份硬过滤范围（与检索范围保持一致）
PUB_YEAR_MIN = SEARCH_YEAR_MIN
PUB_YEAR_MAX = SEARCH_YEAR_MAX

# ── 硬过滤规则 ────────────────────────────────────────────────
# 摘要最小字符数（太短说明记录不完整）
ABSTRACT_MIN_LEN = 80

# 需要排除的文章类型（PubMed PublicationType 字段）
EXCLUDED_ARTICLE_TYPES = [
    "Letter", "Comment", "Correction", "Retraction",
    "Published Erratum", "Editorial", "News"
]

# ── LLM 验证配置 ──────────────────────────────────────────────
LLM_PROVIDER    = "zhipu"          # "deepseek" | "zhipu" | "openai"

# DeepSeek（OpenAI 兼容格式）
DEEPSEEK_API_KEY  = os.environ.get("DEEPSEEK_API_KEY", "")
DEEPSEEK_BASE_URL = "https://api.deepseek.com"
DEEPSEEK_MODEL    = "deepseek-v4-flash"

# 智谱AI（原生 zhipuai SDK）
ZHIPU_API_KEY   = os.environ.get("ZHIPU_API_KEY", "")
ZHIPU_MODEL     = "glm-4-Flash-250414"
ZHIPU_BATCH_MODEL = "glm-4-flash"      # Batch API 使用的模型（价格 50% off）

# 其他 OpenAI 兼容 API（如 OpenAI、SiliconFlow、vLLM 等）
OPENAI_API_KEY  = os.environ.get("OPENAI_API_KEY", "")
OPENAI_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
OPENAI_MODEL    = "qwen3.7-plus"

LLM_BATCH_SIZE  = 5                     # 每次调用验证的文献数量
LLM_CONCURRENCY = 2                     # 并行发送的批次数（同时进行的 API 调用数）
LLM_MAX_TOKENS  = 8192                  # 每次 API 调用的最大 token 数
LLM_MAX_RETRIES = 3                     # 单次 API 调用重试次数（指数退避 2s/4s/8s）
LLM_MAX_ROUNDS  = 2                     # 轮次重试次数（初始 1 轮 + 额外重试轮数）

# ── LLM Batch API 配置（仅 zhipu） ──
LLM_BATCH_POLL_INTERVAL  = 30           # Batch 轮询间隔（秒）
LLM_BATCH_TIMEOUT        = 86400        # Batch 超时时间（24h）
LLM_BATCH_AUTO_DELETE    = True         # 完成后自动删除输入文件

# Provider 配置字典 — 新增 provider 只需在此添加一项
LLM_PROVIDER_CONFIGS = {
    "zhipu": {
        "api_key_env": "ZHIPU_API_KEY",
        "api_key_fallback": ZHIPU_API_KEY,
        "client_type": "zhipuai",
        "model": ZHIPU_MODEL,
        "base_url": None,
        "extra_kwargs": {"temperature": 0, "max_tokens": LLM_MAX_TOKENS},
        "fix_multi_array": True,
    },
    "deepseek": {
        "api_key_env": "DEEPSEEK_API_KEY",
        "api_key_fallback": DEEPSEEK_API_KEY,
        "client_type": "openai",
        "model": DEEPSEEK_MODEL,
        "base_url": DEEPSEEK_BASE_URL,
        "extra_kwargs": {
            "temperature": 0, "max_tokens": LLM_MAX_TOKENS,
            "timeout": 120, "response_format": {"type": "json_object"},
        },
        # V4 模型思考模式默认开启：temperature 不生效、思考 token 占用输出预算
        # 可能导致 JSON 截断，显式关闭
        "extra_body": {"thinking": {"type": "disabled"}},
        "fix_multi_array": False,
    },
    "openai": {
        "api_key_env": "OPENAI_API_KEY",
        "api_key_fallback": OPENAI_API_KEY,
        "client_type": "openai",
        "model": OPENAI_MODEL,
        "base_url": OPENAI_BASE_URL,
        "extra_kwargs": {"temperature": 0, "max_tokens": LLM_MAX_TOKENS, "timeout": 120},
        "fix_multi_array": False,
    },
}
