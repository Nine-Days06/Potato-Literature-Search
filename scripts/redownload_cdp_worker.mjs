// 重下 worker：按 DOI 打开出版商页面，取 citation_pdf_url（或页面上的 PDF 链接）
// 触发内置浏览器下载，落到 staging 目录，供后续体检通过再替换原文件。
import fs from "node:fs";
import path from "node:path";
import os from "node:os";

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function isPdf(buf) {
  return buf.length > 20000 && buf.subarray(0, 5).toString("latin1") === "%PDF-";
}

export async function runRedownload({
  runtime,
  browserManager,
  tab,
  targetsPath,
  statePath,
  stagingDir,
  limit = 0,
  delayMs = 20000,
  timeoutMs = 60000,
}) {
  const targets = JSON.parse(fs.readFileSync(targetsPath, "utf8"));
  const state = fs.existsSync(statePath) ? JSON.parse(fs.readFileSync(statePath, "utf8")) : {};
  const save = () => fs.writeFileSync(statePath, JSON.stringify(state, null, 1));
  const dlDir = path.join(os.homedir(), "Downloads");
  const page = tab || (await browserManager.tabs.new({ url: "https://doi.org/" }));
  const cdp = await page.capabilities.get("cdp");
  await runtime.documentation.get("confirmations");
  await runtime.documentation.get("capabilities/tab/cdp");

  const snap = () => new Set(fs.readdirSync(dlDir).filter((f) => f.toLowerCase().endsWith(".pdf")));
  const waitPdf = async (before, deadline) => {
    while (Date.now() < deadline) {
      const fresh = fs.readdirSync(dlDir).filter((f) => f.toLowerCase().endsWith(".pdf") && !before.has(f));
      for (const f of fresh) {
        const p = path.join(dlDir, f);
        const buf = fs.readFileSync(p);
        if (isPdf(buf)) return { p, buf, f };
      }
      await sleep(1500);
    }
    return null;
  };

  const stats = { processed: 0, downloaded: 0, failed: 0 };
  const queue = targets.filter(
    (t) => !fs.existsSync(path.join(stagingDir, `${t.pmid}.pdf`)) && !state[t.pmid],
  );
  stats.skipped = targets.length - queue.length;
  for (const tg of limit ? queue.slice(0, limit) : queue) {
    const dest = path.join(stagingDir, `${tg.pmid}.pdf`);
    if (fs.existsSync(dest)) {
      state[tg.pmid] = { status: "exists" };
      stats.processed++;
      continue;
    }
    const before = snap();
    try {
      await page
        .goto(`https://doi.org/${encodeURI(tg.doi)}`, { waitUntil: "domcontentloaded", timeoutMs: 60000 })
        .catch(() => {});
      await sleep(2500);
      const info = await cdp
        .send("Runtime.evaluate", {
          expression: `JSON.stringify({url:location.href,title:document.title,
            pdf:(document.querySelector("meta[name='citation_pdf_url']")||{}).content||"",
            links:[...document.querySelectorAll("a")].map(a=>a.href)
              .filter(h=>/\\.pdf(\\?|$)|pdfdirect|\\/pdf\\b/i.test(h)).slice(0,5)})`,
          returnByValue: true,
        })
        .then((r) => JSON.parse(r.result.value))
        .catch(() => null);
      const cand = info && (info.pdf || (info.links && info.links[0]));
      if (cand) {
        await page.goto(cand, { waitUntil: "domcontentloaded", timeoutMs: 60000 }).catch(() => {});
      } else {
        await page.playwright
          .locator("a[href$='.pdf'],a[href*='pdfdirect'],a[href*='/pdf/']")
          .first()
          .click({ timeout: 15000 })
          .catch(() => {});
      }
      const got = await waitPdf(before, Date.now() + timeoutMs);
      if (!got) {
        state[tg.pmid] = { status: "no_download", landed: info && info.url };
        stats.failed++;
      } else {
        fs.writeFileSync(dest, got.buf);
        fs.unlinkSync(got.p);
        state[tg.pmid] = { status: "downloaded", bytes: got.buf.length, how: "doi_pdf" };
        stats.downloaded++;
      }
    } catch (e) {
      state[tg.pmid] = { status: "error", error: String(e).slice(0, 120) };
      stats.failed++;
    }
    stats.processed++;
    save();
    await sleep(delayMs);
  }
  state.__meta = { ...stats, at: new Date().toISOString() };
  save();
  return stats;
}
