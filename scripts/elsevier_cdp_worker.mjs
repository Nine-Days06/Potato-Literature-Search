// 用内置浏览器（已登录 CARSI/校内账号）逐篇取 ScienceDirect PDF。
// 依赖 browserManager 来自 browser-client.mjs 的 setupBrowserRuntime()。
// 单篇流程：文章页 → 抓带 md5 的 pdfft 链接 → 交给 Chrome 下载 → 复制为 <pmid>.pdf。
import fs from "node:fs";
import path from "node:path";
import { spawn } from "node:child_process";

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// 验证页出现时立刻出声并落一个标记文件，免得用户一直盯屏幕
function alertChallenge(tg, statePath) {
  try {
    fs.writeFileSync(
      path.join(path.dirname(statePath), "NEED_VERIFICATION.json"),
      JSON.stringify({ pmid: tg.pmid, pii: tg.pii, at: new Date().toISOString() }),
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
const CHALLENGE = [/are you a robot/i, /just a moment/i, /captcha/i, /正在检查您的浏览器/, /请稍候/];

function isChallenge(info) {
  return CHALLENGE.some((re) => re.test(`${info.title}\n${info.head}`));
}

async function pageInfo(cdp, pii) {
  const r = await cdp.send("Runtime.evaluate", {
    expression: `JSON.stringify({url:location.href,title:document.title,
      len:document.body?document.body.innerText.length:0,
      head:document.body?document.body.innerText.slice(0,200):"",
      signed:[...document.querySelectorAll('a')].map(a=>a.href)
        .filter(h=>/pdfft/.test(h)&&h.includes(${JSON.stringify(pii)}))[0]||""})`,
    returnByValue: true,
  });
  return JSON.parse(r.result.value);
}

function validPdf(buf) {
  return (
    buf.length > 10240 &&
    buf.subarray(0, 5).toString("latin1") === "%PDF-" &&
    buf.subarray(-2048).toString("latin1").includes("%%EOF")
  );
}

async function downloadOne({ runtime, browserManager, tg, timeoutMs, sharedTab, statePath }) {
  // 复用已授权的 tab 最稳；新建 tab 需带 URL 才有 origin
  const tab = sharedTab || (await browserManager.tabs.new({ url: "https://www.sciencedirect.com/" }));
  const ownTab = !sharedTab;
  let keepOpen = false;
  const started = Date.now();
  const tm = {};
  try {
    const cdp = await tab.capabilities.get("cdp");
    await runtime.documentation.get("confirmations");
    await cdp.documentation();
    const article = `https://www.sciencedirect.com/science/article/pii/${tg.pii}`;
    try {
      await tab.goto(article, { waitUntil: "domcontentloaded", timeoutMs });
    } catch {
      // SPA 慢时继续轮询 DOM
    }
    tm.article = Date.now() - started;

    let info = null;
    for (let i = 0; i < 20; i++) {
      await sleep(800);
      try {
        info = await pageInfo(cdp, tg.pii);
      } catch {
        continue;
      }
      if (isChallenge(info) || info.signed) break;
    }
    if (!info) return { status: "page_unavailable" };
    tm.link = Date.now() - started;
    if (isChallenge(info)) {
      // 留页面给用户点验证，并通过轮询自动续跑，省掉来回确认
      keepOpen = true;
      alertChallenge(tg, statePath);
      let passed = null;
      for (let w = 0; w < 20; w++) {
        await sleep(3000);
        const cur = await pageInfo(cdp, tg.pii).catch(() => null);
        if (!cur || isChallenge(cur)) continue;
        if (cur.signed) {
          passed = cur;
          break;
        }
        if (cur.len > 500) {
          try {
            await tab.goto(article, { waitUntil: "domcontentloaded", timeoutMs });
            await sleep(2500);
            const again = await pageInfo(cdp, tg.pii);
            if (!isChallenge(again) && again.signed) {
              passed = again;
              break;
            }
          } catch {
            // 继续等待
          }
        }
      }
      if (!passed) return { status: "challenge" };
      info = passed;
      keepOpen = false;
    }
    if (!info.signed) return { status: "no_pdf_link" };

    const dlPromise = tab.playwright.waitForEvent("download", { timeout: timeoutMs }).catch(() => null);
    await tab.goto(`${info.signed}&download=true`, {
      waitUntil: "domcontentloaded",
      timeoutMs,
    }).catch(() => {});
    tm.pdfNav = Date.now() - started;
    let dl = await Promise.race([dlPromise, sleep(10000).then(() => null)]);
    if (!dl) {
      const pinfo = await pageInfo(cdp, tg.pii).catch(() => null);
      if (pinfo && isChallenge(pinfo)) {
        alertChallenge(tg, statePath);
        dl = await dlPromise;
        if (!dl) return { status: "challenge" };
      } else {
        dl = await dlPromise;
      }
    }
    if (!dl) return { status: "no_download_event" };
    tm.downloadEvent = Date.now() - started;
    const src = await dl.path();
    const data = fs.readFileSync(src);
    fs.unlinkSync(src); // 不留在用户下载目录
    tm.save = Date.now() - started;
    return validPdf(data)
      ? { status: "downloaded", bytes: data.length, data, timings: tm }
      : { status: "invalid_pdf", bytes: data.length };
  } finally {
    if (ownTab && !keepOpen) await tab.close().catch(() => {});
  }
}

export async function runElsevierBatch({
  runtime,
  browserManager,
  targetsPath,
  statePath,
  pdfDir,
  limit = 0,
  delayMs = 8000,
  cooldownMs = 60000,
  retries = 1,
  timeoutMs = 45000,
  tab = null,
}) {
  const targets = JSON.parse(fs.readFileSync(targetsPath, "utf8"));
  const state = fs.existsSync(statePath) ? JSON.parse(fs.readFileSync(statePath, "utf8")) : {};
  const save = () => fs.writeFileSync(statePath, JSON.stringify(state, null, 1));
  const queue = limit ? targets.slice(0, limit) : targets;
  const stats = { processed: 0, downloaded: 0, failed: 0, skipped: 0, paused: null };

  outer: for (const tg of queue) {
    if (!tg.pii) {
      state[tg.pmid] = { status: "no_pii" };
      continue;
    }
    const dest = path.join(pdfDir, `${tg.pmid}.pdf`);
    if (fs.existsSync(dest) && fs.statSync(dest).size > 10240) {
      state[tg.pmid] = { status: "exists", bytes: fs.statSync(dest).size };
      stats.skipped++;
      continue;
    }
    let result = null;
    for (let attempt = 0; attempt <= retries; attempt++) {
      result = await downloadOne({ runtime, browserManager, tg, timeoutMs, sharedTab: tab, statePath });
      if (result.status === "challenge" || result.status === "downloaded") break;
      if (attempt < retries) await sleep(cooldownMs);
    }

    state[tg.pmid] = { status: result.status, pii: tg.pii };
    if (result.bytes) state[tg.pmid].bytes = result.bytes;
    if (result.status === "downloaded") {
      fs.writeFileSync(dest, result.data);
      fs.appendFileSync(statePath + ".timing.jsonl",
        JSON.stringify({ pmid: tg.pmid, ...(result.timings || {}) }) + "\n");
      stats.downloaded++;
    } else if (result.status === "challenge") {
      state.__meta = { ...(state.__meta || {}), paused: "challenge", at: tg.pmid };
      stats.paused = "challenge";
      save();
      break outer;
    } else {
      stats.failed++;
    }
    stats.processed++;
    save();
    await sleep(delayMs);
  }
  state.__meta = { ...(state.__meta || {}), ...stats, finishedAt: new Date().toISOString() };
  save();
  return stats;
}

// 快路径：浏览器已设「下载 PDF 而非打开」时，点页面上的 View PDF 会直接落盘到 ~/Downloads，
// Playwright 的 download 事件收不到，只能盯目录。
export async function runElsevierClick({
  runtime,
  browserManager,
  targetsPath,
  statePath,
  pdfDir,
  limit = 0,
  delayMs = 20000,
  timeoutMs = 60000,
  tab = null,
}) {
  const fs = await import("node:fs");
  const os = await import("node:os");
  const path = await import("node:path");
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const dlDir = path.join(os.homedir(), "Downloads");
  const targets = JSON.parse(fs.readFileSync(targetsPath, "utf8"));
  const state = fs.existsSync(statePath) ? JSON.parse(fs.readFileSync(statePath, "utf8")) : {};
  const save = () => fs.writeFileSync(statePath, JSON.stringify(state, null, 1));
  const stats = { processed: 0, downloaded: 0, failed: 0, skipped: 0 };
  const page = tab || (await browserManager.tabs.new({ url: "https://www.sciencedirect.com/" }));

  const waitFile = async (pii, deadline) => {
    // 文件名形如 1-s2.0-<PII>-main.pdf 或 -mainext.pdf（含扩展数据）；重复下载会变成 " (1)"
    const re = new RegExp(`^(1-s2\\.0-)?${pii}-main(ext)?( \\(\\d+\\))?\\.pdf$`);
    let last = -1;
    while (Date.now() < deadline) {
      const hit = fs.readdirSync(dlDir).find((f) => re.test(f));
      if (hit) {
        const p = path.join(dlDir, hit);
        const size = fs.statSync(p).size;
        if (size === last && size > 10240) {
          const buf = fs.readFileSync(p);
          if (buf.subarray(0, 5).toString("latin1") === "%PDF-" &&
              buf.subarray(-2048).toString("latin1").includes("%%EOF")) return { buf, p };
        }
        last = size;
      }
      await sleep(1000);
    }
    return null;
  };

  for (const tg of (limit ? targets.slice(0, limit) : targets)) {
    const dest = path.join(pdfDir, `${tg.pmid}.pdf`);
    if (fs.existsSync(dest) && fs.statSync(dest).size > 10240) {
      state[tg.pmid] = { status: "exists" };
      stats.skipped++;
      continue;
    }
    try {
      await page.goto(`https://www.sciencedirect.com/science/article/pii/${tg.pii}`,
        { waitUntil: "domcontentloaded", timeoutMs: 45000 }).catch(() => {});
      await sleep(2500);
      // locator 点击是唯一能触发下载的方式：鼠标坐标点不动，JS a.click() 被拦，AX 快照要 40s
      let got = null;
      await page.playwright.locator("a[href*='pdfft']").first()
        .click({ timeout: 20000 }).catch(() => {});
      if (!got) {
        got = await waitFile(tg.pii, Date.now() + timeoutMs);
      }
      if (!got) {
        state[tg.pmid] = { status: "no_download" };
        stats.failed++;
      } else {
        fs.writeFileSync(dest, got.buf);
        fs.unlinkSync(got.p);
        state[tg.pmid] = { status: "downloaded", bytes: got.buf.length, how: "click_download" };
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
