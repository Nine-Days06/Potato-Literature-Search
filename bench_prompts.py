# bench_prompts.py
"""
提示词基准测试：多版本候选提示词定义。

设计意图
--------
删掉 few-shot 示例后，模型在“马铃薯仅顺带提及”这一边界上出现误判
（通用 DNA 提取方法验证了 45 个物种含马铃薯 → 误判 RELEVANT）。
本文件把该边界写成显式文字规则，并保留几个候选版本做 A/B 对比。

版本说明
--------
V1_BARE   : 删示例后的原始版本（对照组，即当前 cleaner/llm_validator.py 的状态）
V2_RULES  : 显式列出“顺带提及”的判定问句与反例特征
V3_CHECK  : 在 V2 基础上加“输出前自检”，要求模型先回答两个布尔问题
V4_STRICT : 在 V3 基础上把默认倾向改为“存疑即排除”，并给出材料/方法的区分规则

用法
----
    from bench_prompts import PROMPT_VARIANTS
    for name, prompt in PROMPT_VARIANTS.items(): ...
"""

from config.settings import RESEARCH_FIELDS

# ── 各版本共用的骨架片段 ─────────────────────────────────────────

_ROLE = (
    "你是一个马铃薯（Solanum tuberosum）文献数据库建设的专家。\n"
    "本项目的目标是收录所有与马铃薯相关的文献，不限研究方向：\n"
    "分子生物学、遗传育种、栽培生理、植物保护、加工储藏、品质营养、\n"
    "产后处理、产业经济、综述评论等均在收录范围内。\n\n"
)

_CORE_Q = (
    "判断标准只有一个核心问题：这篇文献是否以马铃薯（或其近缘种、\n"
    "其块茎/植株/种质资源、其病虫害互作、其加工产品）为研究对象\n"
    "或重要研究材料。\n\n"
)

_POSITIVE = (
    "判断标准：\n"
    "- 以马铃薯的任何方面为研究对象：基因功能、性状、育种、栽培、\n"
    "  病虫害、生理、加工、储藏、品质、营养、经济等 → RELEVANT\n"
    "- 以马铃薯近缘种/野生种（Solanum sect. Petota 等）为研究对象 → RELEVANT\n"
    "- 以马铃薯病原物（晚疫病菌、青枯菌、线虫、病毒等）与马铃薯的\n"
    "  互作，甚至病原物本身的研究（植保背景）→ RELEVANT\n"
    "- 以马铃薯块茎或加工产品（薯片、薯条、淀粉等）为材料的\n"
    " 食品科学研究 → RELEVANT\n"
    "- 马铃薯相关综述、方法学论文（以马铃薯为研究对象或材料）→ RELEVANT\n"
)

_NEGATIVE_BASIC = (
    "- 甘薯/红薯（sweet potato, Ipomoea batatas）为主要研究对象，\n"
    "    马铃薯仅作对比 → NOT_RELEVANT\n"
    "- 马铃薯仅在摘要中顺带提及（如列举多种作物之一），\n"
    "   研究对象与马铃薯无实质关联 → NOT_RELEVANT\n"
    "- 研究对象与马铃薯完全无关 → NOT_RELEVANT\n"
)

# V2 起的加强规则：把“顺带提及”拆成可执行的判定问句
_NEGATIVE_RULES = (
    "\n## 「顺带提及」的判定（务必严格）\n"
    "以下情形属于「马铃薯不是研究对象」，一律判 NOT_RELEVANT：\n"
    "1. 通用方法/通用材料研究：研究对象是某种通用技术、试剂盒、\n"
    "   提取流程、测序方法或通用仪器，马铃薯只是被验证的物种之一。\n"
    "   例：某 DNA 提取法在 45 种植物（含马铃薯）上验证 → NOT_RELEVANT\n"
    "2. 多物种并列筛选：马铃薯与水稻、小麦、番茄等并列出现在\n"
    "   物种列表、比较矩阵或统计样本中，没有针对马铃薯的单独结论。\n"
    "3. 马铃薯仅作对照/背景：以其他作物（尤其甘薯 Ipomoea batatas）\n"
    "   或非植物对象（真菌、细菌、动物）为主体，马铃薯只出现在\n"
    "   对比组、培养基成分（如马铃薯葡萄糖琼脂 PDA）、\n"
    "   背景介绍或参考文献里。\n"
    "4. 名称陷阱：sweet potato / 红薯 / 木薯 / 马铃薯淀粉仅作为\n"
    "   通用商品名出现时，需判断主体物种；主体是甘薯等非马铃薯\n"
    "   作物时判 NOT_RELEVANT。\n"
    "\n判定问句（必须能对第一条回答「是」才判 RELEVANT）：\n"
    "- 若把马铃薯从这篇文献里去掉，文献的核心结论是否还能成立？\n"
    "  能成立 → 马铃薯不是研究对象 → NOT_RELEVANT\n"
    "- 文献的标题或摘要主体句，是否以马铃薯（或其产品/病原互作）\n"
    "  为主语或核心实验材料？\n"
)

_CHECK_STEP = (
    "\n## 输出前自检\n"
    "逐条判定后，先在心里回答这两个问题，再决定 verdict：\n"
    "  Q1 马铃薯是否是核心研究对象或关键实验材料？（是/否）\n"
    "  Q2 马铃薯是否只出现在物种列表、对比组、通用方法或背景中？（是/否）\n"
    "Q1=否 或 Q2=是 → NOT_RELEVANT；否则 RELEVANT。\n"
    "若理由中出现了「仅作对比」「顺带提及」「验证物种之一」「通用方法」\n"
    "等措辞，则 verdict 必须是 NOT_RELEVANT，两者不得矛盾。\n"
)

_STRICT_DEFAULT = (
    "\n## 默认倾向\n"
    "证据不足时倾向排除：若摘要无法明确指出马铃薯在其中的作用，\n"
    "或马铃薯的贡献只是提供了一种通用实验材料，判 NOT_RELEVANT。\n"
    "宁可漏收一篇边缘文献，也不要让通用方法/多物种筛选类文献\n"
    "大量混入数据集。\n"
)

# V5 起的物种甄别块：直击实测中最顽固的错误——把 sweet potato 当马铃薯
_SPECIES_GUARD = (
    "\n## 物种甄别（先做这一步，再谈相关性）\n"
    "马铃薯的学名只有 Solanum tuberosum，英文只有 potato / Irish potato。\n"
    "以下物种**都不是**马铃薯，以它们为研究对象的文献一律 NOT_RELEVANT：\n"
    "  - sweet potato / sweetpotato / 甘薯 / 红薯 / 地瓜 → Ipomoea batatas（旋花科）\n"
    "  - cassava / 木薯 → Manihot esculenta\n"
    "  - tomato / 番茄 → Solanum lycopersicum\n"
    "  - 其他 Solanum 属非马铃薯物种（如 Solanum photeinocarpum 等杂草）\n"
    "注意：sweet potato 的英文里含有 potato 一词，但物种完全不同，\n"
    "绝不能因为出现 potato 就判 RELEVANT。若摘要以 sweet potato 为\n"
    "主体、只顺带对比马铃薯，判 NOT_RELEVANT。\n"
    "另注意：potato starch / potato dextrose agar 作为通用材料或\n"
    "培养基成分出现时，不代表该文献研究马铃薯。\n"
)

# V7 用的修正版：去掉「其他 Solanum 属非马铃薯物种一律排除」这句。
# 它与正例规则「近缘种/野生种（Solanum sect. Petota）算 RELEVANT」直接矛盾，
# 实测导致 Solanum 属糖苷生物碱类研究（PMID 39271954）在两模型上都被误排。
_SPECIES_GUARD_FIXED = (
    "\n## 物种甄别（先做这一步，再谈相关性）\n"
    "马铃薯的学名是 Solanum tuberosum，英文是 potato / Irish potato。\n"
    "以下物种**都不是**马铃薯，以它们为研究对象的文献一律 NOT_RELEVANT：\n"
    "  - sweet potato / sweetpotato / 甘薯 / 红薯 / 地瓜 → Ipomoea batatas（旋花科）\n"
    "  - cassava / 木薯 → Manihot esculenta\n"
    "  - tomato / 番茄 → Solanum lycopersicum\n"
    "注意：sweet potato 的英文里含有 potato 一词，但物种完全不同，\n"
    "绝不能因为出现 potato 就判 RELEVANT。若摘要以 sweet potato 为\n"
    "主体、只顺带对比马铃薯，判 NOT_RELEVANT。\n"
    "但注意区分：马铃薯的**近缘野生种**（Solanum sect. Petota 组内物种）\n"
    "属于研究范围，以它们为研究对象 → RELEVANT；番茄、甘薯等\n"
    "同科/同属但非 Petota 组的作物 → NOT_RELEVANT。\n"
    "另注意：potato starch / potato dextrose agar 作为通用材料或\n"
    "培养基成分出现时，不代表该文献研究马铃薯。\n"
)



def _research_block():
    return (
        "\n## 研究方向分类\n"
        "对判为 RELEVANT 的文献，还必须从下列枚举中选择一个最贴合的\n"
        "研究方向，写入 research_field 字段（只能选一个，不得自造标签）：\n"
        + "".join(f"  - {f}\n" for f in RESEARCH_FIELDS) +
        "判为 NOT_RELEVANT 时 research_field 填空字符串 \"\"。\n"
    )


_OUTPUT = (
    "\n请以 JSON 对象格式逐条回答，不要包含其他内容：\n"
    '{"results":[{"pmid":"...","verdict":"RELEVANT 或 NOT_RELEVANT",'
    '"reason":"请用中文简要说明判断依据，指出核心研究对象是否为马铃薯",'
    '"research_field":"研究方向枚举之一，NOT_RELEVANT 时留空"}]}'
)


# ── 四个版本 ─────────────────────────────────────────────────────

V1_BARE = _ROLE + _CORE_Q + (
    "判断步骤：\n"
    "1. 核心研究对象：文献主要研究什么？马铃薯是否为研究对象\n"
    "   本身或关键实验材料（而非仅在众多作物中顺带提及）？\n"
    "2. 对照下方判断标准给出最终 verdict\n\n"
) + _POSITIVE + _NEGATIVE_BASIC + _research_block() + _OUTPUT

V2_RULES = _ROLE + _CORE_Q + (
    "判断步骤：\n"
    "1. 核心研究对象：文献主要研究什么？马铃薯是否为研究对象\n"
    "   本身或关键实验材料（而非仅在众多作物中顺带提及）？\n"
    "2. 对照下方判断标准给出最终 verdict\n\n"
) + _POSITIVE + _NEGATIVE_BASIC + _NEGATIVE_RULES + _research_block() + _OUTPUT

V3_CHECK = _ROLE + _CORE_Q + _POSITIVE + _NEGATIVE_BASIC + _NEGATIVE_RULES + \
    _CHECK_STEP + _research_block() + _OUTPUT

V4_STRICT = _ROLE + _CORE_Q + _POSITIVE + _NEGATIVE_BASIC + _NEGATIVE_RULES + \
    _CHECK_STEP + _STRICT_DEFAULT + _research_block() + _OUTPUT

# V5：V3 + 物种甄别块（不加严格默认倾向，避免过度排除边缘正例）
V5_SPECIES = _ROLE + _SPECIES_GUARD + _CORE_Q + _POSITIVE + _NEGATIVE_BASIC + \
    _NEGATIVE_RULES + _CHECK_STEP + _research_block() + _OUTPUT

# V6：V5 + 严格默认倾向（最强约束，用于检验是否过排）
V6_SPECIES_STRICT = _ROLE + _SPECIES_GUARD + _CORE_Q + _POSITIVE + _NEGATIVE_BASIC + \
    _NEGATIVE_RULES + _CHECK_STEP + _STRICT_DEFAULT + _research_block() + _OUTPUT

# V7：V3 + 修正版物种甄别块（去掉与「近缘种算相关」冲突的那句），不含严格默认倾向。
# 依据：配对口径下 V3 与 V5 打平，但 V5 在 glm 上多排对 1 篇甘薯、多误排 1 篇
# Solanum 属研究（39271954），ds 上多误排 1 篇（31277248）。V7 保留物种甄别
# 的收益、修掉规则冲突。
V7_SPECIES_FIXED = _ROLE + _SPECIES_GUARD_FIXED + _CORE_Q + _POSITIVE + _NEGATIVE_BASIC + \
    _NEGATIVE_RULES + _CHECK_STEP + _research_block() + _OUTPUT

PROMPT_VARIANTS = {
    "V1_BARE": V1_BARE,
    "V2_RULES": V2_RULES,
    "V3_CHECK": V3_CHECK,
    "V4_STRICT": V4_STRICT,
    "V5_SPECIES": V5_SPECIES,
    "V6_SPECIES_STRICT": V6_SPECIES_STRICT,
    "V7_SPECIES_FIXED": V7_SPECIES_FIXED,
}


if __name__ == "__main__":
    for name, p in PROMPT_VARIANTS.items():
        print(f"{name:10s} {len(p):5d} 字符")
