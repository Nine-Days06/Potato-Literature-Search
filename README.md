# 马铃薯文献批量下载与清洗系统

基于 NCBI E-utilities API，批量下载 PubMed 中马铃薯相关文献（覆盖胁迫、发育、基因调控、多组学、功能遗传学等方向），
经过解析、去重、质量过滤、关键词评分、LLM 二次验证，最终产出结构化数据集，
用于知识图谱构建或智能体效果评测。

## 关于文献格式

PubMed E-utilities 下载的是 **XML 格式的元数据**（标题、摘要、关键词、MeSH词、作者等），
**不是 PDF**。这对后续分析已经足够，因为：
- 90% 的关键信息可从摘要中提取
- XML 结构化程度高，解析准确
- 不存在版权问题，可自由下载

如需全文，PMC 开放获取文章可通过 `--step pdf` 参数额外下载 PDF/TXT。

## 项目结构

```
Potato-Literature-Search/
├── config/
│   └── settings.py              # 全局配置（API Key、路径、搜索词、LLM 配置等）
├── downloader/
│   ├── pubmed_downloader.py     # NCBI E-utilities 批量下载 XML
│   └── pdf_downloader.py        # PMC OA 全文下载（PDF/TGZ），含重试机制
├── parser/
│   └── xml_parser.py            # XML 解析 → SQLite
├── cleaner/
│   ├── hard_filter.py           # 硬过滤（语言、年份、文章类型、去重等）
│   ├── relevance_scorer.py      # 关键词评分（基因/功能/性状三分类）
│   └── llm_validator.py         # LLM 二次验证（DeepSeek，输出待复核 CSV）
├── utils/
│   ├── logger.py                # 统一日志
│   └── db.py                    # 数据库工具函数（含四张表）
├── data/
│   ├── raw_xml/                 # 原始 XML 文件
│   ├── processed/               # SQLite 数据库（potato_lit.db）
│   ├── output/                  # CSV 输出
│   └── pdfs/                    # 按相关性分级的 PDF/TXT 全文
│       ├── high/
│       ├── mid/
│       └── low/
├── main.py                      # 一键运行入口
└── requirements.txt
```

## 快速开始

```bash
# 1. 安装依赖
pip install -r requirements.txt

# 2. 配置 API Key
#    复制 config/.env.example 为 config/.env，填入你的密钥
#    .env 已加入 .gitignore，不会误提交

# 3. 运行核心流程（下载 → 解析 → 清洗 → 导出 CSV）
python main.py

# PDF 下载、LLM 验证、导入复核等进阶步骤需单独运行（见下文）

# 4. LLM 二次验证（需在 .env 中配置 LLM_API_KEY 或 ZHIPU_API_KEY）
python main.py --step validate

# 5. 导入人工复核结果（标注 Y/N 后）
python main.py --step import-review

# 6. 仅运行某个阶段
python main.py --step download   # 下载 XML
python main.py --step parse      # 解析 XML → SQLite
python main.py --step clean      # 硬过滤 + 评分 + 导出 CSV
python main.py --step export     # 仅重新导出 CSV
python main.py --step pdf        # 下载 OA 全文 PDF/TXT
python main.py --step validate   # LLM 验证
python main.py --step import-review  # 导入人工复核

# 7. 自定义搜索词
python main.py --query "potato AND drought AND gene"
```

## 输出文件

| 文件 | 说明 |
|------|------|
| `data/processed/potato_lit.db` | 全量结构化文献库（articles + filter_log + relevance_scores + llm_validation 四张表） |
| `data/output/{high,mid,low}_relevance.csv` | 关键词评分三级分类输出 |
| `data/output/clean_report.txt` | 清洗报告（各阶段数量统计） |
| `data/output/llm_review_pending_*.csv` | LLM 验证待人工复核清单（标注 Y/N） |
| `data/output/llm_filtered_*.csv` | LLM + 人工复核后的最终过滤结果 |
| `data/output/failed_downloads_*.csv` | PDF 下载失败链接清单 |
| `data/output/oa_download_links_*.csv` | OA 资源下载链接清单 |
| `data/pdfs/{high,mid,low}/` | 按相关性分级的 PDF/TXT 全文文件 |
