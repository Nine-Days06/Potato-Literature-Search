// 用内置浏览器（知网机构登录）批量下载中文全文：abstract 页 → #pdfDown → 落盘 <pmid>.pdf
// 用法见文件末尾注释；由 node_repl 调用。
import fs from "node:fs";
import path from "node:path";

const DL = "C:\\Users\\22813\\Downloads";
const OUT = "D:\\Project\\Potato-Literature-Search\\data\\pdfs_relevant";
const STATE = "D:\\Project\\Potato-Literature-Search\\data\\output\\cache\\zh_cn_download_state.json";
const HITS = "D:\\Project\\Potato-Literature-Search\\data\\output\\cache\\zh_search_all.json";

const isPdf = (p) => fs.readFileSync(p).subarray(0, 5).toString("latin1").startsWith("%PDF");
const norm = (s) => (s || "").replace(/\s+/g, "");

// 中断后 Downloads 里可能残留已下载文件，按「篇名_作者.pdf」回填
export function recover(byTitle) {
  const done = [];
  for (const n of fs.readdirSync(DL)) {
    const p = path.join(DL, n);
    if (!fs.statSync(p).isFile() || !n.endsWith(".pdf")) continue;
    const pmid = byTitle[norm(n.replace(/_[^_]+\.pdf$/, ""))];
    if (!pmid) continue;
    fs.copyFileSync(p, path.join(OUT, pmid + ".pdf"));
    fs.unlinkSync(p);
    done.push(pmid);
  }
  return done;
}

export async function dlOne(tab, pmid, h, pollSeconds = 45) {
  const t0 = Date.now();
  await tab.goto(h);
  try {
    await tab.playwright.locator("#pdfDown").click({ timeout: 15000 });
  } catch {
    return { pmid, status: "nobtn" };
  }
  for (let i = 0; i < pollSeconds; i++) {
    await new Promise((r) => setTimeout(r, 600));
    for (const n of fs.readdirSync(DL)) {
      const p = path.join(DL, n);
      const st = fs.statSync(p);
      if (!st.isFile() || st.mtimeMs < t0 - 500) continue;
      if (st.size > 10000 && isPdf(p)) {
        const dst = path.join(OUT, pmid + ".pdf");
        fs.copyFileSync(p, dst);
        fs.unlinkSync(p);
        return { pmid, status: "done", size: fs.statSync(dst).size };
      }
    }
  }
  return { pmid, status: "nofile" };
}

export function loadQueue() {
  const hits = JSON.parse(fs.readFileSync(HITS, "utf8"));
  const byPmid = Object.fromEntries(hits.map((h) => [h.pmid, h]));
  const byTitle = Object.fromEntries(hits.filter((h) => h.title).map((h) => [norm(h.title), h.pmid]));
  const state = JSON.parse(fs.readFileSync(STATE, "utf8"));
  const dead = (p) => ["nobtn", "record_only", "nohref"].includes((state[p] || {}).reason);
  const queue = hits
    .filter((h) => h.h && ["single", "matched"].includes(h.flag) && !dead(h.pmid) && !fs.existsSync(path.join(OUT, h.pmid + ".pdf")))
    .map((h) => h.pmid);
  return { byPmid, byTitle, queue };
}

// 顺序下 n 篇，每篇落盘后立即写 state，避免中断丢进度
export async function run(tab, n = 8) {
  const { byPmid, byTitle, queue } = loadQueue();
  recover(byTitle);
  const state = JSON.parse(fs.readFileSync(STATE, "utf8"));
  const out = [];
  for (const pmid of queue.slice(0, n)) {
    let r;
    try {
      r = await dlOne(tab, pmid, byPmid[pmid].h);
    } catch {
      r = { pmid, status: "err" };
    }
    await new Promise((res) => setTimeout(res, 3000));
    out.push(r);
    state[pmid] = r.status === "done" ? { status: "done", file: pmid + ".pdf", bytes: r.size } : { status: "failed", reason: r.status };
    fs.writeFileSync(STATE, JSON.stringify(state, null, 1));
  }
  return { left: queue.length - out.length, log: out.map((r) => r.pmid + ":" + r.status).join(" ") };
}
