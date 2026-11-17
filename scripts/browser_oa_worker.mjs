import fs from "node:fs/promises";

// OA Round2 worker v3: 内置浏览器（iab）补全仍缺失的 OA 文献 → data/pdfs_relevant/<pmid>.pdf
// 路径：goto → Network 抓 application/pdf 响应体（失败则 Page.getResourceContent）；
//       HTML 页若判定是全文（词数达标）则 Page.printToPDF；否则跟页面里的 PDF 链接再试一层。
// 状态逐条落盘（data/output/cache/browser_oa_round2_state.json），内核重启可续跑。

function validPdf(buf) {
  if (!buf || buf.length < 10240) return false;
  const head = buf.subarray(0, 5).toString("latin1");
  return head.startsWith("%PDF-");
}

async function readJson(path, fallback) {
  try { return JSON.parse(await fs.readFile(path, "utf8")); } catch { return fallback; }
}

async function writeJson(path, data) {
  await fs.writeFile(path, JSON.stringify(data), "utf8");
}

const CHALLENGE_MARKERS = ["just a moment", "attention required", "正在检查您的浏览器", "checking your browser", "enable javascript and cookies", "px-captcha", "captcha", "请稍候", "access denied"];

// 有 PDF 直链但取不到字节、或打印只剩 cookie 墙的站点，浏览器同样过不去
const BLOCKED_HOSTS = /(sciencedirect\.com|wiley\.com|tandfonline\.com|cell\.com|apsnet\.org|annals\.org|ascb\.org|pubsonline\.pnas\.org|nature\.com|link\.springer\.com)$/;

function scoreUrl(u) {
  let h = "";
  try { h = new URL(u).hostname; } catch { return 99; }
  if (BLOCKED_HOSTS.test(h)) return 90;
  if (/\.pdf($|\?|#)|\.full\.pdf|pdf=render|citation_pdf|download.*pdf/i.test(u)) return 0;
  if (h === "europepmc.org") return 1;
  if (/(core\.ac\.uk|citeseerx|figshare|zenodo|dspace|hdl\.handle\.net)/.test(h)) return 2;
  if (/(repository|ir\.|pure\.|research\.|openagrar|koreascience|scielo|doaj|alice|embrapa)/.test(h)) return 3;
  if (h === "www.ncbi.nlm.nih.gov" || h === "pmc.ncbi.nlm.nih.gov") return 9; // PMC 页面有 reCAPTCHA
  return 5; // doi.org 等落地页：能解析出全文就用
}

function pickUrls(target) {
  return [...(target.urls || [])]
    .map((u) => ({ u, s: scoreUrl(u) }))
    .filter((x) => x.s < 90)
    .sort((a, b) => a.s - b.s)
    .slice(0, 3)
    .map((x) => x.u);
}

const PAGE_INFO_EXPR = `(function(){
  var t = document.body ? document.body.innerText : "";
  var words = (t.match(/[A-Za-z\\u00c0-\\u017f]+/g) || []).length;
  var pdfs = [];
  try {
    pdfs = Array.prototype.slice.call(document.querySelectorAll("a[href]")).map(function(a){return a.href})
      .filter(function(h){ return /\\.pdf($|[?#])|pdf=render|citation_pdf|download\\/pdf|\\/pdf$/i.test(h) })
      .filter(function(h,i,arr){ return arr.indexOf(h) === i }).slice(0, 4);
  } catch (e) {}
  return JSON.stringify({ title: document.title, words: words, len: t.length, url: location.href, pdfs: pdfs });
})()`;

export async function runOaRound2({ browserManager, targetsPath, statePath, pdfDir, maxPriority = 5, concurrency = 2, delayMs = 6000, timeoutMs = 45000, minWords = 4000 }) {
  const allTargets = await readJson(targetsPath, []);
  const saved = await readJson(statePath, {});
  const state = { ...saved };
  delete state._meta;
  const meta = { status: "running", startedAt: new Date().toISOString(), processed: 0, success: 0, failed: 0, skipped: 0, log: [] };
  const flush = async () => {
    meta.success = Object.values(state).filter((s) => s && s.status === "downloaded").length;
    meta.failed = Object.values(state).filter((s) => s && s.status === "failed").length;
    meta.processedAt = new Date().toISOString();
    await writeJson(statePath, { ...state, _meta: meta });
  };
  const pushLog = (line) => { meta.log.push(line); if (meta.log.length > 80) meta.log.shift(); };
  await flush();

  // CDP 调用（tab_cdp_call/tab_cdp_events）要求先读过 confirmations 文档，一次即可
  await browserManager.documentation.get("confirmations").catch(() => {});

  const pending = allTargets
    .filter((t) => (t.priority ?? 30) <= maxPriority)
    .sort((a, b) => (a.priority ?? 30) - (b.priority ?? 30))
    .filter((t) => {
      const st = state[t.pmid];
      if (!st) return true;
      if (st.status === "downloaded") return false;
      return !["preexisting"].includes(st.reason);
    });

  const browser = await browserManager.browsers.getDefault();

  const existingFile = async (pmid) => {
    for (const ext of [".pdf", ".xml", ".txt"]) {
      try {
        const st = await fs.stat(pdfDir + "/" + pmid + ext);
        if (st.size > 10240) return ext;
      } catch {}
    }
    return null;
  };

  async function fetchBody(cdp, requestId) {
    try {
      const r = await cdp.send("Network.getResponseBody", { requestId });
      if (r.base64Encoded) return Buffer.from(r.body, "base64");
      return Buffer.from(r.body || "", "latin1");
    } catch { return null; }
  }

  async function fetchResource(cdp, url) {
    try {
      const tree = await cdp.send("Page.getFrameTree", {});
      const frameId = tree.frameTree?.frame?.id;
      if (!frameId) return null;
      const r = await cdp.send("Page.getResourceContent", { frameId, requestUrl: url });
      if (r.base64Encoded) return Buffer.from(r.content, "base64");
      return Buffer.from(r.content || "", "latin1");
    } catch { return null; }
  }

  async function pageInfo(cdp) {
    try {
      const r = await cdp.send("Runtime.evaluate", { expression: PAGE_INFO_EXPR, returnByValue: true });
      return JSON.parse(r.result?.value || "{}");
    } catch { return { title: "", words: 0, len: 0, url: "", pdfs: [] }; }
  }

  async function attempt(url, follow = true) {
    const tab = await browser.tabs.new();
    let u0 = url;
    if (/europepmc\.org\/(pmc\/)?articles\//.test(u0) && !/pdf=render/.test(u0)) {
      u0 += (u0.includes("?") ? "&" : "?") + "pdf=render";
    }
    try { await tab.goto(u0, { waitUntil: "domcontentloaded", timeoutMs: 30000 }); } catch {}
    let cdp = null;
    try {
      cdp = await tab.capabilities.get("cdp");
      await cdp.documentation();
      await cdp.send("Network.enable", {});
      let cursor = (await cdp.readEvents({ limit: 1 })).cursor;
      // 若首屏已经出现 PDF 响应，先按资源内容取一次
      let info = await pageInfo(cdp);
      if (/\.pdf($|\?|#)|pdf=render/i.test(info.url || "")) {
        for (const r of await pdfResponses(cdp, cursor, 4000)) {
          const buf = r.buf;
          if (buf) return { ok: true, kind: r.kind, buf, url: info.url };
        }
      } else {
        await tab.reload({ waitUntil: "domcontentloaded" }).catch(() => {});
      }
      const deadline = Date.now() + timeoutMs;
      let printCandidate = false;
      let challenge = false;
      let pdfSeen = false;
      let pdfLinks = [];
      while (Date.now() < deadline) {
        const found = await pdfResponses(cdp, cursor, 0);
        cursor = found.cursor;
        for (const r of found.list) {
          if (r.buf) return { ok: true, kind: r.kind, buf: r.buf, url: r.url || u0 };
          pdfSeen = true;
        }
        info = await pageInfo(cdp);
        const titleLow = (info.title || "").toLowerCase();
        challenge = CHALLENGE_MARKERS.some((m) => titleLow.includes(m));
        pdfLinks = info.pdfs || [];
        const fulltextUrl = /(\/articles\/|\/doi\/full|\/pmc\/articles\/|\.full(\.pdf)?($|\?|#)|pdf=render)/i.test(info.url || "");
        if (info.words >= minWords || (fulltextUrl && info.words >= 900)) { printCandidate = true; break; }
        if (challenge && Date.now() + 15000 > deadline) break;
        await new Promise((r) => setTimeout(r, Math.min(delayMs, 3000)));
      }
      if (printCandidate) {
        const r = await cdp.send("Page.printToPDF", { printBackground: true, preferCSSPageSize: true });
        const buf = r && r.data ? Buffer.from(r.data, "base64") : null;
        if (validPdf(buf)) return { ok: true, kind: "print", buf, url: info.url || u0 };
      }
      if (follow && pdfLinks.length) {
        for (const purl of pdfLinks.slice(0, 2)) {
          if (purl === u0) continue;
          const res = await attempt(purl, false);
          if (res.ok) return res;
        }
        return { ok: false, reason: "pdf_link_failed", url: pdfLinks[0] };
      }
      if (challenge) return { ok: false, reason: "challenge", url: u0 };
      if (pdfSeen) return { ok: false, reason: "pdf_body_unavailable", url: u0 };
      return { ok: false, reason: info.words ? "print_failed" : "no_fulltext", url: u0 };
    } catch (err) {
      return { ok: false, reason: "error", message: String(err?.message || err).slice(0, 200), url: u0 };
    } finally {
      await new Promise((r) => setTimeout(r, 200));
      await tab.close().catch(() => {});
    }
  }

  async function pdfResponses(cdp, afterSequence, waitMs) {
    const evts = await cdp.readEvents({ afterSequence, methods: ["Network.responseReceived"], limit: 100, ...(waitMs ? { timeoutMs: waitMs } : {}) }).catch(() => null);
    if (!evts) return { cursor: afterSequence, list: [] };
    const list = [];
    for (const evt of evts.events) {
      const headers = evt.params?.response?.headers || {};
      const ct = (headers["content-type"] || headers["Content-Type"] || "").toLowerCase();
      const rurl = evt.params?.response?.url || "";
      if (!ct.includes("application/pdf") && !/\.pdf($|\?|#)|pdf=render/i.test(rurl)) continue;
      let buf = await fetchBody(cdp, evt.params.requestId);
      let kind = "network";
      if (!validPdf(buf)) {
        buf = await fetchResource(cdp, rurl);
        kind = "resource";
      }
      list.push({ kind, buf: validPdf(buf) ? buf : null, url: rurl });
    }
    return { cursor: evts.cursor, list };
  }

  async function processTarget(target) {
    const pmid = target.pmid;
    const have = await existingFile(pmid);
    if (have) {
      state[pmid] = { status: "downloaded", kind: "preexisting", ext: have, ts: new Date().toISOString() };
      meta.skipped++;
      pushLog(pmid + " preexisting " + have);
      return;
    }
    const urls = pickUrls(target);
    if (!urls.length) {
      state[pmid] = { status: "failed", reason: "no_url", priority: target.priority, ts: new Date().toISOString() };
      pushLog(pmid + " no_url");
      return;
    }
    let last = null;
    for (const url of urls) {
      const res = await attempt(url);
      if (res.ok) {
        const file = pdfDir + "/" + pmid + ".pdf";
        await fs.writeFile(file, res.buf);
        state[pmid] = { status: "downloaded", kind: res.kind, size: res.buf.length, url: res.url, ts: new Date().toISOString() };
        pushLog(pmid + " OK " + res.kind + " " + Math.round(res.buf.length / 1024) + "KB");
        return;
      }
      last = res;
      state[pmid] = { status: "failed", reason: res.reason, message: res.message, url: res.url, ts: new Date().toISOString() };
      if (res.reason === "challenge") break;
    }
    pushLog(pmid + " fail " + (last && last.reason));
  }

  let idx = 0;
  async function workerLoop() {
    while (idx < pending.length) {
      const target = pending[idx++];
      try {
        await processTarget(target);
      } catch (err) {
        state[target.pmid] = { status: "failed", reason: "worker_error", message: String(err?.message || err).slice(0, 200), ts: new Date().toISOString() };
        pushLog(target.pmid + " ERR " + String(err?.message || err).slice(0, 120));
      }
      meta.processed++;
      await flush();
    }
  }

  const workers = [];
  for (let i = 0; i < concurrency; i++) workers.push(workerLoop());
  const results = await Promise.allSettled(workers);
  meta.errors = results.filter((r) => r.status === "rejected").map((r) => String(r.reason?.message || r.reason).slice(0, 200));
  meta.status = "finished";
  meta.finishedAt = new Date().toISOString();
  await flush();
  return meta;
}
