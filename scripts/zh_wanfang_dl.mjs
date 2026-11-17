// 万方批量下载：检索中文题名 → 点“下载” → 校验落盘文件名与题名 → 存入 data/pdfs_relevant/<pmid>.pdf
// 用法（在 node_repl 内）：const m = await import("file:///D:/Project/Potato-Literature-Search/scripts/zh_wanfang_dl.mjs"); await m.worun(agent, [[pmid,title,journal],...])
import fs from "node:fs";
import path from "node:path";

const DL = "C:\\Users\\22813\\Downloads";
const OUT = "D:\\Project\\Potato-Literature-Search\\data\\pdfs_relevant";
const LOG = "D:\\Project\\Potato-Literature-Search\\data\\output\\cache\\wf_log.json";

const norm = (s) => (s || "").replace(/[\s\u3000·—\-－,，。.:：;；()（）\[\]【】"'"'、\/]/g, "").toLowerCase();
const dlFiles = () => { try { return fs.readdirSync(DL).filter((f) => f.toLowerCase().endsWith(".pdf")); } catch { return []; } };

function save(pmid, src, title) {
  const fn = norm(path.basename(src, ".pdf"));
  const chars = new Set(norm(title));
  const cov = [...chars].filter((c) => fn.includes(c)).length / Math.max(1, chars.size);
  if (cov < 0.6) { fs.renameSync(src, path.join(DL, "_rej_" + path.basename(src))); return "title_mismatch " + path.basename(src).slice(0, 40); }
  fs.copyFileSync(src, path.join(OUT, pmid + ".pdf"));
  fs.unlinkSync(src);
  return "OK " + path.basename(src).slice(0, 45);
}

async function one(iab, pmid, title, journal) {
  const target = path.join(OUT, pmid + ".pdf");
  if (fs.existsSync(target)) return "skip";
  const before = new Set(dlFiles());
  const tab = await iab.tabs.new();
  try {
    await tab.goto("https://s.wanfangdata.com.cn/paper?q=" + encodeURIComponent(title));
    let body = "";
    for (let i = 0; i < 12; i++) { await new Promise((r) => setTimeout(r, 1500)); body = await tab.playwright.evaluate(() => document.body.innerText); if (/找到\s*\d+\s*条|没有找到/.test(body)) break; }
    if (/没有找到/.test(body)) return "no_record";
    await tab.playwright.evaluate(() => window.scrollTo(0, 400));
    await new Promise((r) => setTimeout(r, 2000));
    const cands = await tab.playwright.evaluate(() => {
      const out = [];
      for (const s of [...document.querySelectorAll("span")].filter((e) => (e.textContent || "").trim() === "下载")) {
        const r = s.getBoundingClientRect();
        let row = "";
        for (const dy of [90, 60, 120, 30]) {
          let p = document.elementFromPoint(r.x, r.y - dy);
          for (let i = 0; i < 8 && p; i++) { const tx = (p.innerText || "").replace(/\s+/g, " "); if (tx.includes("《")) { row = tx.slice(0, 120); break; } p = p.parentElement; }
          if (row) break;
        }
        out.push({ x: Math.round(r.x + r.width / 2), y: Math.round(r.y + r.height / 2), row });
      }
      return out;
    });
    if (!cands.length) return "no_download_btn";
    const nt = norm(title), wj = norm(journal);
    let best = null, bs = 0;
    for (const c of cands) {
      if (!c.row) continue;
      const ct = norm(c.row);
      let sc = wj && ct.includes(wj) ? 4 : -1;
      if (ct.includes(nt)) sc += 5;
      sc += 3 * [...new Set(nt)].filter((ch) => ct.includes(ch)).length / Math.max(1, new Set(nt).size);
      if (sc > bs) { bs = sc; best = c; }
    }
    if (!best || bs < 4) return "no_match_row " + cands.map((c) => c.row.slice(0, 30)).join("|").slice(0, 90);
    await tab.ax.click([best.x, best.y]);
    // 结果页“下载”常弹新标签：无权限页直接判死，可下载页再点一次
    await new Promise((r) => setTimeout(r, 5000));
    const mine = Number(tab.id);
    const extra = (await iab.tabs.list()).filter((t) => Number(t.id) > mine);
    for (const t of extra) {
      const nt = await iab.tabs.get(t.id);
      const txt = await nt.playwright.evaluate(() => document.body.innerText).catch(() => "");
      if (/无权限|没有权限|权限不足/.test(txt)) { try { await nt.close(); } catch {} return "no_permission"; }
      const pos = await nt.playwright.evaluate(() => {
        for (const w of ["确认下载", "立即下载", "点此下载", "下载"]) {
          const e = [...document.querySelectorAll("a,button,span,div")].find((x) => (x.textContent || "").trim() === w && x.getBoundingClientRect().width > 0);
          if (e) { e.scrollIntoView({ block: "center" }); const r = e.getBoundingClientRect(); return [Math.round(r.x + r.width / 2), Math.round(r.y + r.height / 2)]; }
        }
        return null;
      }).catch(() => null);
      if (pos) await nt.ax.click(pos).catch(() => {});
    }
    for (let i = 0; i < 20; i++) {
      await new Promise((r) => setTimeout(r, 1000));
      for (const f of dlFiles().filter((f) => !before.has(f))) {
        const src = path.join(DL, f);
        let st; try { st = fs.statSync(src); } catch { continue; }
        if (st.size < 10240) continue;
        return save(pmid, src, title);
      }
    }
    return "click_no_file " + best.row.slice(0, 40);
  } finally { try { await tab.close(); } catch {} }
}

export async function worun(iab, list) {
  let log = {}; try { log = JSON.parse(fs.readFileSync(LOG, "utf8")); } catch {}
  const res = [];
  for (const [pmid, title, journal] of list) {
    const r = await one(iab, pmid, title, journal);
    log[pmid] = r;
    fs.writeFileSync(LOG, JSON.stringify(log, null, 1));
    res.push(pmid + " => " + r);
  }
  return res.join("\n");
}
