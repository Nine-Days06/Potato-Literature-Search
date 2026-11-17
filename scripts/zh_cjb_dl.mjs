// 生物工程学报/微生物学报（勤云平台）：检索题名 → 点 PDF 链接 → 轮询 Downloads 落盘
// 用法：const m = await import("file:///D:/Project/Potato-Literature-Search/scripts/zh_cjb_dl.mjs"); await m.run(iab, [[pmid,title,base]])
import fs from "node:fs";
import path from "node:path";

const DL = "C:\\Users\\22813\\Downloads";
const OUT = "D:\\Project\\Potato-Literature-Search\\data\\pdfs_relevant";
const LOG = "D:\\Project\\Potato-Literature-Search\\data\\output\\cache\\cjb_log.json";
const norm = (s) => (s || "").replace(/[\s\u3000·—\-－,，。.:：;；()（）\[\]【】"'"'、\/]/g, "").toLowerCase();
const dlFiles = () => { try { return fs.readdirSync(DL).filter((f) => f.toLowerCase().endsWith(".pdf")); } catch { return []; } };

async function results(tab, base, title) {
  await tab.goto(`${base}/article/search?key=${encodeURIComponent(title)}&field=&ArticleSearch=GO`);
  await new Promise((r) => setTimeout(r, 2500));
  return tab.playwright.evaluate(() => {
    const out = [];
    for (const a of document.querySelectorAll("a")) {
      const h = a.getAttribute("href") || "";
      const m = h.match(/article\/abstract\/(\w+)/);
      if (!m) continue;
      const t = (a.textContent || "").trim();
      if (t.length < 6) continue;
      out.push({ id: m[1], t: t.slice(0, 80) });
    }
    return out;
  });
}

async function one(iab, pmid, title, base, pollSeconds = 60) {
  if (fs.existsSync(path.join(OUT, pmid + ".pdf"))) return "skip";
  const before = new Set(dlFiles());
  const tab = await iab.tabs.new();
  try {
    const list = await results(tab, base, title);
    if (!list.length) return "no_result";
    const nt = norm(title);
    const scored = list.map((x) => {
      const ct = norm(x.t);
      let sc = 0;
      if (ct.includes(nt)) sc += 10;
      sc += [...new Set(nt)].filter((c) => ct.includes(c)).length / Math.max(1, new Set(nt).size);
      return { ...x, sc };
    }).sort((a, b) => b.sc - a.sc);
    const best = scored[0];
    if (best.sc < 1.2) return "no_match " + list.slice(0, 3).map((x) => x.t).join("|").slice(0, 100);
    const pos = await tab.playwright.evaluate((id) => {
      const a = [...document.querySelectorAll("a")].find((x) => ((x.getAttribute("href") || "").includes("article/pdf/" + id)));
      if (!a) return null;
      a.scrollIntoView({ block: "center" });
      const r = a.getBoundingClientRect();
      return [Math.round(r.x + r.width / 2), Math.round(r.y + r.height / 2)];
    }, best.id);
    if (!pos) return "no_pdf_link " + best.id;
    await tab.ax.click(pos);
    for (let i = 0; i < pollSeconds; i++) {
      await new Promise((r) => setTimeout(r, 1000));
      for (const f of dlFiles().filter((f) => !before.has(f))) {
        const src = path.join(DL, f);
        let st; try { st = fs.statSync(src); } catch { continue; }
        if (st.size < 10240) continue;
        fs.copyFileSync(src, path.join(OUT, pmid + ".pdf"));
        fs.unlinkSync(src);
        return "OK " + f.slice(0, 40);
      }
    }
    return "no_file " + best.id + " " + best.t.slice(0, 30);
  } finally { try { await tab.close(); } catch {} }
}

export async function run(iab, list) {
  let log = {}; try { log = JSON.parse(fs.readFileSync(LOG, "utf8")); } catch {}
  const out = [];
  for (const [pmid, title, base] of list) {
    const r = await one(iab, pmid, title, base || "https://cjb.ijournals.cn/cjbcn");
    log[pmid] = r;
    fs.writeFileSync(LOG, JSON.stringify(log, null, 1));
    out.push(pmid + " => " + r);
  }
  return out.join("\n");
}
