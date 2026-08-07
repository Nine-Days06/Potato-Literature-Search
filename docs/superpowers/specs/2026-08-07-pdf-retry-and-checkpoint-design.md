# PDF 下载失败重试与断点续传 — 设计文档

日期：2026-08-07

## 目标

1. 新增命令 `python main.py --step pdf-retry`：仅对之前下载失败的部分执行重试，不重新全量跑。
2. 支持程序中断后，下次运行继续上次进度（断点续传）。
3. 仅下载 PDF，不涉及 txt 回退。

## 背景与现状

- `--step pdf` 全量下载：读库中符合条件（human_review='Y' 或 LLM RELEVANT）且有 PMC ID 的文献 → OA API 拉链接 → 并发下载 → 失败的导出 `failed_downloads_*.csv`（pdf_downloader.py:336）。
- 失败有两类：
  - 链接缺失：OA API 查询失败 / 无 record / ID 不存在，未进入 oa_links，`failed_downloads_*.csv` 中无该行。
  - 下载失败：有链接但 aria2c / tgz 提取失败，出现在 CSV 中。
- 现有下载对已存在 `{pmid}.pdf` 已跳过（文件级断点续传，pdf_downloader.py:684）。
- `llm_validator.py` 已有 JSON checkpoint 模式（`llm_validation_progress.json`），本项目沿用该模式。

## 方案：JSON checkpoint + CSV 回退

### 1. 新命令

`main.py` 的 `--step` choices 增加 `pdf-retry`，映射到新函数 `step_pdf_retry()` → `run_pdf_retry()`。

### 2. checkpoint 文件

路径：`<OUTPUT_DIR>/pdf_download_progress.json`

结构：

```json
{
  "pending": [
    {"pmid": "123", "pmc_id": "PMC456", "links": {"pdf": "...", "tgz": "..."}},
    {"pmid": "124", "pmc_id": "PMC457", "links": {}}
  ],
  "updated_at": "2026-08-07T12:00:00+08:00"
}
```

- `pending`：待重试条目。`links` 为空 dict 表示当时链接未查到（需重查 OA API）。
- 保存时机：每成功下载 10 篇，或每轮重试结束后。
- 加载：`run_pdf_retry()` 启动时读。
- 清除：全部成功完成后删除；仍有失败则保留，供下一次 `pdf-retry` 继续。

### 3. 辅助函数

在 `downloader/pdf_downloader.py` 中新增：

- `_pdf_checkpoint_path() -> Path`：返回 checkpoint 文件路径。
- `_save_pdf_checkpoint(pending: list[dict])`：写 JSON。
- `_load_pdf_checkpoint() -> list[dict]`：读取 pending；文件缺失/损坏返回 `[]` 并告警。
- `_clear_pdf_checkpoint()`：删除文件。
- `load_failed_items_from_csv(out_dir: Path = OUTPUT_DIR) -> list[dict]`：读最新 `failed_downloads_*.csv`，返回与 checkpoint 同结构的条目列表。

### 4. 重试主流程 `run_pdf_retry()`

1. 加载待重试清单：
   - 有 checkpoint → `_load_pdf_checkpoint()`
   - 无 checkpoint → `load_failed_items_from_csv()`
   - 两者皆空 → 日志提示无待重试项，退出
2. 文件级去重：跳过已存在 `{pmid}.pdf` 的条目。
3. 混合取链：
   - 条目 `links` 含 pdf/tgz URL → 直接下载（复用 `download_oa_pdf`）。
   - `links` 为空或查不到 → 重新调用 OA API 查询（`fetch_oa_links` 或单条查询），再下载。
4. 并发下载：复用 `ThreadPoolExecutor(max_workers=DOWNLOAD_MAX_WORKERS)`。
5. 定期写 checkpoint（成功 10 篇后 / 每轮后）。
6. 结束：
   - 全部成功 → `_clear_pdf_checkpoint()`。
   - 仍有失败 → 保留 checkpoint + 导出新 `failed_downloads_*.csv`。

### 5. `--pdf` 全量逻辑不变

- `run_pdf_download()` 逻辑保持现状。
- 仅在原失败导出处同时调用 `_save_pdf_checkpoint(剩余失败项)`，使中断后可无缝转 `--step pdf-retry`。

### 6. 测试

- checkpoint 保存/加载/清除（含损坏文件回退）。
- `load_failed_items_from_csv` 解析失败 CSV。
- `retry` 主流程：mock 下载与 OA 查询，验证「有链接直下、缺链接重查、完成即清除、仍有失败则保留」。

## 非目标

- 不修改 `--step pdf` 的全量拉取与下载逻辑（除失败后追加写 checkpoint）。
- 不引入 SQLite 状态表。
- 不做 PDF 之外的 txt/text 下载。