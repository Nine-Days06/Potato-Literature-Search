"""把"订阅库内仍缺全文"清单导出为单文件 HTML，可按库/期刊分组浏览并点击跳转。

用法: python scripts/export_missing_html.py [--csv ...] [--out ...]
"""
import argparse
import csv
import json
from datetime import date
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
DEFAULT_CSV = BASE / "data/output/exports/full/missing_subscribed_20261101.csv"
DEFAULT_OUT = BASE / "data/output/exports/full/missing_subscribed_20261101.html"


def load(csv_path):
    rows = list(csv.DictReader(open(csv_path, encoding="utf-8-sig")))
    items = []
    for r in rows:
        doi = (r.get("doi") or "").strip()
        items.append(
            {
                "pmid": r.get("pmid", "").strip(),
                "doi": doi,
                "title": (r.get("title") or "").strip() or "(无标题)",
                "journal": (r.get("journal") or "").strip() or "(未知期刊)",
                "year": (r.get("year") or "").strip()[:4],
                "authors": (r.get("authors") or "").strip(),
                "db": (r.get("db") or "").strip() or "(未分类)",
            }
        )
    return items


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", default=str(DEFAULT_CSV))
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    args = ap.parse_args()

    items = load(Path(args.csv))
    payload = json.dumps(items, ensure_ascii=False).replace("<", "\\u003c")
    html = TEMPLATE.replace("__DATA__", payload).replace("__TOTAL__", str(len(items))
                                                          ).replace("__DATE__", date.today().isoformat())
    Path(args.out).write_text(html, encoding="utf-8")
    print(f"{len(items)} 条 -> {args.out}")


TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>待补全文清单 · 订阅库内 __TOTAL__ 篇</title>
<style>
:root{
  --ink:#1b1a17; --ink-2:#5b574e; --paper:#faf7f0; --card:#fffdf8;
  --line:#e2dbcb; --accent:#8a4b2a; --accent-2:#b8863b; --ok:#3f6b4f;
  --serif:"Source Han Serif SC","Songti SC","Noto Serif SC",Georgia,serif;
  --sans:"Source Han Sans SC","PingFang SC","Microsoft YaHei",system-ui,sans-serif;
}
*{box-sizing:border-box}
body{margin:0;background:
   radial-gradient(1200px 500px at 15% -10%,#f3e9d7 0%,transparent 60%),
   radial-gradient(900px 400px at 95% 0%,#eef1e6 0%,transparent 55%),
   var(--paper);
 color:var(--ink);font-family:var(--sans);line-height:1.55;-webkit-font-smoothing:antialiased}
header{padding:46px 28px 26px;max-width:1180px;margin:0 auto}
h1{font-family:var(--serif);font-weight:600;font-size:clamp(26px,4vw,42px);margin:0 0 10px;letter-spacing:.01em}
.sub{color:var(--ink-2);font-size:14px;max-width:62ch}
.stats{display:flex;flex-wrap:wrap;gap:10px;margin-top:20px}
.stat{background:var(--card);border:1px solid var(--line);border-radius:2px;padding:8px 13px;font-size:13px}
.stat b{font-family:var(--serif);font-size:19px;margin-right:5px}
main{max-width:1180px;margin:0 auto;padding:0 28px 80px}
.bar{position:sticky;top:0;z-index:5;background:rgba(250,247,240,.94);backdrop-filter:blur(6px);
     border-bottom:1px solid var(--line);display:flex;flex-wrap:wrap;gap:10px;align-items:center;
     padding:12px 0;margin-bottom:8px}
input[type=search]{flex:1 1 240px;min-width:180px;padding:9px 12px;border:1px solid var(--line);
  border-radius:2px;background:var(--card);font:14px var(--sans);color:var(--ink)}
input[type=search]:focus{outline:2px solid var(--accent-2);outline-offset:1px}
.seg{display:flex;border:1px solid var(--line);border-radius:2px;overflow:hidden;background:var(--card)}
.seg button{border:0;background:none;padding:9px 14px;font:13px var(--sans);color:var(--ink-2);cursor:pointer}
.seg button[aria-pressed=true]{background:var(--ink);color:#fbf8f1}
.seg button+button{border-left:1px solid var(--line)}
.hint{font-size:12px;color:var(--ink-2)}
.group{margin:26px 0 0}
.group>h2{font-family:var(--serif);font-size:clamp(19px,2.4vw,24px);font-weight:600;margin:0 0 4px;
  display:flex;align-items:baseline;gap:10px;flex-wrap:wrap}
.group>h2 .n{font:12px var(--sans);color:#fff;background:var(--accent);border-radius:2px;padding:2px 7px}
.group>p{margin:0 0 14px;color:var(--ink-2);font-size:13px}
.jgroup{border-top:1px solid var(--line);padding:12px 0 4px}
.jgroup h3{font-family:var(--serif);font-size:16px;font-weight:600;margin:0 0 2px}
.jgroup h3 span{font:12px var(--sans);color:var(--ink-2);margin-left:8px}
.item{display:grid;grid-template-columns:64px 1fr auto;gap:14px;align-items:start;
  padding:9px 0;border-bottom:1px dotted #e9e2d3}
.item>div{min-width:0}
.item .yr{font-family:var(--serif);font-size:15px;color:var(--ink-2);padding-top:2px}
.item a.t{color:var(--ink);text-decoration:none;font-size:14.5px;border-bottom:1px solid transparent;overflow-wrap:anywhere}
.item a.t:hover{border-bottom-color:var(--accent-2);color:var(--accent)}
.meta{font-size:12px;color:var(--ink-2);margin-top:3px;word-break:break-all}
.links{display:flex;gap:8px;font-size:12px;white-space:nowrap}
.links a{color:var(--accent);text-decoration:none;border:1px solid var(--line);border-radius:2px;padding:2px 7px}
.links a:hover{border-color:var(--accent);background:#fff}
.links a.pmid{color:var(--ok)}
.empty{padding:60px 0;text-align:center;color:var(--ink-2);font-size:14px}
.group,.jgroup{min-width:0}
@media (max-width:720px){
  body{overflow-x:hidden}
  .bar{gap:8px}
  input[type=search]{flex:1 1 100%;min-width:0}
  .hint{width:100%}
  .item{grid-template-columns:1fr;gap:4px}
  .item .yr{padding:0}
  .links{padding-top:4px}
  header{padding:30px 18px 18px} main{padding:0 18px 60px}
}
@media print{.bar{position:static}body{background:#fff}}
</style>
<header>
  <h1>待补全文清单</h1>
  <div class="sub">华中农业大学订阅库范围内、仍缺 PDF 全文的相关文献。点击标题经 DOI 跳转到出版社页面（需校内 IP）；右侧另有 PubMed 入口。按库或按期刊分组，可用上方搜索框过滤标题 / 期刊 / PMID / DOI。</div>
  <div class="stats" id="stats"></div>
</header>
<main>
  <div class="bar">
    <div class="seg" role="group" aria-label="分组方式">
      <button id="byDb" aria-pressed="true">按库</button>
      <button id="byJournal" aria-pressed="false">按期刊</button>
    </div>
    <input id="q" type="search" placeholder="搜索标题、期刊、PMID、DOI…" autocomplete="off">
    <span class="hint" id="hint"></span>
  </div>
  <div id="body"></div>
</main>
<script>
const DATA = __DATA__;
const body = document.getElementById('body');
const q = document.getElementById('q');
const hint = document.getElementById('hint');
let mode = 'db';

const doiUrl = d => 'https://doi.org/' + encodeURI(d);
const pmUrl = p => 'https://pubmed.ncbi.nlm.nih.gov/' + p + '/';
const esc = s => String(s).replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));

function counts(key){
  const m = new Map();
  for (const it of DATA) m.set(it[key], (m.get(it[key])||0)+1);
  return [...m.entries()].sort((a,b)=> b[1]-a[1]);
}

function statCard(n,label){ return `<div class="stat"><b>${n}</b>${esc(label)}</div>`; }

function render(){
  const term = q.value.trim().toLowerCase();
  const rows = term ? DATA.filter(it =>
      (it.title+' '+it.journal+' '+it.pmid+' '+it.doi+' '+it.db).toLowerCase().includes(term)) : DATA;
  hint.textContent = term ? `${rows.length} / ${DATA.length} 条` : `${DATA.length} 条`;

  const primary = mode === 'db' ? 'db' : 'journal';
  const secondary = mode === 'db' ? 'journal' : 'db';
  const groups = new Map();
  for (const it of rows){
    const k = it[primary];
    if (!groups.has(k)) groups.set(k, new Map());
    const sub = groups.get(k);
    const sk = it[secondary];
    if (!sub.has(sk)) sub.set(sk, []);
    sub.get(sk).push(it);
  }
  const ordered = [...groups.entries()].sort((a,b)=>
    [...b[1].values()].reduce((n,v)=>n+v.length,0) - [...a[1].values()].reduce((n,v)=>n+v.length,0));

  const out = [];
  for (const [name, subs] of ordered){
    const total = [...subs.values()].reduce((n,v)=>n+v.length,0);
    out.push(`<section class="group"><h2>${esc(name)}<span class="n">${total}</span></h2>`);
    out.push(`<p>${subs.size} ${mode==='db'?'种期刊':'个库'}</p>`);
    const subOrdered = [...subs.entries()].sort((a,b)=> b[1].length - a[1].length);
    for (const [sname, list] of subOrdered){
      list.sort((a,b)=> (b.year||'0').localeCompare(a.year||'0') || a.title.localeCompare(b.title));
      out.push(`<div class="jgroup"><h3>${esc(sname)}<span>${list.length}</span></h3>`);
      for (const it of list){
        const primary = it.doi ? doiUrl(it.doi) : pmUrl(it.pmid);
        const links = [];
        if (it.doi) links.push(`<a class="doi" href="${esc(doiUrl(it.doi))}" target="_blank" rel="noopener">DOI</a>`);
        if (it.pmid) links.push(`<a class="pmid" href="${esc(pmUrl(it.pmid))}" target="_blank" rel="noopener">PubMed</a>`);
        out.push(`<div class="item"><div class="yr">${esc(it.year||'—')}</div>`+
          `<div><a class="t" href="${esc(primary)}" target="_blank" rel="noopener">${esc(it.title)}</a>`+
          `<div class="meta">PMID ${esc(it.pmid||'—')} · ${esc(it.doi||'无 DOI，按期刊检索')}${it.authors?' · '+esc(it.authors.split('|').slice(0,2).join(', ')):''}</div></div>`+
          `<div class="links">${links.join('')}</div></div>`);
      }
      out.push('</div>');
    }
    out.push('</section>');
  }
  body.innerHTML = out.join('') || '<div class="empty">没有匹配的文献</div>';
}

function renderStats(){
  const dbs = counts('db'), js = counts('journal');
  document.getElementById('stats').innerHTML =
    statCard(DATA.length,'篇待补全文') + statCard(dbs.length,'个订阅库') + statCard(js.length,'种期刊') +
    dbs.slice(0,6).map(([k,v])=>statCard(v, k)).join('');
}

document.getElementById('byDb').onclick = e => { mode='db'; toggle(e.target); };
document.getElementById('byJournal').onclick = e => { mode='journal'; toggle(e.target); };
function toggle(btn){
  document.getElementById('byDb').setAttribute('aria-pressed', String(btn.id==='byDb'));
  document.getElementById('byJournal').setAttribute('aria-pressed', String(btn.id==='byJournal'));
  render();
}
q.addEventListener('input', render);
renderStats(); render();
</script>
</html>
"""


if __name__ == "__main__":
    main()
