// 用内置浏览器（已登录校内账号）下载 Oxford Journals 的 PDF。
// 文章页 a[href*="/article-pdf/"] 是直链；先开文章页取链接，再交给 Chrome 下载器。
import fs from "node:fs";
import path from "node:path";
import { spawn } from "node:child_process";

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const CHALLENGE = [/just a moment/i, /请稍候/, /are you a robot/i, /captcha/i];

// 验证页出现时出声 + 落标记文件
function alertChallenge(tg, statePath) {
  try {
    fs.writeFileSync(
      path.join(path.dirname(statePath), "NEED_VERIFICATION.json"),
      JSON.stringify({ pmid: tg.pmid, doi: tg.doi, at: new Date().toISOString() }),
    );
  } catch {}
  try {
    spawn(
      "powershell",
      ["-NoProfile", "-Command",
       "1..3|%{[console]::beep(1200,400);Start-Sleep -m 150};[System.Media.SystemSounds]::Exclamation.Play()"],
      { detached: true, stdio: "ignore" },
    ).unref();
  } catch {}
}

function validPdf(buf) {
  return (
    buf.length > 10240 &&
    buf.subarray(0, 5).toString("latin1") === "%PDF-" &&
    buf.subarray(-2048).toString("latin1").includes("%%EOF")
  );
}

// advance-article 用的是 /advance-article-pdf/，与正式卷期的 /article-pdf/ 不同
const linkExpr = (pattern) => `JSON.stringify([...document.querySelectorAll('a')]
  .map(a=>a.href).filter(h=>new RegExp(${JSON.stringify(pattern)}).test(h))[0]||'')`;

export async function runOupBatch({
  runtime,
  browserManager,
  targetsPath,
  statePath,
  pdfDir,
  limit = 0,
  delayMs = 8000,
  timeoutMs = 45000,
  tab = null,
  linkRegex = "\\/(advance-)?article-pdf\\/",
}) {
  const targets = JSON.parse(fs.readFileSync(targetsPath, "utf8"));
  const state = fs.existsSync(statePath) ? JSON.parse(fs.readFileSync(statePath, "utf8")) : {};
  const save = () => fs.writeFileSync(statePath, JSON.stringify(state, null, 1));
  const queue = limit ? targets.slice(0, limit) : targets;
  const stats = { processed: 0, downloaded: 0, failed: 0, skipped: 0, paused: null };

  const ownTab = !tab;
  const page = tab || (await browserManager.tabs.new({ url: "https://academic.oup.com/" }));
  await runtime.documentation.get("confirmations");
  const cdp = await page.capabilities.get("cdp");
  await cdp.documentation();
  const evalJson = async (expr) => {
    const r = await cdp.send("Runtime.evaluate", { expression: expr, returnByValue: true });
    return JSON.parse(r.result.value);
  };

  for (const tg of queue) {
    const dest = path.join(pdfDir, `${tg.pmid}.pdf`);
    if (fs.existsSync(dest) && fs.statSync(dest).size > 10240) {
      state[tg.pmid] = { status: "exists", bytes: fs.statSync(dest).size };
      stats.skipped++;
      continue;
    }
    try {
      await page
        .goto(`https://doi.org/${tg.doi}`, { waitUntil: "domcontentloaded", timeoutMs })
        .catch(() => {});
      await sleep(1500);
      const title = await page.title().catch(() => "");
      if (CHALLENGE.some((re) => re.test(title))) {
        alertChallenge(tg, statePath);
        state[tg.pmid] = { status: "challenge" };
        state.__meta = { ...(state.__meta || {}), paused: "challenge", at: tg.pmid };
        stats.paused = "challenge";
        break;
      }
      const pdfUrl = await evalJson(linkExpr(linkRegex)).catch(() => "");
      if (!pdfUrl) {
        state[tg.pmid] = { status: "no_pdf_link", title: title.slice(0, 60) };
        stats.failed++;
      } else {
        const dlPromise = page.playwright.waitForEvent("download", { timeout: 90000 }).catch(() => null);
        await page.goto(pdfUrl, { waitUntil: "domcontentloaded", timeoutMs }).catch(() => {});
        const dl = await dlPromise;
        if (!dl) {
          state[tg.pmid] = { status: "no_download_event" };
          stats.failed++;
        } else {
          const src = await dl.path();
          const data = fs.readFileSync(src);
          fs.unlinkSync(src);
          if (validPdf(data)) {
            fs.writeFileSync(dest, data);
            state[tg.pmid] = { status: "downloaded", bytes: data.length };
            stats.downloaded++;
          } else {
            state[tg.pmid] = { status: "invalid_pdf", bytes: data.length };
            stats.failed++;
          }
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
