// 用内置浏览器（已登录 CARSI/校内账号）按 DOI 直接取 Wiley PDF。
// Wiley 的 /doi/pdf/<doi> 会 302 到对应子域并直接触发下载，不需要先开文章页。
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const CHALLENGE = [/just a moment/i, /are you a robot/i, /captcha/i, /请稍候/];

function validPdf(buf) {
  return (
    buf.length > 10240 &&
    buf.subarray(0, 5).toString("latin1") === "%PDF-" &&
    buf.subarray(-2048).toString("latin1").includes("%%EOF")
  );
}

export async function runWileyBatch({
  runtime,
  browserManager,
  targetsPath,
  statePath,
  pdfDir,
  limit = 0,
  delayMs = 3000,
  timeoutMs = 45000,
  tab = null,
  retryFailed = false,
  pdfBase = "https://onlinelibrary.wiley.com/doi/pdf/",
}) {
  const targets = JSON.parse(fs.readFileSync(targetsPath, "utf8"));
  const state = fs.existsSync(statePath) ? JSON.parse(fs.readFileSync(statePath, "utf8")) : {};
  const save = () => fs.writeFileSync(statePath, JSON.stringify(state, null, 1));
  const queue = limit ? targets.slice(0, limit) : targets;
  const stats = { processed: 0, downloaded: 0, failed: 0, skipped: 0, paused: null };

  const ownTab = !tab;
  const page = tab || (await browserManager.tabs.new({ url: "https://onlinelibrary.wiley.com/" }));
  let cdp = null;
  try {
    await runtime.documentation.get("confirmations");
    cdp = await page.capabilities.get("cdp");
    await cdp.documentation();
  } catch (e) {
    console.log("cdp_unavailable", String(e).slice(0, 80));
  }

  for (const tg of queue) {
    const dest = path.join(pdfDir, `${tg.pmid}.pdf`);
    if (fs.existsSync(dest) && fs.statSync(dest).size > 10240) {
      state[tg.pmid] = { status: "exists", bytes: fs.statSync(dest).size };
      stats.skipped++;
      continue;
    }
    // 失败项默认跳过，否则每次重跑都会在同一个坏 DOI 上白等 60s
    if (!retryFailed && state[tg.pmid] && /^(no_download|invalid_pdf|error)$/.test(state[tg.pmid].status)) {
      stats.skipped++;
      continue;
    }
    try {
      const dlPromise = page.playwright.waitForEvent("download", { timeout: 60000 }).catch(() => null);
      const startedAt = Date.now();
      await page
        .goto(`${pdfBase}${tg.doi}`, {
          waitUntil: "domcontentloaded",
          timeoutMs,
        })
        .catch(() => {});
      const dl = await dlPromise;
      let data = null;
      if (dl) {
        const src = await dl.path();
        data = fs.readFileSync(src);
        fs.unlinkSync(src);
      } else {
        // 老刊的下载事件常在 Playwright 侧丢失，但文件确实落到了磁盘
        const dlDir = path.join(os.homedir(), "Downloads");
        const fresh = fs.readdirSync(dlDir)
          .filter((f) => /\.pdf$/i.test(f))
          .map((f) => ({ f, t: fs.statSync(path.join(dlDir, f)).mtimeMs }))
          .filter((x) => x.t >= startedAt - 2000)
          .sort((a, b) => b.t - a.t);
        if (fresh.length) {
          data = fs.readFileSync(path.join(dlDir, fresh[0].f));
          fs.unlinkSync(path.join(dlDir, fresh[0].f));
        }
      }
      if (!data) {
        const title = await page.title().catch(() => "");
        if (CHALLENGE.some((re) => re.test(title))) {
          state.__meta = { ...(state.__meta || {}), paused: "challenge", at: tg.pmid };
          stats.paused = "challenge";
          break;
        }
        state[tg.pmid] = { status: "no_download" };
        stats.failed++;
      } else {
        if (validPdf(data)) {
          fs.writeFileSync(dest, data);
          state[tg.pmid] = { status: "downloaded", bytes: data.length };
          stats.downloaded++;
        } else {
          state[tg.pmid] = { status: "invalid_pdf", bytes: data.length };
          stats.failed++;
        }
      }
    } catch (e) {
      state[tg.pmid] = { status: "error", error: String(e).slice(0, 120) };
      stats.failed++;
    }
    stats.processed++;
    save();
    await sleep(delayMs);
  }
  state.__meta = { ...(state.__meta || {}), ...stats, finishedAt: new Date().toISOString() };
  save();
  if (ownTab) await page.close().catch(() => {});
  return stats;
}
