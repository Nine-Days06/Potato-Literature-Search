"""一次性脚本：判定标准变更（不限研究方向）后，删除旧 LLM 判定并用新提示词重跑

删除范围：所有 human_review IS NULL 的记录（RELEVANT 与 NOT_RELEVANT 都删——
旧 NOT_RELEVANT 多因"无具体基因名/农艺方向"被排除，新标准下这些应为 RELEVANT）。
人工复核过的记录（human_review = 'Y'/'N'）是人工事实，保留不重跑。
"""
import sys
sys.stdout.reconfigure(encoding='utf-8')
from config.settings import DB_PATH
import sqlite3

conn = sqlite3.connect(DB_PATH)
cur = conn.cursor()

total = cur.execute('SELECT COUNT(1) FROM llm_validation').fetchone()[0]
relevant = cur.execute("SELECT COUNT(1) FROM llm_validation WHERE llm_verdict='RELEVANT'").fetchone()[0]
not_rel = cur.execute("SELECT COUNT(1) FROM llm_validation WHERE llm_verdict='NOT_RELEVANT'").fetchone()[0]

print(f'llm_validation 表总计: {total} 条')
to_delete = cur.execute(
    "SELECT COUNT(1) FROM llm_validation WHERE human_review IS NULL"
).fetchone()[0]
reviewed = total - to_delete
print(f'  RELEVANT: {relevant} 条')
print(f'  NOT_RELEVANT: {not_rel} 条')
print(f'  未复核（将删除后重跑）: {to_delete} 条')
print(f'  已人工复核（保留）: {reviewed} 条')

if to_delete == 0:
    print('没有需要重跑的待复核记录，退出')
    sys.exit(0)

print(f'\n正在删除 {to_delete} 条未复核记录...')
cur.execute("DELETE FROM llm_validation WHERE human_review IS NULL")
conn.commit()
print('删除完成')

remaining = cur.execute('SELECT COUNT(1) FROM llm_validation').fetchone()[0]
print(f'llm_validation 表现有 {remaining} 条（均为已人工复核记录）')
conn.close()
print('\n请运行: python main.py --step validate')
