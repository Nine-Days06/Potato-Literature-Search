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
PDF_DIR      = DATA_DIR / "pdfs"
PDF_HIGH_DIR = PDF_DIR / "high"
PDF_MID_DIR  = PDF_DIR / "mid"
PDF_LOW_DIR  = PDF_DIR / "low"

DB_PATH     = PROC_DIR / "potato_lit.db"

# ── NCBI API 配置 ────────────────────────────────────────────
# 填入你的 API Key，可将速率从 3 次/秒 提升到 10 次/秒
# 留空也可运行，但会更慢
NCBI_API_KEY = os.environ.get("NCBI_API_KEY", "")
NCBI_EMAIL   = os.environ.get("NCBI_EMAIL", "")   # NCBI 要求提供联系邮箱，建议设置到 .env

# PMC Open Access Web Service API
PMC_OA_API = "https://www.ncbi.nlm.nih.gov/pmc/utils/oa/oa.fcgi"

# API 请求间隔（秒）：有 Key 用 0.11，无 Key 用 0.34
REQUEST_INTERVAL = 0.11 if NCBI_API_KEY else 0.34

# 每批 efetch 的 PMID 数量（建议 200–500）
EFETCH_BATCH_SIZE = 300

# ── 搜索策略 ─────────────────────────────────────────────────
# 主搜索词：马铃薯 ×（基因调控 / 胁迫 / 发育 / 多组学 / 性状）
PUBMED_QUERY = (
    '(potato[Title/Abstract] OR "Solanum tuberosum"[Title/Abstract] '
    'OR "S. tuberosum"[Title/Abstract] OR "solanum tuberosum"[MeSH Terms]) '
    'AND ('
    # 基因调控
    'gene[Title/Abstract] OR genes[Title/Abstract] '
    'OR "gene expression"[Title/Abstract] OR regulatory[Title/Abstract] '
    'OR transcription[Title/Abstract] OR transcriptome[Title/Abstract] '
    # 胁迫
    'OR stress[Title/Abstract] OR drought[Title/Abstract] OR cold[Title/Abstract] '
    'OR heat[Title/Abstract] OR salt[Title/Abstract] OR "abiotic stress"[Title/Abstract] '
    'OR "biotic stress"[Title/Abstract] OR resistance[Title/Abstract] '
    'OR tolerance[Title/Abstract] OR defense[Title/Abstract] '
    # 发育
    'OR development[Title/Abstract] OR growth[Title/Abstract] '
    'OR tuber[Title/Abstract] OR morphogenesis[Title/Abstract] '
    'OR senescence[Title/Abstract] OR flowering[Title/Abstract] '
    # 多组学
    'OR proteom*[Title/Abstract] OR metabolom*[Title/Abstract] '
    'OR genom*[Title/Abstract] OR epigenom*[Title/Abstract] '
    'OR "multi-omics"[Title/Abstract] OR miRNA[Title/Abstract] '
    # 性状
    'OR trait[Title/Abstract] OR traits[Title/Abstract] '
    'OR QTL[Title/Abstract] OR allele[Title/Abstract] OR alleles[Title/Abstract] '
    'OR phenotype[Title/Abstract] OR genotype[Title/Abstract])'
)

# 文献时间范围
SEARCH_YEAR_MIN = 2000
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

# ── 软评分词典 ────────────────────────────────────────────────
# 基因相关词汇
GENE_TERMS = [
    "gene", "genes", "allele", "alleles", "locus", "loci",
    "QTL", "SNP", "SNPs", "haplotype", "haplotypes",
    "transcript", "transcription", "mRNA", "promoter",
    "regulatory", "overexpression", "knockout", "knockdown",
    "CRISPR", "RNAi", "siRNA", "mutation", "mutations",
    "polymorphism", "genotype", "genome", "genomic",
    "marker", "markers", "SSR", "microsatellite",
    "protein", "enzyme", "kinase", "transcription factor",
    "cloning", "sequencing", "expression",
]

# 功能/机制相关词汇
FUNCTION_TERMS = [
    "function", "functional", "pathway", "pathways",
    "expression", "regulation", "regulated", "interaction",
    "biosynthesis", "metabolism", "metabolic", "signaling",
    "signal transduction", "enzyme activity", "catalysis",
    "phosphorylation", "methylation", "acetylation",
    "binding", "receptor", "activation", "inhibition",
    "response", "mechanism", "characterization",
]

# 性状/表型相关词汇
TRAIT_TERMS = [
    "yield", "tuber", "tubers", "starch", "starch content",
    "disease resistance", "blight", "late blight", "early blight",
    "drought", "drought tolerance", "cold tolerance", "heat stress",
    "flesh color", "skin color", "glycoalkaloid", "solanine",
    "anthocyanin", "phenotype", "phenotypic", "agronomic",
    "maturity", "dormancy", "sprouting", "storage",
    "quality", "nutritional", "protein content", "iron",
    "vitamin", "antioxidant", "flavor", "texture",
    "pathogen", "virus", "bacterial", "fungal",
    "Phytophthora", "Alternaria", "nematode", "aphid",
    "biomass", "growth", "development",
]

# 相关性分级阈值
# 三类都命中且总分 >= HIGH_SCORE_THRESHOLD → 高相关
HIGH_SCORE_THRESHOLD = 5
# 三类都命中 或 总分 >= MID_SCORE_THRESHOLD → 中相关
MID_SCORE_THRESHOLD  = 3

# ── LLM 验证配置 ──────────────────────────────────────────────
LLM_PROVIDER    = "deepseek"          # "deepseek" | "zhipu" | "openai"

# DeepSeek（OpenAI 兼容格式）
DEEPSEEK_API_KEY  = os.environ.get("DEEPSEEK_API_KEY", "")
DEEPSEEK_BASE_URL = "https://api.deepseek.com"
DEEPSEEK_MODEL    = "deepseek-v4-flash"

# 智谱AI（原生 zhipuai SDK）
ZHIPU_API_KEY   = os.environ.get("ZHIPU_API_KEY", "")
ZHIPU_MODEL     = "glm-4.7-flash"

# 其他 OpenAI 兼容 API（如 OpenAI、SiliconFlow、vLLM 等）
OPENAI_API_KEY  = os.environ.get("OPENAI_API_KEY", "")
OPENAI_BASE_URL = "https://api.openai.com/v1"
OPENAI_MODEL    = "gpt-4o-mini"

LLM_BATCH_SIZE  = 10                    # 每次调用验证的文献数量
LLM_MAX_TOKENS  = 8192                   # 每次 API 调用的最大 token 数
LLM_MAX_RETRIES = 3                     # 单次 API 调用重试次数（指数退避 2s/4s/8s）
LLM_MAX_ROUNDS  = 2                     # 轮次重试次数（初始 1 轮 + 额外重试轮数）
