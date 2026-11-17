// Springer：直接命中 link.springer.com 会回登录墙；HZAU 电子资源导航的代理前缀
// （bcyc.hzau.edu.cn/s/com/springer/link/G.https/...）才带机构权限。
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const PROXY = "http://bcyc.hzau.edu.cn/s/com/springer/link/G.https/content/pdf/";
const CHAPTER = "http://bcyc.hzau.edu.cn/s/com/springer/link/G.https/chapter/";

// 代理把完整 PDF 发完后不关连接，Chrome 会永远停在 100%（.crdownload 不落成 .pdf）。
// 所以按「文件大小稳定且尾部有 %%EOF」判定完成，不等下载事件收尾。
async function collectDownload(dir, before, startedAt, timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  let last = null;
  let stable = 0;
  while (Date.now() < deadline) {
    const fresh = fs
      .readdirSync(dir)
      .filter((f) => /\.(crdownload|pdf)$/i.test(f) && !before.has(f))
      .map((f) => ({ f, st: fs.statSync(path.join(dir, f)) }))
      .filter((x) => x.st.mtimeMs >= startedAt - 3000)
      .sort((a, b) => b.st.mtimeMs - a.st.mtimeMs);
    const top = fresh[0];
    if (top) {
      if (last && last.f === top.f && last.size === top.st.size) stable++;
      else stable = 0;
      last = { f: top.f, size: top.st.size };
      if (stable >= 1) {
        const buf = fs.readFileSync(path.join(dir, top.f));
        if (buf.subarray(0, 5).toString("latin1") === "%PDF-" &&
            buf.subarray(-2048).toString("latin1").includes("%%EOF")) {
          return { buf, file: top.f };
        }
      }
    }
    await sleep(1000);
  }
  return null;
}

export async function runSpringerProxy({
  tab,
  targetsPath,
  statePath,
  pdfDir,
  limit = 0,
  delayMs = 8000,
  timeoutMs = 60000,
}) {
  const targets = JSON.parse(fs.readFileSync(targetsPath, "utf8"));
  const state = fs.existsSync(statePath) ? JSON.parse(fs.readFileSync(statePath, "utf8")) : {};
  const save = () => fs.writeFileSync(statePath, JSON.stringify(state, null, 1));
  const stats = { processed: 0, downloaded: 0, failed: 0, skipped: 0 };

  for (const tg of (limit ? targets.slice(0, limit) : targets)) {
    const dest = path.join(pdfDir, `${tg.pmid}.pdf`);
    if (fs.existsSync(dest) && fs.statSync(dest).size > 10240) {
      state[tg.pmid] = { status: "exists", bytes: fs.statSync(dest).size };
      stats.skipped++;
      continue;
    }
    if (state[tg.pmid]?.status === "no_download") {
      stats.skipped++;
      continue;
    }
    const dlDir = path.join(os.homedir(), "Downloads");
    const before = new Set(fs.readdirSync(dlDir));
    const startedAt = Date.now();
    // 必须 await goto：不 await 时浏览器侧根本不发请求。
    // 导航到 PDF 会以 ERR_ABORTED 结束，这是正常的。
    await tab.goto(PROXY + tg.doi + ".pdf", { waitUntil: "domcontentloaded", timeoutMs }).catch(() => {});
    const got = await collectDownload(dlDir, before, startedAt, timeoutMs);
    if (!got) {
      state[tg.pmid] = { status: "no_download" };
      stats.failed++;
    } else {
      fs.writeFileSync(dest, got.buf);
      fs.unlinkSync(path.join(dlDir, got.file));
      state[tg.pmid] = { status: "downloaded", bytes: got.buf.length };
      stats.downloaded++;
    }
    stats.processed++;
    save();
    await sleep(delayMs);
  }
  state.__meta = { ...stats, at: new Date().toISOString() };
  save();
  return stats;
}

// 走「章节页 → 点页面上的 Download PDF」：直拼 /content/pdf/ 会丢 x-chain-id 授权。
export async function runSpringerClick({
  tab,
  targetsPath,
  statePath,
  pdfDir,
  limit = 0,
  delayMs = 4000,
  timeoutMs = 40000,
}) {
  const targets = JSON.parse(fs.readFileSync(targetsPath, "utf8"));
  const state = fs.existsSync(statePath) ? JSON.parse(fs.readFileSync(statePath, "utf8")) : {};
  const save = () => fs.writeFileSync(statePath, JSON.stringify(state, null, 1));
  const stats = { processed: 0, downloaded: 0, failed: 0, skipped: 0 };
  const dlDir = path.join(os.homedir(), "Downloads");

  for (const tg of (limit ? targets.slice(0, limit) : targets)) {
    const dest = path.join(pdfDir, `${tg.pmid}.pdf`);
    if (fs.existsSync(dest) && fs.statSync(dest).size > 10240) {
      state[tg.pmid] = { status: "exists", bytes: fs.statSync(dest).size };
      stats.skipped++;
      continue;
    }
    if (state[tg.pmid]?.status === "no_chapter") {
      stats.skipped++;
      continue;
    }
    const before = new Set(fs.readdirSync(dlDir));
    const startedAt = Date.now();
    await tab.goto(CHAPTER + tg.doi, { waitUntil: "domcontentloaded", timeoutMs }).catch(() => {});
    await sleep(1500);
    const ax = await tab.ax.get();
    const text = String(ax?.value ?? ax);
    const line = text.split("\n").find((l) => /link\s+.*(download|view).*pdf/i.test(l));
    const idx = line ? parseInt(line.trim(), 10) : NaN;
    if (!Number.isFinite(idx)) {
      state[tg.pmid] = { status: "no_chapter" };
      stats.failed++;
    } else {
      await tab.ax.click(idx).catch(() => {});
      const got = await collectDownload(dlDir, before, startedAt, timeoutMs);
      if (!got) {
        state[tg.pmid] = { status: "no_download" };
        stats.failed++;
      } else {
        fs.writeFileSync(dest, got.buf);
        fs.unlinkSync(path.join(dlDir, got.file));
        state[tg.pmid] = { status: "downloaded", bytes: got.buf.length };
        stats.downloaded++;
      }
    }
    stats.processed++;
    save();
    await sleep(delayMs);
  }
  state.__meta = { ...stats, at: new Date().toISOString() };
  save();
  return stats;
}
