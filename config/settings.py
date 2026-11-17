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

# ── 配置助手 ──────────────────────────────────────────────────
# 必须定义在所有读取环境变量的段落之前：TDM 段（第 51 行起）就要用 _env_float，
# 助手若留在文件末尾的 LLM 段里会在导入期抛 NameError。
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


def _env_float(name: str, default: float) -> float:
    """读浮点环境变量；非法值回退默认并给出可读告警"""
    v = os.environ.get(name)
    if not v or not v.strip():
        return default
    try:
        return float(v.strip())
    except ValueError:
        print(f"[settings] 环境变量 {name}={v!r} 不是数字，回退默认值 {default}")
        return default


# ── 项目路径 ──────────────────────────────────────────────────
BASE_DIR    = _BASE_DIR
DATA_DIR    = BASE_DIR / "data"
RAW_XML_DIR = DATA_DIR / "raw_xml"
PROC_DIR    = DATA_DIR / "processed"
OUTPUT_DIR  = DATA_DIR / "output"
LOG_DIR     = BASE_DIR / "logs"

# ── data/output 子目录（相对 OUTPUT_DIR）───────────────────────
# 按用途分开，导出再按全量/增量分开：根目录只留子目录，避免几十个
# CSV 混在一起看不出哪个是最新交付件。
# 各模块不要直接拼字符串，统一走各自的 `_output_dir(SUBDIR_*)` 助手，
# 它按**调用时**的模块级 OUTPUT_DIR 重新派生，测试 patch OUTPUT_DIR 即可重定向。
SUBDIR_EXPORT_FULL        = "exports/full"         # 全量交付 CSV
SUBDIR_EXPORT_INCREMENTAL = "exports/incremental"  # 增量交付 CSV
SUBDIR_REVIEW             = "review"               # 待人工复核清单
SUBDIR_FAILURE            = "failures"             # 失败清单（LLM / PDF）
SUBDIR_CACHE              = "cache"                # 检查点与批次中间件
SUBDIR_TDM                = "tdm"                  # TDM 申请清单与下载计划
OUTPUT_SUBDIRS = [
    SUBDIR_EXPORT_FULL,
    SUBDIR_EXPORT_INCREMENTAL,
    SUBDIR_REVIEW,
    SUBDIR_FAILURE,
    SUBDIR_CACHE,
    SUBDIR_TDM,
]

# 全文存储路径：只保存复核通过的相关文献（按 PMID 命名）
PDF_DIR = DATA_DIR / "pdfs_relevant"
# 站点补录（potato-ai.top）里 PubMed 不收的文献全文，按 DOI 命名，与 PDF_DIR 分开
EXTERNAL_PDF_DIR = DATA_DIR / "pdfs_nonpubmed"

DB_PATH     = PROC_DIR / "potato_lit.db"

# ── TDM（Text and Data Mining）全文获取 ──────────────────────
# 出版社 TDM API 用于拿订阅内容的全文（Elsevier 给 XML，Wiley/Springer 给 PDF）。
# 三条渠道的授权模型不同，必须分清：
#   Wiley   —— TDM Client Token 授权，但官方客户端 README 明确按 IP 判定：
#              必须在机构网段内执行，校外需先连学校 VPN。申请：
#              https://static.wiley.com/tdm/ （JS 渲染页，需浏览器打开）
#              端点 https://api.wiley.com/onlinelibrary/tdm/v1/articles/{DOI}
#              header `Wiley-TDM-Client-Token: <UUID>`
#   Springer —— 无 key，Crossref 查 text-mining 链接后按校园 IP 订阅权限下载 PDF。
#   Elsevier —— API key 只证明注册过，**不含内容权限**。内容权限靠 IP 归属
#              或 insttoken（X-ELS-Insttoken，需 integration support 签发）。
#              家庭宽带直连时即使有 key 也只能拿到摘要。端点
#              https://api.elsevier.com/content/article/doi/{DOI}?httpAccept=text/xml
# 两个 token 都为空时，相关命令直接报错退出，不会静默跳过。
WILEY_TDM_TOKEN    = os.environ.get("WILEY_TDM_TOKEN", "").strip() or None
ELSEVIER_API_KEY   = os.environ.get("ELSEVIER_API_KEY", "").strip() or None
ELSEVIER_INSTTOKEN = os.environ.get("ELSEVIER_INSTTOKEN", "").strip() or None

# 请求节奏。Wiley 官方文档写「3 篇/秒」，但条款同时限制
# 「60 次/10 分钟」，后者才是真瓶颈 —— 按 60/10min 折算即 10 秒/篇。
# 官方 Python 客户端 tdm-client 默认 sleep 5 秒，这里取更保守的 10 秒。
WILEY_TDM_INTERVAL   = _env_float("WILEY_TDM_INTERVAL", 10.0)
ELSEVIER_TDM_INTERVAL = _env_float("ELSEVIER_TDM_INTERVAL", 2.0)

# 单篇最大体积（字节）。TDM 返回的是整篇全文，正常 1-10 MB，
# 超过 50 MB 视为异常响应（多为错误页被当成文件）直接丢弃。
TDM_MAX_BYTES = _env_int("TDM_MAX_BYTES", 50 * 1024 * 1024)
# 单次请求超时（秒）
TDM_TIMEOUT = _env_int("TDM_TIMEOUT", 120)

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
# 结构：马铃薯核心实体（含栽培种 + 野生近缘种） - 排除纯甘薯文献
#
# 2026-10 检索式收紧（实测数据见 README「检索式调优」）：
#   1. 补 18 个野生近缘种（Solanum sect. Petota）。它们在 MeSH 里没有独立主题词，
#      只能靠 TIAB 补，旧式漏掉 71 篇、其中 77.5% 判为相关。
#   2. 甘薯排除从「命中即排除」改为「命中且无马铃薯学名证据才排除」。
#      旧式误伤 142 篇真正同时研究两者的文献（其中 30.3% 判为相关）。

# 1. 核心实体（栽培马铃薯）
POTATO_CORE = (
    'potato[Title/Abstract] OR "Solanum tuberosum"[Title/Abstract] '
    'OR "Solanum tuberosum"[MeSH Terms] OR "Solanum sect. Petota"[MeSH Terms]'
)

# 2. 野生近缘种（Solanum sect. Petota 组内）。
#    只写物种名、不写 potato，因此不会与 POTATO_CORE 重叠；
#    实测 436 篇里 365 篇已被 CORE 覆盖，净增 66 篇。
WILD_SPECIES = [
    "Solanum phureja", "Solanum andigena", "Solanum andigenum",
    "Solanum juzepczukii", "Solanum curtilobum", "Solanum ajanhuiri",
    "Solanum stenotomum", "Solanum goniocalyx", "Solanum chaucha",
    "Solanum chacoense", "Solanum bulbocastanum", "Solanum commersonii",
    "Solanum demissum", "Solanum stoloniferum", "Solanum pinnatisectum",
    "Solanum verrucosum", "Solanum microdontum", "Solanum okadae",
]
POTATO_RELATIVES = "(" + " OR ".join(
    f'"{name}"[Title/Abstract]' for name in WILD_SPECIES) + ")"

# 3. 干扰项：甘薯（Ipomoea batatas）是旋花科，与马铃薯无关
SWEET_POTATO = '("sweet potato"[Title/Abstract] OR "Ipomoea batatas"[Title/Abstract])'

# 4. 「马铃薯学名证据」——用于在甘薯子句命中时救回两者同研的文献。
#    学名只认 Solanum tuberosum：甘薯论文里的 "potato" 指的就是甘薯，
#    拿 potato 当证据等于没排除。
TUBEROSUM_EVIDENCE = (
    '("Solanum tuberosum"[Title/Abstract] OR "Solanum tuberosum"[MeSH Terms])'
)

# 5. 排除项：只排除「提甘薯且无马铃薯学名证据」的文献。
#    必须写成 `A NOT (B NOT C)`，不能写 `A AND NOT (B NOT C)`——
#    实测 PubMed 的 `A AND NOT B` 会退化成交集语义，返回完全错误的结果。
EXCLUSIONS = f"NOT ({SWEET_POTATO} NOT {TUBEROSUM_EVIDENCE})"

# 最终组合查询：栽培种 + 野生近缘种，排除纯甘薯文献
# 括号保留：与实测验证过的候选式逐字符一致，改动前先重跑 tests/test_settings.py 的检索式测试
PUBMED_QUERY = f"(({POTATO_CORE}) OR {POTATO_RELATIVES}) {EXCLUSIONS}"

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
    "品质营养",     # 块茎成分、理化与感官品质、营养品质形成
    "产后处理",     # 采后生理、损伤、分选、物流
    "食品科学",     # 食品工艺与设备、配方与产品开发、食品安全
    "营养与健康",   # 人体营养、临床与膳食干预、动物饲养营养
    "生物医学与药理",  # 提取物药理、生物反应器产药用蛋白/疫苗/抗体
    "材料与能源",   # 淀粉基材料、生物塑料、胶黏剂、生物燃料、废弃物资源化
    "环境与生态",   # 生命周期评价、温室气体、面源污染、轮作与土壤生态
    "机械与智能装备",  # 农机装备、智能分选、遥感与决策模型
    "产业经济",     # 产业、市场、贸易、政策、消费
    "综述评论",     # 综述、评论、方法学探讨
    "其他",         # 难以归入上述任一类（兜底，非排除）
]

# 提示词里跟在标签后的一句话释义；不参与写库，只用于让模型选对标签。
RESEARCH_FIELD_HINTS = {
    "分子生物学": "基因、基因组、表达调控、蛋白结构与功能、组学",
    "遗传育种": "种质资源、育种方法、品种选育、抗性遗传、基因编辑育种",
    "栽培生理": "水肥、光温、种薯、光合、胁迫生理、栽培制度",
    "植物保护": "病害、虫害、杂草、农药、抗性机制、检测诊断",
    "加工储藏": "贮藏保鲜、加工适性、淀粉与糖代谢、加工后品质变化",
    "品质营养": "块茎成分、感官与理化品质、营养品质形成",
    "产后处理": "采后绿变、鲜切、涂膜、无损检测、分选分级",
    "食品科学": "食品工艺与设备、配方与产品开发、食品安全",
    "营养与健康": "人体营养、临床与膳食干预、动物饲养营养",
    "生物医学与药理": "提取物药理、生物反应器产药用蛋白/疫苗/抗体",
    "材料与能源": "淀粉基材料、生物塑料、胶黏剂、生物燃料、废弃物资源化",
    "环境与生态": "生命周期评价、温室气体、面源污染、轮作与土壤生态",
    "机械与智能装备": "农机装备、智能分选、遥感与决策模型",
    "产业经济": "产业、市场、贸易、政策、消费与人文社科",
    "综述评论": "综述、展望、评论",
    "其他": "以上都不贴切时使用",
}

DEFAULT_RESEARCH_FIELD = "其他"

# ── LLM 验证配置 ──────────────────────────────────────────────
# 全部可由 config/.env 覆盖，改配置无需动本文件。
# 未在 .env 中设置时使用下列默认值。
# 取值助手 _env_str/_env_int/_env_bool/_env_float 定义在文件顶部（TDM 段也要用）。

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
