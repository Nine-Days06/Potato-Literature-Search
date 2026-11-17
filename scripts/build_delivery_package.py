"""生成给接收方的清理包：删除/替换清单 + 替换用的新文件 + 执行脚本 + 用法说明。"""

import csv
import hashlib
import shutil
import sys
from pathlib import Path

sys.path[:0] = [str(Path(__file__).resolve().parents[1]), str(Path(__file__).resolve().parent)]

from config.settings import OUTPUT_DIR, PDF_DIR
from downloader.fulltext_library import logger

HANDOFF = Path(OUTPUT_DIR) / "handoff"
DEST = HANDOFF / "delivery"
PENDING = HANDOFF / "pending_recipient_cleanup.csv"

APPLY_PY = '''"""按 manifest.csv 清理全文库：delete 移到 _deleted/，replace 用 replace/ 里的新文件覆盖。用法：python apply_cleanup.py <全文库目录> [--dry-run]"""
import csv, hashlib, shutil, sys
from pathlib import Path

here = Path(__file__).resolve().parent
args = [a for a in sys.argv[1:] if not a.startswith("--")]
root = Path(args[0] if args else ".")
dry = "--dry-run" in sys.argv
rows = list(csv.DictReader(open(here / "manifest.csv", encoding="utf-8-sig")))

def md5(path):
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()

done = 0
def targets(pmid, filename):
    path = root / filename
    if path.exists():
        return [path]
    return [p for p in sorted(root.glob(pmid + ".*")) if p.suffix in (".pdf", ".txt", ".xml")]

for row in rows:
    if row["action"] == "delete":
        old = targets(row["pmid"], row["filename"])
        if not old:
            print(f"已不存在 {row['filename']}")
            continue
        if dry:
            print(f"将删除 {' '.join(p.name for p in old)}")
            continue
        (root / "_deleted").mkdir(exist_ok=True)
        for path in old:
            shutil.move(str(path), str(root / "_deleted" / path.name))
            done += 1
        continue
    old = root / row["filename"]
    new = here / "replace" / row["filename"]
    if not new.exists():
        print(f"缺新文件 {row['filename']}")
        continue
    if old.exists() and md5(old) == row["md5"]:
        print(f"已是新版 {row['filename']}")
        continue
    if dry:
        print(f"将替换 {row['filename']}")
        continue
    (root / "_replaced_backup").mkdir(exist_ok=True)
    if old.exists():
        shutil.copy2(old, root / "_replaced_backup" / old.name)
    shutil.copy2(new, old)
    done += 1

if dry:
    print(f"以上为待处理项，去掉 --dry-run 再跑一次即执行")
else:
    left = [r["pmid"] for r in rows
            if r["action"] == "delete" and targets(r["pmid"], r["filename"])]
    stale = [r["pmid"] for r in rows
             if r["action"] == "replace"
             and (not (root / r["filename"]).exists() or md5(root / r["filename"]) != r["md5"])]
    print(f"已处理 {done}/{len(rows)}")
    print(f"该删未删 {len(left)} 篇" + (f": {' '.join(left)}" if left else ""))
    print(f"替换不符 {len(stale)} 篇" + (f": {' '.join(stale)}" if stale else ""))
'''

README = r"""# 全文替换包

53 篇删除 + 15 篇替换。替换件均已校验为完整正文，旧版不可用或内容不符。

## 用法

在同一台机器上打开本目录，执行：

```powershell
python apply_cleanup.py <全文库目录>
```

先看会动哪些文件（不实际修改）：

```powershell
python apply_cleanup.py <全文库目录> --dry-run
```

例如全文库在 `D:\project\pdfs`：

```powershell
python apply_cleanup.py D:\project\pdfs --dry-run
python apply_cleanup.py D:\project\pdfs
```

删除的文件移到 `<全文库目录>/_deleted/`，被替换的旧文件备份到 `_replaced_backup/`。
脚本结束会核对：删除项是否已不存在、替换项 md5 是否一致，两项都是 0 篇即完成。

需要 Python 3.8+，无第三方依赖。
"""


def md5(path: Path) -> str:
    h = hashlib.md5()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def merged_rows() -> list[dict]:
    rows = list(csv.DictReader(open(HANDOFF / "recipient_cleanup.csv", encoding="utf-8-sig")))
    if not PENDING.exists():
        return rows
    by_pmid = {row["pmid"]: row for row in rows}
    for row in csv.DictReader(open(PENDING, encoding="utf-8-sig")):
        by_pmid[row["pmid"]] = row
    return list(by_pmid.values())


def main() -> int:
    rows = merged_rows()
    if DEST.exists():
        shutil.rmtree(DEST)
    (DEST / "replace").mkdir(parents=True)

    manifest = []
    for row in rows:
        src = Path(PDF_DIR) / row["filename"]
        entry = {"pmid": row["pmid"], "action": row["action"],
                 "filename": row["filename"], "size": "", "md5": ""}
        if row["action"] == "replace":
            if not src.exists():
                logger.error(f"缺少替换文件 {src}")
                continue
            shutil.copy2(src, DEST / "replace" / row["filename"])
            entry.update(size=src.stat().st_size, md5=md5(src))
        manifest.append(entry)

    with open(DEST / "manifest.csv", "w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(manifest[0]))
        writer.writeheader()
        writer.writerows(manifest)
    with open(DEST / "apply_cleanup.py", "w", encoding="utf-8") as fh:
        fh.write(APPLY_PY)
    with open(DEST / "README.md", "w", encoding="utf-8") as fh:
        fh.write(README.replace("53 篇删除 + 15 篇替换",
                                f"{sum(m['action'] == 'delete' for m in manifest)} 篇删除 + "
                                f"{sum(m['action'] == 'replace' for m in manifest)} 篇替换"))

    logger.info(f"清理包 {len(manifest)} 篇（删 {sum(m['action'] == 'delete' for m in manifest)}，"
                f"换 {sum(m['action'] == 'replace' for m in manifest)}）-> {DEST}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
