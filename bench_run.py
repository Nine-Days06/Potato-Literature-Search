# bench_run.py
"""
提示词基准测试执行器。

对每套候选提示词，在 bench_pool.json 的 300 篇文献上各跑一遍，
把模型原始判定落盘为 bench_results_<VERSION>.json，供 analyze 阶段统计。

只调用 LLM，不写生产数据库。
"""

import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from config.settings import LLM_BATCH_SIZE, LLM_CONCURRENCY
from bench_prompts import PROMPT_VARIANTS
from cleaner.llm_validator import _build_client, _extract_json


def _parse_content(content: str, fix: bool):
    """解析模型返回。兼容三种形态：裸数组 / {"results": [...]} / ```json 包裹的二者。"""
    text = content.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1]
        text = text.rsplit("```", 1)[0] if "```" in text else text
        text = text.strip()
    try:
        data = json.loads(text)
        if isinstance(data, list):
            return data
        if isinstance(data, dict) and isinstance(data.get("results"), list):
            return data["results"]
        if isinstance(data, dict) and data.get("pmid"):
            return [data]
    except (json.JSONDecodeError, ValueError):
        pass
    # 兜底：走生产的数组提取器
    return _extract_json(content, fix_glm_multi_array=fix)


def call_with_prompt(prompt: str, rows: list) -> tuple[list[dict], list[str]]:
    """用指定 system prompt 调用一次 LLM（与生产的 _call_llm 同逻辑，但可换提示词）"""
    parts = ["以下是需要你根据上述标准判断的文献列表，请逐条判断其核心研究对象是否为马铃薯：\n\n"]
    for i, row in enumerate(rows, 1):
        parts.append(
            f"## 文献 {i}\nPMID: {row['pmid']}\nTitle: {(row['title'] or '').strip()}\n"
            f"Abstract: {(row['abstract'] or '').strip()}\n"
        )
    user_msg = "\n".join(parts)

    client, model, create_kwargs, fix = _build_client()
    if client is None:
        return [], [r["pmid"] for r in rows]

    failed = [r["pmid"] for r in rows]
    for attempt in range(1, 4):
        try:
            resp = client.chat.completions.create(
                messages=[
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": user_msg},
                ],
                **create_kwargs,
            )
            content = resp.choices[0].message.content.strip()
            result = _parse_content(content, fix)
            if not result:
                raise ValueError(f"JSON 解析失败: {content[:150]}")
            return result, []
        except Exception as e:
            print(f"    重试 {attempt}/3: {e}")
            if attempt < 3:
                time.sleep(2 ** attempt)
    return [], failed


def _model_slug() -> str:
    """把当前 provider/model 变成文件名安全的短标签，避免不同模型的结果互相覆盖"""
    from config.settings import LLM_PROVIDER, LLM_PROVIDER_CONFIGS
    model = LLM_PROVIDER_CONFIGS[LLM_PROVIDER]["model"]
    slug = re.sub(r"[^0-9A-Za-z]+", "-", f"{LLM_PROVIDER}-{model}").strip("-")
    return slug


def _verdict_key(result: dict | None) -> str:
    """把 verdict 归一成 REL / NOT / ? 三个投票键"""
    s = str((result or {}).get("verdict", "")).upper()
    s = s.replace("_", "").replace("-", "").strip()
    if s == "RELEVANT":
        return "REL"
    if s in ("NOTRELEVANT", "IRRELEVANT"):
        return "NOT"
    return "?"


def _merge_runs(runs: list[dict], valid_pmids: set[str]) -> dict:
    """
    多次运行按多数票合并。

    为什么必须这样做：即使 temperature=0，同一提示词连续两次运行的判定
    翻转率实测 2.4–2.7%，三票后仍约 3% 不稳定。单次运行会把这种随机噪声
    当成提示词效果，导致版本之间 1–2 个百分点的差异完全不可信。

    模型偶尔会幻觉出不存在的 pmid，这里按基准集白名单丢弃。
    """
    from collections import Counter

    merged: dict[str, dict] = {}
    for pmid in valid_pmids:
        votes = Counter()
        fields = Counter()
        reasons: dict[str, str] = {}
        for run in runs:
            r = run.get(pmid)
            if not r:
                continue
            key = _verdict_key(r)
            votes[key] += 1
            if key == "REL":
                f = str(r.get("research_field") or "").strip()
                if f:
                    fields[f] += 1
            reasons.setdefault(key, str(r.get("reason") or ""))
        if not votes:
            continue
        top = votes.most_common(1)[0][0]
        merged[pmid] = {
            "verdict": {"REL": "RELEVANT", "NOT": "NOT_RELEVANT"}.get(top, ""),
            "reason": reasons.get(top, ""),
            "research_field": fields.most_common(1)[0][0] if fields else "",
            "_votes": dict(votes),
            "_unanimous": len(votes) == 1,
        }
    return merged


def run_once(prompt: str, bench: list, label: str) -> tuple[dict, list[str], float]:
    """跑一遍完整基准集，返回 (pmid->结果, 失败 pmid 列表, 耗时秒)"""
    batches = [bench[i:i + LLM_BATCH_SIZE] for i in range(0, len(bench), LLM_BATCH_SIZE)]
    print(f"  [{label}] {len(bench)} 篇 / {len(batches)} 批（每批 {LLM_BATCH_SIZE}）...")

    collected: dict[str, dict] = {}
    failed: list[str] = []
    done = 0
    t0 = time.time()

    with ThreadPoolExecutor(max_workers=LLM_CONCURRENCY) as ex:
        futs = {ex.submit(call_with_prompt, prompt, b): b for b in batches}
        for fut in as_completed(futs):
            batch = futs[fut]
            try:
                results, fails = fut.result()
            except Exception as e:
                print(f"    批次异常: {e}")
                failed.extend(r["pmid"] for r in batch)
                continue
            failed.extend(fails)
            for r in results:
                if isinstance(r, dict) and r.get("pmid"):
                    collected[str(r["pmid"])] = r
            done += 1
            if done % 10 == 0 or done == len(batches):
                print(f"    {done}/{len(batches)} 批  已收集 {len(collected)} 篇  "
                      f"{time.time() - t0:.0f}s")
    return collected, failed, round(time.time() - t0, 1)


def run_variant(name: str, prompt: str, bench: list, out_dir: Path,
                repeat: int = 3) -> dict:
    """
    跑一个提示词版本。repeat > 1 时跑多遍再按多数票合并，
    合并结果写入主结果文件，各次原始结果写入 _run{i}_ 前缀文件留证。
    """
    slug = _model_slug()
    out_file = out_dir / f"bench_results_{name}__{slug}.json"
    if out_file.exists():
        print(f"[{name}] 已存在，跳过（删除可重跑）")
        return json.loads(out_file.read_text(encoding="utf-8"))

    valid = {str(x["pmid"]) for x in bench}
    runs = []
    elapsed = 0.0
    for i in range(1, repeat + 1):
        cache = out_dir / f"_run{i}_{name}__{slug}.json"
        # 兼容早期命名（无 model_slug 后缀，当时只跑过一个模型）
        legacy = out_dir / f"_run{i}_{name.split('_')[0]}.json"
        if not cache.exists() and legacy.exists():
            cache = legacy
        if cache.exists():
            cached = json.loads(cache.read_text(encoding="utf-8"))
            # 早期文件靠内容里的 model_slug 兜底校验，避免换模型后误用旧缓存
            if cached.get("model_slug") not in (None, slug):
                print(f"  [{name} 第 {i}/{repeat} 遍] 缓存属于 {cached['model_slug']}，"
                      f"与当前 {slug} 不符，重跑")
                cache = None
            else:
                print(f"  [{name} 第 {i}/{repeat} 遍] 已有缓存，复用")
                runs.append(cached["results"])
                continue
        if cache is None:
            cache = out_dir / f"_run{i}_{name}__{slug}.json"
        got, failed, sec = run_once(prompt, bench, f"{name} 第 {i}/{repeat} 遍")
        elapsed += sec
        cache.write_text(json.dumps(
            {"variant": name, "model_slug": slug, "run_index": i,
             "results": got, "failed": failed, "elapsed_sec": sec},
            ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"  [{name} 第 {i}/{repeat} 遍] 收集 {len(got)} 篇 / 失败 {len(failed)} 篇 / {sec}s")
        runs.append(got)

    merged = _merge_runs(runs, valid)
    unstable = sum(1 for x in merged.values() if not x["_unanimous"])
    payload = {"variant": name, "prompt_chars": len(prompt), "model_slug": slug,
               "repeat": repeat, "unanimous_rate": round(1 - unstable / len(merged), 3)
               if merged else 0.0,
               "results": merged, "elapsed_sec": round(elapsed, 1)}
    out_file.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[{name}] 完成: {len(merged)} 篇（{repeat} 遍投票，"
          f"三票一致 {payload['unanimous_rate']:.1%}）-> {out_file.name}")
    return payload


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    repeat = 3
    for a in sys.argv[1:]:
        if a.startswith("--repeat="):
            repeat = int(a.split("=", 1)[1])

    bench = json.loads(Path("bench_pool.json").read_text(encoding="utf-8"))
    out_dir = Path("bench_out")
    out_dir.mkdir(exist_ok=True)

    which = args or list(PROMPT_VARIANTS)
    print(f"模型: {_model_slug()} ｜ 版本: {', '.join(which)} ｜ 每版 {repeat} 遍投票")
    for name in which:
        if name not in PROMPT_VARIANTS:
            print(f"未知版本: {name}")
            continue
        run_variant(name, PROMPT_VARIANTS[name], bench, out_dir, repeat=repeat)


if __name__ == "__main__":
    main()
