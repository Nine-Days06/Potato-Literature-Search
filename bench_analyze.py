# bench_analyze.py
"""
提示词基准测试分析器（支持多模型横向对比）。

统计口径说明
------------
基准集里正例来自历史人工复核 Y（可信），负例来自历史人工复核 N。
但历史 N 是在**旧的“五方向限定”标准**下打的，其中一部分在新标准
（不限研究方向、病原互作也算）下应为 RELEVANT。

因此本脚本给出两套指标：

  严格口径（strict）：把基准集标签当金标准。仅作参考——负例里混有
                      按新标准应改判的样本，会把好提示词算低。

  分层口径（stratum）：只统计不受标准变更影响的层。
                      - pos 层（人工 Y）：模型判 RELEVANT 的比例（召回）
                      - sweet_potato_mention / method 层：这些层多数
                        在旧新标准下都应为 NOT_RELEVANT，误收率可参考

最终选型主要看：pos 召回（不能掉）+ 明显该排除的层（sweet_potato、
method、few_potato_mentions）的误收率（越低越好）。

**配对口径**：各版本样本数不等（限流导致失败数不同），直接比准确率等于在
不同难度的子集上比较。因此另给一张「配对表」——只统计该模型下所有版本
都有结果的样本，选型以配对表为准。

用法
----
  python bench_analyze.py              # 全部模型
  python bench_analyze.py zhipu        # 只统计文件名含 zhipu 的
  python bench_analyze.py --diff       # 额外打印跨模型判定分歧
"""

import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

BENCH = Path("bench_pool.json")
OUT = Path("bench_out")

# 早期结果文件没有 model_slug 字段（当时只跑过一个模型）
LEGACY_SLUG = "zhipu(glm-4-Flash-250414)"

# 干净金标集：pos 层（人工 Y）+ sweet_potato_mention 层（明确该排除）
CLEAN_STRATA = {"pos": "RELEVANT", "sweet_potato_mention": "NOT_RELEVANT"}


def norm(v) -> str:
    """verdict 归一化，与生产 _normalize_verdict 同义"""
    if not v:
        return "?"
    s = str(v).strip().upper().replace(" ", "").replace("-", "").replace("_", "")
    if s == "RELEVANT":
        return "RELEVANT"
    if s in ("NOTRELEVANT", "IRRELEVANT"):
        return "NOT_RELEVANT"
    return "?"


def load_variants(filter_slug: str | None = None):
    """返回 {(model_slug, variant): payload}"""
    data = {}
    for f in sorted(OUT.glob("bench_results_*.json")):
        d = json.loads(f.read_text(encoding="utf-8"))
        slug = d.get("model_slug") or LEGACY_SLUG
        if filter_slug and filter_slug not in slug:
            continue
        data[(slug, d["variant"])] = d
    return data


def score(results: dict, meta: dict, strata: set | None = None):
    """在指定层上算混淆矩阵。strata=None 表示全样本。"""
    tp = fp = tn = fn = 0
    for pmid, x in meta.items():
        if strata and x["stratum"] not in strata:
            continue
        r = results.get(pmid)
        if not r:
            continue
        pred = norm(r.get("verdict"))
        if pred == "?":
            continue
        gold = "RELEVANT" if x["old_label"] == "Y" else "NOT_RELEVANT"
        if gold == "RELEVANT":
            if pred == "RELEVANT":
                tp += 1
            else:
                fn += 1
        else:
            if pred == "NOT_RELEVANT":
                tn += 1
            else:
                fp += 1
    n = tp + fp + tn + fn
    return {
        "n": n, "tp": tp, "fn": fn, "fp": fp, "tn": tn,
        "acc": (tp + tn) / n if n else 0,
        "rec": tp / (tp + fn) if (tp + fn) else 0,
        "spec": tn / (tn + fp) if (tn + fp) else 0,
        "prec": tp / (tp + fp) if (tp + fp) else 0,
    }


def paired_pmids(variants: dict, slug: str, names: list, meta: dict,
                 strata: set | None = None) -> list:
    """该模型下所有版本都有结果的样本 pmid。

    限流会让不同版本的失败样本数不同（glm 上 276–291 不等），直接比准确率
    等于在不同难度的子集上比较。配对口径只取交集，才是公平对比。
    """
    cand = [p for p, x in meta.items() if not strata or x["stratum"] in strata]
    return [p for p in cand
            if all(p in variants.get((slug, n), {}).get("results", {}) for n in names)]


def score_subset(results: dict, meta: dict, pmids: list):
    """在给定 pmid 子集上算混淆矩阵（口径与 score 一致）"""
    sub = {p: meta[p] for p in pmids}
    return score(results, sub)


def print_paired(variants: dict, slugs: list, meta: dict, clean_strata: set):
    print("\n" + "=" * 108)
    print("配对口径：只统计「该模型下所有版本都有结果」的样本（消除限流造成的样本集差异）")
    print("-" * 108)
    acc = {}
    for slug in slugs:
        names = sorted({v for m, v in variants if m == slug})
        pm = paired_pmids(variants, slug, names, meta, clean_strata)
        print(f"\n【{slug}】金标准集配对 {len(pm)} 篇")
        print(f"{'版本':<22}{'准确率':>9}{'召回':>8}{'特异度':>9}{'错样本':>8}")
        print("-" * 74)
        for n in names:
            s = score_subset(variants[(slug, n)]["results"], meta, pm)
            wrong = [p for p in pm
                     if norm(variants[(slug, n)]["results"][p].get("verdict"))
                     != CLEAN_STRATA[meta[p]["stratum"]]]
            acc.setdefault(n, {})[slug] = s["acc"]
            print(f"{n:<22}{s['acc']:>9.4f}{s['rec']:>8.3f}{s['spec']:>9.3f}{len(wrong):>8}"
                  f"  {','.join(wrong) if wrong else '-'}")
    if len(slugs) > 1:
        print("\n跨模型汇总（配对口径）")
        print(f"{'版本':<22}" + "".join(f"{s[:20]:>22}" for s in slugs) + f"{'平均':>9}{'最差':>9}")
        print("-" * (22 + 22 * len(slugs) + 18))
        for n in sorted(acc):
            vals = [acc[n].get(s) for s in slugs]
            vals = [v for v in vals if v is not None]
            line = f"{n:<22}" + "".join(f"{acc[n].get(s, float('nan')):>22.4f}" for s in slugs)
            print(line + f"{sum(vals)/len(vals):>9.4f}{min(vals):>9.4f}")
    print("=" * 108)


def print_table(title: str, rows: list, show_clean: bool):
    print("\n" + "=" * 118)
    print(title)
    print("-" * 118)
    head = (f"{'版本':<22}{'样本':>6}{'准确率':>9}{'召回':>8}{'特异度':>9}{'精确率':>9}"
            f"{'TP':>6}{'FN':>5}{'FP':>6}{'TN':>6}{'投票一致':>10}")
    if show_clean:
        head += f"{'金标准准确':>12}{'金标准召回':>12}{'金标准特异':>12}"
    print(head)
    print("-" * 118)
    for r in rows:
        unan = r.get("unanimous")
        line = (f"{r['label']:<22}{r['full']['n']:>6}{r['full']['acc']:>9.3f}"
                f"{r['full']['rec']:>8.3f}{r['full']['spec']:>9.3f}{r['full']['prec']:>9.3f}"
                f"{r['full']['tp']:>6}{r['full']['fn']:>5}{r['full']['fp']:>6}{r['full']['tn']:>6}"
                f"{(f'{unan:.1%}' if unan is not None else '-'):>10}")
        if show_clean:
            c = r["clean"]
            line += f"{c['acc']:>12.3f}{c['rec']:>12.3f}{c['spec']:>12.3f}"
        print(line)
    print("=" * 118)


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    show_diff = "--diff" in sys.argv
    filter_slug = args[0] if args else None

    bench = json.loads(BENCH.read_text(encoding="utf-8"))
    meta = {x["pmid"]: x for x in bench}
    variants = load_variants(filter_slug)

    if not variants:
        print("没有找到匹配的结果文件。")
        return

    slugs = sorted({s for s, _ in variants})
    print(f"基准集 {len(bench)} 篇；模型 {len(slugs)} 个；结果文件 {len(variants)} 份")
    for s in slugs:
        vs = [v for m, v in variants if m == s]
        print(f"  · {s}: {len(vs)} 个版本 ({', '.join(vs)})")

    clean_strata = set(CLEAN_STRATA)

    for slug in slugs:
        rows = []
        for (m, name), d in sorted(variants.items()):
            if m != slug:
                continue
            # 多次运行投票的结果文件带 repeat 字段；此时结果里每个 pmid 有 _unanimous
            results = d["results"]
            unanimous = None
            if d.get("repeat", 1) > 1 and results:
                unanimous = sum(1 for r in results.values() if r.get("_unanimous")) / len(results)
            rows.append({
                "label": name,
                "full": score(results, meta),
                "clean": score(results, meta, clean_strata),
                "unanimous": unanimous,
            })
        rep = next((d.get("repeat", 1) for (m, _), d in variants.items() if m == slug), 1)
        suffix = f"，每版本 {rep} 次运行多数票合并" if rep > 1 else "（单次运行）"
        print_table(f"【{slug}】全 300 篇（严格口径：历史 N 当金标准，仅参考）{suffix}", rows, True)
        print("注：'金标准'三列只统计 pos 层(150 篇人工Y) + sweet_potato_mention 层(30 篇明确该排除)，"
              "不受标准变更污染。")

        # 各层误收率
        strata = ["pos", "sweet_potato_mention", "method", "few_potato_mentions",
                  "pathogen", "review", "short_abstract", "fill"]
        strata = [s for s in strata if any(x["stratum"] == s for x in bench)]
        header = f"{'版本':<22}" + "".join(f"{s[:13]:>15}" for s in strata)
        print("\n各层被判 RELEVANT 的比例（pos 越高越好；其余越低越好，pathogen 除外）")
        print(header)
        print("-" * len(header))
        for (m, name), d in sorted(variants.items()):
            if m != slug:
                continue
            line = f"{name:<22}"
            for s in strata:
                st = score(d["results"], meta, {s})
                if not st["n"]:
                    line += f"{'-':>15}"
                    continue
                rate = (st["tp"] + st["fp"]) / st["n"]
                line += f"{st['tp'] + st['fp']}/{st['n']}={rate:.2f}".rjust(15)
            print(line)

        # 方向分类分布
        print("\n研究方向输出分布（仅 RELEVANT，看是否退化成单一类）")
        for (m, name), d in sorted(variants.items()):
            if m != slug:
                continue
            c = Counter()
            for pmid, r in d["results"].items():
                if norm(r.get("verdict")) == "RELEVANT":
                    c[r.get("research_field") or "<空>"] += 1
            print(f"  {name:<22} " + ", ".join(f"{k}:{v}" for k, v in c.most_common(6)))

    # ── 配对口径（选型主依据）──
    print_paired(variants, slugs, meta, clean_strata)

    # ── 跨模型对比（同名版本）──
    if len(slugs) > 1:
        common = set.intersection(*[{v for m, v in variants if m == s} for s in slugs])
        if common:
            print("\n" + "=" * 104)
            print(f"跨模型对比（仅 {len(common)} 个共同版本）")
            print("-" * 104)
            head = f"{'版本':<22}" + "".join(f"{s[:24]:>26}" for s in slugs)
            print(head)
            print("-" * len(head))
            for name in sorted(common):
                line = f"{name:<22}"
                for s in slugs:
                    d = variants[(s, name)]
                    f = score(d["results"], meta)
                    c = score(d["results"], meta, clean_strata)
                    line += f"{f['acc']:.3f}/{c['acc']:.3f}".rjust(26)
                print(line)
            print("单元格 = 全样本准确率 / 金标准准确率")
            print("=" * 104)

    # ── 跨模型判定分歧 ──
    if show_diff and len(slugs) >= 2:
        a, b = slugs[0], slugs[-1]
        print(f"\n{'=' * 104}\n跨模型判定分歧：{a}  vs  {b}\n{'-' * 104}")
        for name in sorted({v for m, v in variants if m == a} & {v for m, v in variants if m == b}):
            ra = variants[(a, name)]["results"]
            rb = variants[(b, name)]["results"]
            diff = [(p, norm(ra[p].get("verdict")), norm(rb[p].get("verdict")))
                    for p in ra if p in rb and norm(ra[p].get("verdict")) != norm(rb[p].get("verdict"))]
            only_b = sum(1 for _, x, y in diff if x == "NOT_RELEVANT" and y == "RELEVANT")
            only_a = sum(1 for _, x, y in diff if x == "RELEVANT" and y == "NOT_RELEVANT")
            print(f"  {name:<22} 分歧 {len(diff):>3} 篇（{a}收→{b}排 {only_a}，{a}排→{b}收 {only_b}）")


if __name__ == "__main__":
    main()
