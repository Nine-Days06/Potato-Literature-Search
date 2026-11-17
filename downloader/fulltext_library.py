"""相关全文库的查询与清理。

PDF_DIR 是唯一的全文库，只保存复核通过的相关文献（按 PMID 命名）。
下载入口负责只挑相关文献；导出时只清掉判定翻转后残留的副本，不再从
任何“总库”拷贝。
"""

import logging
import json
import re
from pathlib import Path

from config.settings import DB_PATH, PDF_DIR, LOG_DIR, OUTPUT_DIR
from utils.db import get_conn
from utils.logger import get_logger

logger = get_logger("fulltext_library", log_dir=LOG_DIR)

FULLTEXT_SUFFIXES = (".pdf", ".xml", ".txt")
_PMID_FILE_RE = re.compile(r"^(\d+)\.(?:pdf|xml|txt)$", re.IGNORECASE)

# ── 完整性体检阈值（2026-11-14 抽样标定） ──────────────────────
# 抽样 200 篇：结构校验 0 失败；3 篇只有版权声明页（103 字符）；
# 正确文件的标题覆盖率最低 0.86，错配的占位页为 0。
TITLE_MATCH_MIN = 0.85
TITLE_WEAK_MIN = 0.60
PLACEHOLDER_MAX_CHARS = 300
PLACEHOLDER_MAX_PAGES = 2
PLACEHOLDER_TEXT_MAX_CHARS = 500
NO_TEXT_MAX_CHARS = 100
NO_TEXT_AVG_CHARS = 1000
NO_TEXT_MIN_PAGES = 4
SHORT_MAX_CHARS = 1500

PLACEHOLDER_MARKERS = (
    "reproduced with permission of the copyright owner",
)
_PLACEHOLDER_TITLE_RE = re.compile(
    r"^\[\s*(not available|no title|untitled)\s*\]\.?$", re.IGNORECASE
)

_SUPPLEMENT_MARKERS = (
    "supplemental", "supplementary material", "supplementary data", "supporting information",
)
_SUPPLEMENT_MAX_CHARS = 6000

# 撤稿声明/勘误本身就是 1-2 页短文本，不能按“过短”判有问题。
SHORT_NOTICE_TYPES = frozenset({
    "Published Erratum", "Retraction Notice", "Retracted Publication",
    "Corrected and Republished Article",
})

_FRONT_PAGE_MARKERS = (
    "this article was downloaded by",
    "full terms & conditions of access",
    "bioone sees sustainable scholarly publishing",
    "this content has been downloaded from",
    "research libraries, and research funders",
)

_LANG_HINTS = {
    "de": ("der", "die", "und", "des", "von", "aus", "bei", "mit", "nicht", "eine"),
    "fr": ("les", "des", "une", "dans", "pour", "avec", "sur", "est", "par"),
    "es": ("los", "las", "una", "para", "con", "por", "del", "que"),
    "da": ("og", "af", "til", "den", "det", "som", "ikke", "med", "en", "et"),
}

_DOI_RE = re.compile(r'10\.\d{4,9}/[^\s"<>,;)\]}\u201d]+', re.IGNORECASE)

# 老 PDF 缺 ToUnicode 映射时，pypdf 会抽出字形名 /C65/C98/...（= ASCII 65/98 = "Ab..."）。
_GLYPH_RE = re.compile(r"/[Cc](\d+)")
# 另一些老 PDF 直接用十进制字节值：/65/98 与 /G65（= ASCII 65 = "A"）。
_GLYPH_DECIMAL_RE = re.compile(r"/[Gg]?(\d{1,3})")

_STOPWORDS = frozenset(
    "a an the of in on for and or to with by from at as is are was were be been "
    "this that these those using use used its their it we our".split()
)

# 判断抽出来的文本是不是自然语言（扫描件/字体乱码会失败）。
_COMMON_WORDS = ("the ", " of ", " and ", " in ", " for ", " to ", " with ")
_COMMON_WORDS_SET = frozenset(word.strip() for word in _COMMON_WORDS)

INTEGRITY_FIELDS = [
    "pmid", "file", "suffix", "size_kb", "pages", "chars",
    "title_cov", "ngram_cov", "doi_in_pdf", "dup_group", "verdict", "reason", "suggested_action",
]
_JUDGE_FIELDS = (
    "pages", "chars", "title_cov", "ngram_cov", "doi_in_pdf",
    "verdict", "reason", "suggested_action",
)

# 只有这三类属于硬结论，--apply 才隔离；其余转人工。
# mismatch 实测仍有预印本改题、非英语原文等对应情况，只报告不自动删。
HARD_FAIL_VERDICTS = frozenset({"broken", "placeholder", "front_page"})


def existing_fulltext_path(pmid: str) -> Path | None:
    """相关全文库中该 PMID 的全文路径（按后缀顺序），没有则 None。"""
    for suffix in FULLTEXT_SUFFIXES:
        path = Path(PDF_DIR) / f"{pmid}{suffix}"
        if path.exists():
            return path
    return None


def relevant_pmids(db_path: Path | None = None) -> set[str]:
    """复核通过的相关 PMID 集合。

    延迟导入 llm_validator：它会拉起 openai，单纯查询全文库时不需要这个开销。
    """
    from cleaner.llm_validator import _REVIEW_PASS_PREDICATE

    sql = f"""
    SELECT a.pmid
    FROM articles a
    JOIN llm_validation v ON a.pmid = v.pmid
    WHERE {_REVIEW_PASS_PREDICATE}
      AND COALESCE(a.source, 'pubmed') != 'potato_ai'
    """
    with get_conn(db_path or DB_PATH) as conn:
        return {row[0] for row in conn.execute(sql)} - excluded_fulltext_pmids()


def excluded_fulltext_pmids() -> set[str]:
    """被明确移出全文库的 PMID（撤稿原文、撤稿声明、孤立勘误）。

    名单由 scripts/exclude_notice_fulltext.py --apply 写，文件已挪到
    `_excluded_notices/`；这里排除是为了让 pdf / tdm 不再把它们抓回来。
    """
    path = Path(OUTPUT_DIR) / "cache" / "fulltext_excluded.json"
    try:
        return set(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return set()


def load_records(db_path: Path | None = None) -> dict[str, dict]:
    """pmid → {title, doi, article_types}，供完整性体检做身份比对。"""
    sql = "SELECT pmid, title, doi, article_types FROM articles"
    with get_conn(db_path or DB_PATH) as conn:
        return {
            str(pmid): {
                "title": title or "",
                "doi": doi or "",
                "article_types": article_types or "",
            }
            for pmid, title, doi, article_types in conn.execute(sql)
        }


def prune_relevant_fulltext(db_path: Path | None = None) -> dict:
    """删除相关全文库中已翻转为不相关的副本。"""
    relevant = relevant_pmids(db_path)
    dest_dir = Path(PDF_DIR)
    dest_dir.mkdir(parents=True, exist_ok=True)

    removed = 0
    for path in dest_dir.iterdir():
        match = _PMID_FILE_RE.match(path.name)
        if match and match.group(1) not in relevant:
            path.unlink()
            removed += 1

    logger.info(
        f"相关全文库清理完成：相关 {len(relevant)} 篇，删除翻转副本 {removed} 篇。"
    )
    return {"removed": removed, "relevant": len(relevant)}


# ── 完整性体检 ─────────────────────────────────────────────────

# pypdf 对老 PDF 会大量输出 "Ignoring wrong pointing object" 等 warning，
# 它们不代表文件损坏（strict=False 下仍可读），静音以免淹没体检输出。
logging.getLogger("pypdf").setLevel(logging.ERROR)

_SUGGESTED_ACTION = {
    "broken": "redownload",
    "placeholder": "redownload_or_manual",
    "front_page": "redownload",
    "mismatch": "manual_review",
    "supplement": "manual_review",
    "non_english": "manual_review",
    "unparsed": "manual_review",
    "too_short": "manual_review",
    "no_text": "manual_review",
    "weak": "manual_review",
    "ok": "",
}


def normalize_title(text: str) -> str:
    """标题归一化：小写、连字还原、非字母数字转空格。"""
    text = (text or "").lower().replace("ﬁ", "fi").replace("ﬂ", "fl")
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text).split())


def _normalize_body(text: str) -> str:
    """正文归一化；先合并跨行连字符（"po-\\ntato" → "potato"）。"""
    text = re.sub(r"-\s*\n\s*", "", text or "")
    return normalize_title(text)


def _decode_glyph_names(text: str) -> str:
    """把 /C65 这类字形名还原成可打印 ASCII；非 ASCII/控制字符留空格。"""
    if "/C" not in text:
        return text

    def repl(match: re.Match) -> str:
        code = int(match.group(1))
        return chr(code) if 32 <= code <= 126 else " "

    return _GLYPH_RE.sub(repl, text)


def _decode_decimal_glyphs(text: str) -> str:
    """把 /65、/G65 这类十进制字形名还原成 ASCII。

    只在原文判不出可读文本时才调用：正常 PDF 里的 "1/2" 会被一起改写，
    所以不能无条件套用。
    """

    def repl(match: re.Match) -> str:
        code = int(match.group(1))
        return chr(code) if 32 <= code <= 126 else " "

    return _GLYPH_DECIMAL_RE.sub(repl, text)


def _title_tokens(title: str) -> list[str]:
    return [
        t for t in normalize_title(title).split()
        if t not in _STOPWORDS and len(t) > 2
    ]


def title_coverage(title: str, body: str) -> float | None:
    """数据库标题的词有多大比例出现在全文文本里；标题为空时 None。"""
    tokens = _title_tokens(title)
    if not tokens:
        return None
    normalized = _normalize_body(body)
    seen = set(normalized.split())
    coverage = sum(1 for t in tokens if t in seen) / len(tokens)
    if coverage >= TITLE_WEAK_MIN:
        return coverage

    # 1980-90 年代 PDF 常丢词间空格（"Woundinginducesthefirstenzyme"），
    # 逐词匹配会全军覆没；去掉空格后用标题前 60% 做整串比对兜底。
    compact_title = normalize_title(title).replace(" ", "")
    if len(tokens) >= 3 and len(compact_title) >= 20:
        prefix = compact_title[: max(20, int(len(compact_title) * 0.6))]
        if prefix in normalized.replace(" ", ""):
            return max(coverage, 0.9)
    return coverage


def _first_doi(text: str) -> str:
    match = _DOI_RE.search(text or "")
    return match.group(0).rstrip(".,;:").lower() if match else ""


def ngram_coverage(title: str, body: str, size: int = 5) -> float | None:
    """标题字符 5-gram 出现在正文里的比例。

    比逐词匹配抗 OCR 错字（"Ptant"/"Plant"）和丢词间空格；标题过短时不可靠。
    """
    compact_title = normalize_title(title).replace(" ", "")
    compact_body = _normalize_body(body).replace(" ", "")
    if len(compact_title) < 10 or len(compact_body) < size:
        return None
    grams = {compact_title[i:i + size] for i in range(len(compact_title) - size + 1)}
    return sum(1 for gram in grams if gram in compact_body) / len(grams)


def _page_has_large_image(page) -> bool:
    """整页扫描图的宽度通常 ≥1200px（300dpi 传真扫描约 2528px）。"""
    try:
        resources = page.get("/Resources")
        xobjects = resources.get("/XObject") if resources else None
        if not xobjects:
            return False
        for ref in xobjects.get_object().values():
            obj = ref.get_object()
            if obj.get("/Subtype") == "/Image" and (obj.get("/Width") or 0) >= 1200:
                return True
    except Exception:  # noqa: BLE001 - 编码异常的页面按无图处理
        return False
    return False


def _read_pdf_pages(path: Path, max_pages: int = 3) -> tuple[int, str, bool]:
    """返回 (总页数, 前 N 页文本, 前 N 页是否有整页扫描图)。"""
    from pypdf import PdfReader

    reader = PdfReader(str(path), strict=False)
    pages = reader.pages[:max_pages]
    parts = [_decode_glyph_names(page.extract_text() or "") for page in pages]
    has_scan = any(_page_has_large_image(page) for page in pages)
    body = "\n".join(parts)
    if not _looks_like_text(body):
        decoded = _decode_decimal_glyphs(body)
        if _looks_like_text(decoded):
            body = decoded
    return len(reader.pages), body, has_scan


def _read_text(path: Path) -> str:
    raw = path.read_text(encoding="utf-8", errors="replace")
    if path.suffix.lower() == ".xml":
        raw = re.sub(r"<[^>]+>", " ", raw)
    return raw


def _looks_like_text(body: str) -> bool:
    """扫描件/字体乱码会抽出零散字符，功能词命中率极低。"""
    low = " " + body.lower()
    hits = sum(low.count(word) for word in _COMMON_WORDS)
    if hits >= max(5, len(body) // 500):
        return True

    # 老 PDF 会丢掉词间空格，改在去空格文本里数高频三字串。
    compact = re.sub(r"[^a-z]", "", body.lower())
    compact_hits = compact.count("the") + compact.count("and") + compact.count("ofthe")
    return compact_hits >= max(10, len(compact) // 300)


def _detect_foreign_language(body: str) -> str:
    """PDF 是原文（德/法/西），PubMed 标题却是英文译文时，两者永远对不上。"""
    words = normalize_title(body).split()
    counts = {lang: 0 for lang in _LANG_HINTS}
    for word in words:
        for lang, hints in _LANG_HINTS.items():
            if word in hints:
                counts[lang] += 1
    english = sum(word in _COMMON_WORDS_SET for word in words)
    lang = max(counts, key=counts.get)
    return lang if counts[lang] >= 5 and counts[lang] > english else ""


def _detect_non_latin(body: str) -> str:
    """俄语/中日韩文本在 normalize 后会被清空，用拉丁字母占比判断。"""
    stripped = body.strip()
    if not stripped:
        return ""
    latin = sum(1 for ch in stripped if ch.isascii() and ch.isalpha())
    return "non_latin" if latin / len(stripped) < 0.3 else ""


def _is_placeholder_title(title: str) -> bool:
    return bool(_PLACEHOLDER_TITLE_RE.match((title or "").strip()))


def _is_short_notice(record: dict) -> bool:
    types = {t.strip() for t in (record.get("article_types") or "").split("|") if t.strip()}
    return bool(types & SHORT_NOTICE_TYPES)


def _is_supplement(body: str) -> bool:
    low = body.lower()
    return any(m in low for m in _SUPPLEMENT_MARKERS) and len(body) < _SUPPLEMENT_MAX_CHARS


def _is_front_page(body: str) -> bool:
    """出版社 landing page / 下载页被当成 PDF 存了下来。"""
    low = body.lower()
    return any(m in low for m in _FRONT_PAGE_MARKERS)


def _result(out: dict, verdict: str, reason: str, **extra) -> dict:
    out.update(extra)
    out["verdict"] = verdict
    out["reason"] = reason
    out["suggested_action"] = _SUGGESTED_ACTION[verdict]
    return out


def judge_fulltext(
    body: str,
    record: dict | None = None,
    pages: int = 0,
    has_scan: bool = False,
) -> dict:
    """对已抽出的文本做内容/身份判定；不读磁盘，供 pypdf 和 MinerU 共用。

    返回 `_JUDGE_FIELDS` 各列。`pages=0` 表示页数未知（纯文本或多个页面已合并）。
    `has_scan=True` 表示页面由整页扫描图占据，此时文字层可能只剩版权水印。
    """
    record = record or {}
    out = {field: "" for field in _JUDGE_FIELDS}
    out["pages"] = pages
    chars = len(body.strip())
    out["chars"] = chars

    if any(marker in body.lower() for marker in PLACEHOLDER_MARKERS):
        if has_scan:
            return _result(out, "no_text", "scanned_page_watermark")
        return _result(out, "placeholder", "copyright_placeholder")
    if pages and pages <= PLACEHOLDER_MAX_PAGES and chars < PLACEHOLDER_TEXT_MAX_CHARS:
        if has_scan:
            return _result(out, "no_text", "scanned_page")
        return _result(out, "placeholder", "cover_page_only")
    if chars < NO_TEXT_MAX_CHARS:
        return _result(out, "no_text", "no_text_layer")
    if pages >= NO_TEXT_MIN_PAGES and chars < NO_TEXT_AVG_CHARS:
        return _result(out, "no_text", "scanned_only")
    if not _looks_like_text(body):
        language = _detect_foreign_language(body) or _detect_non_latin(body)
        if language:
            return _result(out, "non_english", f"text_{language}")
        return _result(out, "unparsed", "text_unreadable")
    if _is_placeholder_title(record.get("title") or ""):
        return _result(out, "ok", "no_db_title")
    # 撤稿声明/勘误只有 1-2 页，标题常写成 "Erratum: ..."，不能按标题不匹配判错。
    if _is_short_notice(record) and chars < SHORT_MAX_CHARS:
        return _result(out, "ok", "short_notice")

    coverage = title_coverage(record.get("title") or "", body)
    out["doi_in_pdf"] = _first_doi(body)
    if coverage is None:
        return _result(out, "ok", "no_db_title")

    out["title_cov"] = round(coverage, 2)
    if coverage < TITLE_WEAK_MIN:
        ngram = ngram_coverage(record.get("title") or "", body)
        if ngram is not None:
            out["ngram_cov"] = round(ngram, 2)
        if ngram is not None and ngram >= 0.5:
            return _result(out, "ok", "ngram_match")
        if _is_front_page(body):
            return _result(out, "front_page", "publisher_front_page")
        if _is_supplement(body):
            return _result(out, "supplement", "supplement_material")
        language = _detect_foreign_language(body)
        if language:
            return _result(out, "non_english", f"text_{language}")
        return _result(out, "mismatch", "title_mismatch")
    if chars < SHORT_MAX_CHARS:
        return _result(out, "too_short", "short_content")
    if coverage < TITLE_MATCH_MIN:
        return _result(out, "weak", "title_partial")
    return _result(out, "ok", "")


def inspect_fulltext(path: Path, record: dict | None = None) -> dict:
    """体检单篇全文：文件结构 + 内容完整 + 是否对应这个 PMID。

    返回 INTEGRITY_FIELDS 各列；`verdict` 取值见 _SUGGESTED_ACTION。
    只读，不改文件。
    """
    path = Path(path)
    record = record or {}
    out = {field: "" for field in INTEGRITY_FIELDS}
    out.update(pmid=path.stem, file=path.name, suffix=path.suffix.lower())

    size = path.stat().st_size
    out["size_kb"] = round(size / 1024, 1)

    pages = 0
    try:
        if path.suffix.lower() == ".pdf":
            if size < 1024:
                return _result(out, "broken", "too_small")
            with path.open("rb") as fh:
                if not fh.read(5).startswith(b"%PDF"):
                    return _result(out, "broken", "not_pdf")
            pages, body, has_scan = _read_pdf_pages(path)
        else:
            body = _read_text(path)
            has_scan = False
    except Exception as exc:  # noqa: BLE001 - 单篇异常不能终止整批体检
        # 这三类都是解析器限制，不代表文件损坏，不能判 broken 后隔离。
        name = type(exc).__name__
        detail = str(exc)
        if name == "LimitReachedError":
            return _result(out, "unparsed", "content_stream_too_large")
        if name == "DependencyError":
            return _result(out, "unparsed", "missing_dependency")
        if "More than one /FontFile" in detail:
            return _result(out, "unparsed", "font_dict_ambiguous")
        return _result(out, "broken", f"parse_error: {name}")

    out["pages"] = pages
    out.update(judge_fulltext(body, record, pages, has_scan))
    # 出版社封面常占前几页，正文在后面；老 PDF 还可能整本页序倒置，
    # 所以这里用全文复验，不能只扩到前 12 页（实测 2053837 标题在第 13 页之后）。
    if out["verdict"] == "front_page" and path.suffix.lower() == ".pdf":
        _, body_all, _ = _read_pdf_pages(path, 10_000)
        ngram = ngram_coverage(record.get("title") or "", body_all)
        if ngram is not None and ngram >= 0.5:
            out["ngram_cov"] = round(ngram, 2)
            return _result(out, "ok", "front_page_then_fulltext")
    return out
