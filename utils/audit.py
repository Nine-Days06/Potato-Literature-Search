"""全文库变更审计流水：每条文件操作追加一行 JSON。

只回答一个问题——某个 PMID 的全文，谁在什么时候删了、换了、导入了、发给了接收方。
明细仍以各脚本自己的 CSV 为准，这里只留时间线。
"""

import hashlib
import json
from datetime import datetime
from pathlib import Path

from config.settings import OUTPUT_DIR


def audit_path() -> Path:
    return Path(OUTPUT_DIR) / "logs" / "fulltext_audit.jsonl"


def md5_file(path: Path) -> str:
    digest = hashlib.md5()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def record(op: str, pmid: str, path: Path | None = None, **fields) -> None:
    """追加一条审计记录。path 存在时补 file/size_kb/md5；写失败只告警不抛。"""
    row = {"ts": datetime.now().isoformat(timespec="seconds"), "op": op, "pmid": str(pmid)}
    if path is not None:
        path = Path(path)
        if path.exists():
            row["file"] = path.name
            row.setdefault("size_kb", round(path.stat().st_size / 1024, 1))
            row.setdefault("md5", md5_file(path))
    row.update({k: v for k, v in fields.items() if v is not None})

    target = audit_path()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError as exc:
        print(f"[audit] 写审计流水失败：{exc}")
