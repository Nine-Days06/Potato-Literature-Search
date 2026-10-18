# config/settings.py
"""
全局配置文件
使用前请填入你的 NCBI API Key（免费申请：https://www.ncbi.nlm.nih.gov/account/）
"""

import os
from pathlib import Path
from datetime import datetime
from dotenv import load_dotenv

# .env 约定放在 config/ 下（见 AGENTS.md），**必须显式给路径**：
# load_dotenv() 无参会沿调用栈回溯查找，在 `python -m pytest`、`python scripts/xxx.py`
# 等入口下找不到 config/.env，导致 API Key 静默为空。此处按文件位置定位，与 cwd 无关。
_BASE_DIR = Path(__file__).parent.parent
load_dotenv(Path(__file__).parent / ".env")

# ── 项目路径 ──────────────────────────────────────────────────
BASE_DIR    = _BASE_DIR
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
# 全量马铃薯文献检索：不限定研究方向，
# 分子生物学、遗传育种、栽培生理、植保、加工储藏、品质营养、产业经济等均收录
# 结构：马铃薯核心实体 - 排除甘薯（甘薯/红薯 Ipomoea batatas 是旋花科，与马铃薯无关）

# 1. 核心实体（马铃薯，含普通马铃薯及其近缘种/野生种）
POTATO_CORE = (
    'potato[Title/Abstract] OR "Solanum tuberosum"[Title/Abstract] '
    'OR "Solanum tuberosum"[MeSH Terms] OR "Solanum sect. Petota"[MeSH Terms]'
)

# 2. 排除项：仅排除研究对象无关的干扰项（甘薯不是马铃薯）
EXCLUSIONS = (
    'NOT ("sweet potato"[Title/Abstract] OR "Ipomoea batatas"[Title/Abstract])'
)

# 最终组合查询：马铃薯核心实体，排除甘薯
PUBMED_QUERY = f"({POTATO_CORE}) {EXCLUSIONS}"

# 文献时间范围
SEARCH_YEAR_MIN = 1800
SEARCH_YEAR_MAX = datetime.now().year

# 每次搜索覆盖的年数，防止单次搜索结果超过 10,000 条分页限制
SEARCH_SLICE_YEARS = 5

# ── 硬过滤规则 ────────────────────────────────────────────────
# 2026-10 起硬过滤大幅放宽，仅保留两条机械性规则（见 cleaner/hard_filter.py）：
#   1. 标题为空 / 摘要为空（记录残缺）
#   2. 重复标题（同期刊同年份同一标题）
#
# 已取消的规则（不要加回；这些维度改由 LLM 二次验证 + 研究方向分类承担）：
#   - ABSTRACT_MIN_LEN（摘要最小长度 80 字符）
#   - PUB_YEAR_MIN / PUB_YEAR_MAX（发表年份越界；检索范围仍由 SEARCH_YEAR_* 控制）
#   - EXCLUDED_ARTICLE_TYPES（Letter/Comment/Erratum 等文章类型黑名单）
#   - 非英文语言过滤、标题最小长度过滤

# ── 研究方向分类（LLM 二次验证的输出维度）─────────────────────
# 全量马铃薯文献，不限研究方向；以下枚举为 LLM 必须选择的封闭标签集，
# 用于统计与后续按方向切分数据。新增/改名会改变已有 research_field 的可比性。
RESEARCH_FIELDS = [
    "分子生物学",   # 基因功能、基因组、转录组、蛋白、代谢、细胞
    "遗传育种",     # 种质资源、杂交、QTL、标记辅助选择、品种选育
    "栽培生理",     # 栽培措施、水肥、光合、胁迫生理、生长调控
    "植物保护",     # 病害虫害、病原物、抗性、防治、互作
    "加工储藏",     # 加工工艺、薯片薯条淀粉、贮藏保鲜、抑芽
    "品质营养",     # 品质性状、成分、营养、感官、食品安全
    "产后处理",     # 采后生理、损伤、分选、物流
    "产业经济",     # 产业、市场、贸易、政策、消费
    "综述评论",     # 综述、评论、方法学探讨
    "其他",         # 难以归入上述任一类（兜底，非排除）
]

DEFAULT_RESEARCH_FIELD = "其他"

# ── LLM 验证配置 ──────────────────────────────────────────────
# 全部可由 config/.env 覆盖，改配置无需动本文件。
# 未在 .env 中设置时使用下列默认值。

def _env_str(name: str, default: str) -> str:
    """读字符串环境变量；空串视为未设置（.env 里写 KEY= 等同于不写）"""
    v = os.environ.get(name)
    return v.strip() if v and v.strip() else default


def _env_int(name: str, default: int) -> int:
    """读整数环境变量；非法值回退默认并给出可读告警"""
    v = os.environ.get(name)
    if not v or not v.strip():
        return default
    try:
        return int(v.strip())
    except ValueError:
        print(f"[settings] 环境变量 {name}={v!r} 不是整数，回退默认值 {default}")
        return default


def _env_bool(name: str, default: bool) -> bool:
    """读布尔环境变量；接受 1/true/yes/on（忽略大小写）"""
    v = os.environ.get(name)
    if not v or not v.strip():
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


LLM_PROVIDER = _env_str("LLM_PROVIDER", "openai")   # "deepseek" | "zhipu" | "openai"

# DeepSeek（OpenAI 兼容格式）
DEEPSEEK_API_KEY  = os.environ.get("DEEPSEEK_API_KEY", "")
DEEPSEEK_BASE_URL = _env_str("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
DEEPSEEK_MODEL    = _env_str("DEEPSEEK_MODEL", "deepseek-v4-flash")

# 智谱AI（原生 zhipuai SDK）
ZHIPU_API_KEY   = os.environ.get("ZHIPU_API_KEY", "")
ZHIPU_MODEL     = _env_str("ZHIPU_MODEL", "glm-4-Flash-250414")
ZHIPU_BATCH_MODEL = _env_str("ZHIPU_BATCH_MODEL", "glm-4-flash")  # Batch API 模型（价格 50% off）

# 其他 OpenAI 兼容 API（如 OpenAI、SiliconFlow、vLLM 等）
OPENAI_API_KEY  = os.environ.get("OPENAI_API_KEY", "")
OPENAI_BASE_URL = _env_str("OPENAI_BASE_URL", "http://127.0.0.1:3425/v1")
OPENAI_MODEL    = _env_str("OPENAI_MODEL", "group/ds4.1")

LLM_BATCH_SIZE  = _env_int("LLM_BATCH_SIZE", 5)        # 每次调用验证的文献数量
LLM_CONCURRENCY = _env_int("LLM_CONCURRENCY", 2)       # 并行发送的批次数（同时进行的 API 调用数）
LLM_MAX_TOKENS  = _env_int("LLM_MAX_TOKENS", 8192)     # 每次 API 调用的最大 token 数
LLM_MAX_RETRIES = _env_int("LLM_MAX_RETRIES", 3)       # 单次 API 调用重试次数（指数退避 2s/4s/8s）
LLM_MAX_ROUNDS  = _env_int("LLM_MAX_ROUNDS", 2)        # 轮次重试次数（初始 1 轮 + 额外重试轮数）
LLM_TIMEOUT     = _env_int("LLM_TIMEOUT", 120)         # 单次 API 调用超时（秒）

# ── LLM Batch API 配置（仅 zhipu） ──
LLM_BATCH_POLL_INTERVAL  = _env_int("LLM_BATCH_POLL_INTERVAL", 30)     # Batch 轮询间隔（秒）
LLM_BATCH_TIMEOUT        = _env_int("LLM_BATCH_TIMEOUT", 86400)        # Batch 超时时间（24h）
LLM_BATCH_AUTO_DELETE    = _env_bool("LLM_BATCH_AUTO_DELETE", True)    # 完成后自动删除输入文件

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
            "timeout": LLM_TIMEOUT, "response_format": {"type": "json_object"},
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
        "extra_kwargs": {"temperature": 0, "max_tokens": LLM_MAX_TOKENS, "timeout": LLM_TIMEOUT},
        "fix_multi_array": False,
    },
}

# 启动时校验 provider 拼写，避免跑到一半才发现
if LLM_PROVIDER not in LLM_PROVIDER_CONFIGS:
    raise ValueError(
        f"LLM_PROVIDER={LLM_PROVIDER!r} 无效，可选：{', '.join(LLM_PROVIDER_CONFIGS)}"
    )
