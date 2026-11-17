# downloader/tdm_downloader.py
"""出版社 TDM（Text and Data Mining）API 全文获取。

与 `pdf_downloader` 的分工：
  - `pdf_downloader` 走 **PMC 开放获取**，免费、无授权问题，但只覆盖有 PMC ID
    且 PMC 侧确实放了 OA 全文的文献（实测有 PMC ID 的里仅约 21.7% 有全文）。
  - 本模块走 **订阅授权渠道**，把相关文献中属 Wiley/Elsevier/Springer 且有 DOI 的全文补齐。

覆盖三条渠道，授权模型完全不同：

  Wiley    PDF。TDM Client Token 授权，但官方客户端 README 明确**按 IP 判定**：
           必须在机构网段内执行，校外需先连学校 VPN。
           端点 https://api.wiley.com/onlinelibrary/tdm/v1/articles/{DOI}
           header `Wiley-TDM-Client-Token: <UUID>`

  Elsevier XML 全文。API key 只标识应用，**不含内容权限**；内容权限由请求源
           IP 归属机构、或 insttoken（X-ELS-Insttoken）决定。家庭宽带直连时
           即使有 key，返回体里也只有摘要（coredata），没有正文 section。
           端点 https://api.elsevier.com/content/article/doi/{DOI}?httpAccept=text/xml

  Springer PDF。无独立 key：先查 Crossref 的 text-mining 链接，再按该链接
           取 PDF。权限仍由请求源 IP 的机构订阅决定，校外拿到的是登录页。

产物与 `pdf_downloader` 同库同名（`data/pdfs_relevant/<pmid>.<ext>`），扩展名区分来源：
Wiley/Springer 是 `.pdf`，Elsevier 是 `.xml`。下游按 `<pmid>.*` 找全文即可，不关心格式。

**执行前必须先跑 `--step tdm-plan` 看清单与预估耗时，用户确认后再 `--step tdm`。**
`run_tdm_download` 不带 `confirm=True` 时只打印计划并退出，不发任何下载请求。
"""

import csv
import threading
import time
from datetime import datetime
from pathlib import Path

import requests

from config.settings import (
    DB_PATH, PDF_DIR, LOG_DIR, PROXY, OUTPUT_DIR, SUBDIR_FAILURE, SUBDIR_TDM,
    WILEY_TDM_TOKEN, ELSEVIER_API_KEY, ELSEVIER_INSTTOKEN,
    WILEY_TDM_INTERVAL, ELSEVIER_TDM_INTERVAL,
    TDM_MAX_BYTES, TDM_TIMEOUT,
)
from utils.db import get_conn
from utils.logger import get_logger

logger = get_logger("tdm_downloader", log_dir=LOG_DIR)

# ── 渠道常量 ──────────────────────────────────────────────────
WILEY = "wiley"
ELSEVIER = "elsevier"
SPRINGER = "springer"

# DOI 前缀归属。与 `scripts/export_tdm_dois.py` 的 PUBLISHER_BY_PREFIX 保持一致，
# 那边是全出版社的申请清单，这里只关心能走 TDM 的三家。
WILEY_DOI_PREFIXES = ("10.1111", "10.1002", "10.1046", "10.2134", "10.1034")
ELSEVIER_DOI_PREFIXES = (
    "10.1016", "10.1006", "10.1074", "10.4315", "10.1053", "10.1054",
)
SPRINGER_DOI_PREFIXES = ("10.1007", "10.1023", "10.1038", "10.1134")

WILEY_API_URL = "https://api.wiley.com/onlinelibrary/tdm/v1/articles/"
WILEY_TOKEN_HEADER = "Wiley-TDM-Client-Token"
ELSEVIER_API_URL = "https://api.elsevier.com/content/article/doi/"
CROSSREF_API_URL = "https://api.crossref.org/works/"

SPRINGER_TDM_INTERVAL = 2.0

# 全文文件可能出现的扩展名（PDF 来自 Wiley / PMC，XML 来自 Elsevier，TXT 来自 PMC 回退）
FULLTEXT_SUFFIXES = (".pdf", ".xml", ".txt")

# 判为「合法全文」的最小体积。XML 全文正常 20KB-1MB，PDF 数 MB。
MIN_VALID_BYTES = 2048

PLAN_FIELDS = ["pmid", "doi", "publisher", "title", "journal", "pub_year", "pmc_id"]
FAILED_FIELDS = ["pmid", "doi", "publisher", "status", "detail"]

# 429 退避基数（秒）。Wiley 条款是 60 次/10 分钟，撞限流时至少等这么久才有意义。
RATE_LIMIT_BACKOFF = 60
MAX_ATTEMPTS = 3
# 服务器偶尔只发几个字节就挂住，单次读超时不会触发；给整篇设总时限
STREAM_DEADLINE = 300

# 计划里的渠道排序（Wiley 先跑：单篇间隔长但成功率高，先拿到确定性收益）
_CHANNEL_ORDER = {WILEY: 0, ELSEVIER: 1, SPRINGER: 2}


def _output_dir(subdir: str) -> Path:
    """按**调用时**的模块级 OUTPUT_DIR 派生子目录。

    不能在 import 期固化成 `OUTPUT_DIR / subdir` 常量，也不能在函数里
    `from config.settings import OUTPUT_DIR` 重新导入 —— 前者测试 patch 失效，
    后者绕过了 patch 直接读 settings 模块，同样重定向不到临时目录。
    """
    return Path(OUTPUT_DIR) / subdir


def classify_doi(doi: str | None) -> str | None:
    """DOI → 渠道名（wiley / elsevier / springer），不属这些渠道返回 None"""
    text = (doi or "").strip().lower()
    if not text.startswith("10.") or "/" not in text:
        # 没有 "/" 的只是前缀（如 "10.1111"），不是完整 DOI，不能当成可下载项
        return None
    prefix = ".".join(text.split("/")[0].split(".")[:2])
    if prefix in WILEY_DOI_PREFIXES:
        return WILEY
    if prefix in ELSEVIER_DOI_PREFIXES:
        return ELSEVIER
    if prefix in SPRINGER_DOI_PREFIXES:
        return SPRINGER
    return None


def local_fulltext_path(pmid: str) -> Path | None:
    """已存在的全文文件路径（按 FULLTEXT_SUFFIXES 顺序找），没有则 None"""
    for suffix in FULLTEXT_SUFFIXES:
        path = PDF_DIR / f"{pmid}{suffix}"
        if path.exists() and path.stat().st_size > MIN_VALID_BYTES:
            return path
    return None


def _available_channels() -> dict[str, bool]:
    """哪些渠道具备发起请求的最低条件（缺 token 的渠道直接跳过，不静默下载空文件）"""
    return {
        WILEY: bool(WILEY_TDM_TOKEN),
        ELSEVIER: bool(ELSEVIER_API_KEY),
        SPRINGER: True,  # 无 key，靠校园网 IP 的订阅权限
    }


# ── 计划生成 ──────────────────────────────────────────────────

def build_tdm_plan(db_path: Path = DB_PATH) -> list[dict]:
    """挑出「相关 + 有 DOI + 属 Wiley/Elsevier/Springer + 本地还没有全文」的文献

    返回按渠道排序的 dict 列表，每项含 pmid / doi / publisher / 书目信息。
    只读数据库，不发网络请求。
    只处理复核通过的相关文献。
    """
    sql = """
    SELECT a.pmid, a.doi, a.title, a.journal, a.pub_year, a.pmc_id
    FROM articles a
    WHERE a.doi IS NOT NULL AND a.doi != ''
    ORDER BY a.pmid
    """
    with get_conn(db_path) as conn:
        rows = [dict(r) for r in conn.execute(sql).fetchall()]

    from downloader.fulltext_library import relevant_pmids

    relevant = relevant_pmids(db_path)
    rows = [row for row in rows if row["pmid"] in relevant]

    plan: list[dict] = []
    for row in rows:
        channel = classify_doi(row["doi"])
        if channel is None:
            continue
        if local_fulltext_path(row["pmid"]):
            continue
        plan.append({
            "pmid": row["pmid"],
            "doi": row["doi"],
            "publisher": channel,
            "title": row["title"] or "",
            "journal": row["journal"] or "",
            "pub_year": row["pub_year"] or "",
            "pmc_id": row["pmc_id"] or "",
        })
    # 按渠道分组排序（Wiley 在前）：下载循环里每个渠道的间隔不同，
    # 交替切换渠道会让 sleep 时长反复跳，分组后节奏稳定。
    plan.sort(key=lambda item: (_CHANNEL_ORDER.get(item["publisher"], 9), item["pmid"]))
    return plan


def export_tdm_plan(plan: list[dict]) -> Path | None:
    """把计划写成 CSV（`data/output/tdm/tdm_plan_<时间戳>.csv`），空计划返回 None"""
    if not plan:
        return None
    out_dir = _output_dir(SUBDIR_TDM)
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = out_dir / f"tdm_plan_{ts}.csv"
    with open(path, "w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=PLAN_FIELDS)
        writer.writeheader()
        writer.writerows(plan)
    return path


def latest_tdm_plan() -> Path | None:
    """最新的计划文件，供 `--step tdm` 复用（没有则返回 None）"""
    out_dir = _output_dir(SUBDIR_TDM)
    if not out_dir.exists():
        return None
    files = sorted(out_dir.glob("tdm_plan_*.csv"), reverse=True)
    return files[0] if files else None


def load_tdm_plan(path: Path) -> list[dict]:
    with open(path, encoding="utf-8-sig", newline="") as fh:
        return [dict(row) for row in csv.DictReader(fh)]


def describe_plan(plan: list[dict]) -> str:
    """把计划汇总成一段人读文本（打印给用户做下载前确认）"""
    if not plan:
        return "没有需要走 TDM 的文献（可能都已下载，或没有 Wiley/Elsevier/Springer 的 DOI）。"
    channels = _available_channels()
    by_channel: dict[str, int] = {}
    for item in plan:
        by_channel[item["publisher"]] = by_channel.get(item["publisher"], 0) + 1

    lines = [f"共 {len(plan)} 篇待下载："]
    for name in (WILEY, ELSEVIER, SPRINGER):
        count = by_channel.get(name, 0)
        if not count:
            continue
        if not channels[name]:
            missing = "WILEY_TDM_TOKEN" if name == WILEY else "ELSEVIER_API_KEY"
            lines.append(f"  {name:<9}{count:>6} 篇   ⚠ 未配置 {missing}，无法下载")
            continue
        interval = {
            WILEY: WILEY_TDM_INTERVAL,
            ELSEVIER: ELSEVIER_TDM_INTERVAL,
            SPRINGER: SPRINGER_TDM_INTERVAL,
        }[name]
        minutes = count * interval / 60
        fmt = "XML 全文" if name == ELSEVIER else "PDF"
        lines.append(
            f"  {name:<9}{count:>6} 篇   {fmt}，间隔 {interval:g}s，"
            f"预计 {minutes:.0f} 分钟"
        )
    if not channels[ELSEVIER] or not ELSEVIER_INSTTOKEN:
        lines.append(
            "  注意：Elsevier 若未配置 ELSEVIER_INSTTOKEN 且请求不来自校园 IP，"
            "只会返回摘要，正文缺失的会被记为失败。"
        )
    return "\n".join(lines)


# ── 单篇下载 ──────────────────────────────────────────────────

def _request(url: str, headers: dict, use_proxy: bool = False) -> requests.Response:
    kwargs: dict = {"headers": headers, "timeout": TDM_TIMEOUT, "stream": True}
    if use_proxy and PROXY:
        kwargs["proxies"] = {"http": PROXY, "https": PROXY}
    return requests.get(url, **kwargs)


def _save_stream(resp: requests.Response, dest: Path, min_bytes: int = MIN_VALID_BYTES) -> int:
    """流式落盘，超过 TDM_MAX_BYTES 直接中断并删文件（错误页可能被当成正文）"""
    dest.parent.mkdir(parents=True, exist_ok=True)
    temp = dest.with_name(dest.name + ".part")
    written = 0
    deadline = time.monotonic() + STREAM_DEADLINE
    # 服务器只握手不发数据时，阻塞在 socket read 里永远走不到循环内的超时检查
    def abort():
        close = getattr(resp, "close", None)
        if close:
            close()

    watchdog = threading.Timer(STREAM_DEADLINE, abort)
    watchdog.daemon = True
    watchdog.start()
    try:
        with open(temp, "wb") as fh:
            for chunk in resp.iter_content(chunk_size=65536):
                if time.monotonic() > deadline:
                    raise requests.Timeout(f"单篇下载超过 {STREAM_DEADLINE}s")
                if not chunk:
                    continue
                written += len(chunk)
                if written > TDM_MAX_BYTES:
                    raise ValueError(f"响应超过 {TDM_MAX_BYTES} 字节上限")
                fh.write(chunk)
        if written <= min_bytes:
            raise ValueError(f"响应过小（{written} 字节），可能不是全文")
        temp.replace(dest)
        return written
    except Exception as exc:
        if temp.exists():
            temp.unlink()
        if time.monotonic() >= deadline and not isinstance(exc, requests.Timeout):
            raise requests.Timeout(f"单篇下载超过 {STREAM_DEADLINE}s") from exc
        raise
    finally:
        watchdog.cancel()


def download_wiley(doi: str, dest: Path, use_proxy: bool = False) -> tuple[bool, str]:
    """Wiley TDM：按 DOI 取 PDF。返回 (成功?, 状态说明)"""
    if not WILEY_TDM_TOKEN:
        return False, "missing_token"

    url = WILEY_API_URL + requests.utils.quote(doi, safe="")
    headers = {WILEY_TOKEN_HEADER: WILEY_TDM_TOKEN, "User-Agent": "TDMClient/1.2.0"}

    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            resp = _request(url, headers, use_proxy=use_proxy)
        except requests.RequestException as exc:
            if attempt == MAX_ATTEMPTS:
                return False, f"network_error: {exc}"
            time.sleep(2 ** attempt)
            continue

        if resp.status_code == 200:
            try:
                _save_stream(resp, dest)
                return True, "ok"
            except Exception as exc:
                return False, f"save_error: {exc}"
        if resp.status_code == 403:
            # 该 DOI 不在机构订阅范围内，或 token 对应的机构无权限
            return False, "access_denied"
        if resp.status_code == 404:
            return False, "unknown_doi"
        if resp.status_code == 429:
            if attempt == MAX_ATTEMPTS:
                return False, "rate_limited"
            wait = RATE_LIMIT_BACKOFF * attempt
            logger.warning(f"Wiley 限流，等待 {wait}s 后重试（{attempt}/{MAX_ATTEMPTS}）")
            time.sleep(wait)
            continue
        return False, f"http_{resp.status_code}"

    return False, "exhausted"


def _elsevier_has_fulltext(body: bytes) -> bool:
    """判断 Elsevier 响应里是否含正文段落。

    **不能只看 `<xocs:doc>`**：无内容权限时 Elsevier 也回 200 且带
    `<xocs:doc><coredata>...`（只有书目元数据、没有正文），拿它当全文会把
    摘要当全文存下来。正文段落的标志是 `<ce:para` / `<ce:section`。
    """
    return b"<ce:para" in body or b"<ce:section" in body


def download_elsevier(doi: str, dest: Path, use_proxy: bool = False) -> tuple[bool, str]:
    """Elsevier TDM：按 DOI 取 XML 全文。返回 (成功?, 状态说明)"""
    if not ELSEVIER_API_KEY:
        return False, "missing_key"

    url = ELSEVIER_API_URL + requests.utils.quote(doi, safe="")
    headers = {"X-ELS-APIKey": ELSEVIER_API_KEY, "Accept": "text/xml"}
    if ELSEVIER_INSTTOKEN:
        headers["X-ELS-Insttoken"] = ELSEVIER_INSTTOKEN

    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            resp = _request(url, headers, use_proxy=use_proxy)
        except requests.RequestException as exc:
            if attempt == MAX_ATTEMPTS:
                return False, f"network_error: {exc}"
            time.sleep(2 ** attempt)
            continue

        if resp.status_code == 200:
            try:
                body = resp.content
            except requests.RequestException as exc:
                if attempt == MAX_ATTEMPTS:
                    return False, f"network_error: {exc}"
                time.sleep(2 ** attempt)
                continue
            if not _elsevier_has_fulltext(body):
                # 有 key 但无内容权限：Elsevier 仍回 200，只是正文缺失
                return False, "no_entitlement"
            try:
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(body)
                return True, "ok"
            except Exception as exc:
                return False, f"save_error: {exc}"
        if resp.status_code in (401, 403):
            return False, "access_denied"
        if resp.status_code == 404:
            return False, "unknown_doi"
        if resp.status_code == 429:
            if attempt == MAX_ATTEMPTS:
                return False, "rate_limited"
            wait = RATE_LIMIT_BACKOFF * attempt
            logger.warning(f"Elsevier 限流，等待 {wait}s 后重试（{attempt}/{MAX_ATTEMPTS}）")
            time.sleep(wait)
            continue
        return False, f"http_{resp.status_code}"

    return False, "exhausted"


def _crossref_pdf_url(doi: str, use_proxy: bool = False) -> str | None:
    """Crossref 的 text-mining 链接里挑 PDF 入口；没有则 None"""
    url = CROSSREF_API_URL + requests.utils.quote(doi, safe="")
    try:
        resp = _request(url, {"User-Agent": "PotatoLit/1.0 (mailto:noreply@example.com)"},
                        use_proxy=use_proxy)
        links = resp.json().get("message", {}).get("link", [])
    except ValueError:
        return None
    links = [link for link in links if link.get("intended-application") == "text-mining"]
    pdfs = [link["URL"] for link in links if link.get("content-type") == "application/pdf"]
    return (pdfs or [link["URL"] for link in links] or [None])[0]


def download_springer(doi: str, dest: Path, use_proxy: bool = False) -> tuple[bool, str]:
    """Springer/Nature TDM：Crossref 链接 + 校园 IP 订阅权限。返回 (成功?, 状态说明)"""
    try:
        url = _crossref_pdf_url(doi, use_proxy=use_proxy)
    except requests.RequestException as exc:
        # Crossref 不可达 ≠ 出版社没有 TDM 链接，不能记成 no_link
        return False, f"crossref_error: {exc}"
    if not url:
        return False, "no_link"

    headers = {
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/131.0 Safari/537.36"),
        "Accept": "application/pdf,*/*",
    }
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            resp = _request(url, headers, use_proxy=use_proxy)
        except requests.RequestException as exc:
            if attempt == MAX_ATTEMPTS:
                return False, f"network_error: {exc}"
            time.sleep(2 ** attempt)
            continue

        if resp.status_code == 200:
            try:
                # 先落盘再验 %PDF，所以这里只挡空响应
                _save_stream(resp, dest, min_bytes=4)
            except requests.RequestException as exc:
                # 传输中断要重试，不能让单篇异常终止整个批次
                # 超时说明对端不发货，重试只会再等一轮
                if attempt == MAX_ATTEMPTS or isinstance(exc, requests.Timeout):
                    return False, f"network_error: {exc}"
                time.sleep(2 ** attempt)
                continue
            except Exception as exc:
                return False, f"save_error: {exc}"
            with open(dest, "rb") as fh:
                is_pdf = fh.read(4) == b"%PDF"
            if not is_pdf:
                # 校外或未订阅时 Springer 会回 200 的 HTML 登录页
                dest.unlink()
                return False, "not_pdf"
            return True, "ok"
        if resp.status_code in (401, 403):
            return False, "access_denied"
        if resp.status_code == 404:
            return False, "unknown_doi"
        if resp.status_code == 429:
            if attempt == MAX_ATTEMPTS:
                return False, "rate_limited"
            time.sleep(RATE_LIMIT_BACKOFF * attempt)
            continue
        return False, f"http_{resp.status_code}"

    return False, "exhausted"


def download_one(item: dict, use_proxy: bool = False) -> tuple[bool, str, Path]:
    """按渠道分发单篇下载。返回 (成功?, 状态, 目标路径)"""
    channel = item["publisher"]
    pmid = item["pmid"]
    if channel == WILEY:
        dest = PDF_DIR / f"{pmid}.pdf"
        ok, status = download_wiley(item["doi"], dest, use_proxy=use_proxy)
    elif channel == ELSEVIER:
        dest = PDF_DIR / f"{pmid}.xml"
        ok, status = download_elsevier(item["doi"], dest, use_proxy=use_proxy)
    elif channel == SPRINGER:
        dest = PDF_DIR / f"{pmid}.pdf"
        ok, status = download_springer(item["doi"], dest, use_proxy=use_proxy)
    else:
        return False, "unsupported_channel", PDF_DIR / f"{pmid}.pdf"
    return ok, status, dest


# ── 主流程 ────────────────────────────────────────────────────

def export_tdm_failures(failed: list[dict]) -> Path | None:
    if not failed:
        return None
    out_dir = _output_dir(SUBDIR_FAILURE)
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = out_dir / f"tdm_failed_{ts}.csv"
    with open(path, "w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=FAILED_FIELDS)
        writer.writeheader()
        writer.writerows(failed)
    return path


def run_tdm_plan(db_path: Path = DB_PATH) -> list[dict]:
    """只生成计划并打印汇总，不下载。这是下载前的确认材料。"""
    logger.info("=" * 60)
    logger.info("阶段：TDM 下载计划（不发起任何下载请求）")
    logger.info("=" * 60)

    plan = build_tdm_plan(db_path)
    path = export_tdm_plan(plan)
    logger.info(describe_plan(plan))
    if path:
        logger.info(f"计划清单: {path}")
        logger.info("确认后执行: python main.py --step tdm --yes")
    return plan


def run_tdm_download(
    db_path: Path = DB_PATH,
    plan_path: Path | None = None,
    confirm: bool = False,
    limit: int | None = None,
    use_proxy: bool = False,
) -> dict:
    """执行 TDM 下载。

    `confirm=False`（默认）时只打印计划与将要执行的规模，**不发起任何下载请求**。
    必须显式传 `confirm=True`（CLI 上是 `--yes`）才会真正下载。
    """
    logger.info("=" * 60)
    logger.info("阶段：TDM 全文下载（Wiley/Springer PDF / Elsevier XML）")
    logger.info("=" * 60)

    if plan_path:
        plan = load_tdm_plan(Path(plan_path))
        logger.info(f"读取计划清单: {plan_path}（{len(plan)} 篇）")
    else:
        plan = build_tdm_plan(db_path)
        latest = latest_tdm_plan()
        if latest:
            logger.info(f"提示：最近一次计划清单为 {latest}（本次按数据库实时重算）")

    from downloader.fulltext_library import relevant_pmids

    relevant = relevant_pmids(db_path)
    before = len(plan)
    plan = [item for item in plan if item["pmid"] in relevant]
    if len(plan) < before:
        logger.info(f"过滤计划中已翻转为不相关的文献 {before - len(plan)} 篇。")

    if limit is not None:
        # limit=0 也要生效（空跑一次看流程），所以判 None 而不是判真值
        plan = plan[:limit]
        logger.info(f"本次仅处理前 {limit} 篇（--limit）")

    logger.info(describe_plan(plan))

    channels = _available_channels()
    runnable = [item for item in plan if channels[item["publisher"]]]
    if not runnable:
        logger.warning("没有可下载项（缺 token 或计划为空）。")
        return {"total": len(plan), "success": 0, "failed": 0, "skipped": 0,
                "planned": 0, "confirmed": bool(confirm)}

    if not confirm:
        logger.warning(
            "未加 --yes，本次不下载。确认上述规模后执行: "
            "python main.py --step tdm --yes"
        )
        return {"total": len(plan), "success": 0, "failed": 0,
                "skipped": 0, "planned": len(runnable), "confirmed": False}

    success = failed = skipped = 0
    failed_rows: list[dict] = []
    started = time.time()

    for index, item in enumerate(plan, 1):
        pmid = item["pmid"]
        if not channels[item["publisher"]]:
            skipped += 1
            continue
        existing = local_fulltext_path(pmid)
        if existing:
            skipped += 1
            continue

        ok, status, dest = download_one(item, use_proxy=use_proxy)
        if ok and not dest.exists():
            # 渠道函数报成功但文件不在，说明中间被清理或写到了别处，
            # 不能算成功（否则统计虚高、失败清单漏项）
            ok, status = False, "missing_file"
        if ok:
            success += 1
            size_mb = dest.stat().st_size / 1024 / 1024
            logger.info(
                f"  [{index}/{len(plan)}] {pmid} {item['publisher']} "
                f"→ {dest.name}（{size_mb:.1f} MB）"
            )
        else:
            failed += 1
            failed_rows.append({
                "pmid": pmid, "doi": item["doi"],
                "publisher": item["publisher"],
                "status": status, "detail": item["title"][:120],
            })
            logger.warning(f"  [{index}/{len(plan)}] {pmid} 失败: {status}")

        if index < len(plan):
            interval = {
                WILEY: WILEY_TDM_INTERVAL,
                ELSEVIER: ELSEVIER_TDM_INTERVAL,
                SPRINGER: SPRINGER_TDM_INTERVAL,
            }.get(item["publisher"], ELSEVIER_TDM_INTERVAL)
            time.sleep(interval)

    elapsed = time.time() - started
    logger.info(
        f"TDM 下载完成：成功 {success} 篇，失败 {failed} 篇，跳过 {skipped} 篇，"
        f"耗时 {elapsed / 60:.1f} 分钟。"
    )
    if failed_rows:
        csv_path = export_tdm_failures(failed_rows)
        logger.info(f"失败清单: {csv_path}")
        status_counts: dict[str, int] = {}
        for row in failed_rows:
            status_counts[row["status"]] = status_counts.get(row["status"], 0) + 1
        for status, count in sorted(status_counts.items(), key=lambda kv: -kv[1]):
            logger.info(f"  {status:<18}{count:>6} 篇")

    logger.info(f"存储位置: {PDF_DIR}")
    return {"total": len(plan), "success": success, "failed": failed,
            "skipped": skipped, "confirmed": True}
