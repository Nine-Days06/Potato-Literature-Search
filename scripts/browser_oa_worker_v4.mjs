import fs from "node:fs/promises";

// OA 补全 worker v4 —— 用内置浏览器（iab）为仍缺全文的文献抓 OA PDF
// 为什么不用 v3 的 Network 抓包：iab 会间歇性拒绝 Network 域
//   （"browser security check was unavailable"），响应体拿不到。
// v4 改为「页面内同源 fetch」直接取原始字节：goto(候选URL) → Runtime.evaluate 里 fetch →
//   content-type 是 application/pdf 且前 4 字节 %PDF 就落盘。拿到的是出版方原始 PDF，
//   不会像 printToPDF 那样把摘要页/cookie 墙当成"全文"。
// printToPDF 仅作兜底：只有整页词数达标（页面本身就是全文）才打印。
// 状态逐条落盘（可重入）；tab 按 worker 复用，避免反复 tabs.new 触发上下文失效。

const MIN_VALID_BYTES = 10240;

function validPdf(buf) {
  return !!buf && buf.length >= MIN_VALID_BYTES && buf.subarray(0, 5).toString("latin1") === "%PDF-";
}

async function readJson(path, fallback) {
  try { return JSON.parse(await fs.readFile(path, "utf8")); } catch { return fallback; }
}
async function writeJson(path, data) {
  await fs.writeFile(path, JSON.stringify(data), "utf8");
}

const CHALLENGE_MARKERS = ["just a moment", "attention required", "正在检查您的浏览器", "checking your browser", "enable javascript and cookies", "px-captcha", "captcha", "请稍候", "access denied"];

// 订阅墙：浏览器过不去，直链也取不到字节，不做无效尝试
const BLOCKED_HOSTS = /(sciencedirect\.com|wiley\.com|onlinelibrary\.wiley\.com|tandfonline\.com|cell\.com|apsnet\.org|plantmanagement\.network|annals\.org|ascb\.org|pubsonline\.pnas\.org|nature\.com|link\.springer\.com|karger\.com|ingentaconnect\.com|ashpublications\.org)$/;

function scoreUrl(u) {
  let h = "";
  try { h = new URL(u).hostname; } catch { return 99; }
  if (BLOCKED_HOSTS.test(h)) return 90;
  if (/europepmc\.org\/api\/getPdf/.test(u)) return 0;
  if (/\.pdf($|\?|#)|\.full\.pdf|pdf=render|citation_pdf|download.*pdf|\/doi\/pdf\//i.test(u)) return 1;
  if (h === "europepmc.org") return 2;
  if (/(core\.ac\.uk|citeseerx|figshare|zenodo|dspace|hdl\.handle\.net)/.test(h)) return 3;
  if (/(repository|ir\.|pure\.|research\.|openagrar|koreascience|scielo|doaj|alice|embrapa|mdpi\.com|frontiersin|plos\.org|dmd\.aspetjournals)/.test(h)) return 4;
  if (/ncbi\.nlm\.nih\.gov/.test(h)) return 8; // PMC 网页有 reCAPTCHA
  return 6; // doi.org 等落地页
}

// 候选 = {url: 要取字节的地址, anchor: 用来发同源 fetch 的 HTML 页}
// anchor 的存在是因为 Chromium 打开 PDF 文档页后没有可脚本的 JS world，
// Runtime.evaluate 在 PDF 页上取不到返回值；必须在同源的 HTML 页里发 fetch。
// EuropePMC 既有全文 HTML（/articles/PMCID）又能在该 origin 下 fetch /api/getPdf，
// 所以 pmc_id 已知时优先它：一次导航同时拿到 fetch 直取与 printToPDF 兜底。
function candidatesFor(target) {
  const out = [];
  if (target.pmc_id) {
    out.push({
      url: "https://europepmc.org/api/getPdf?pmcid=" + target.pmc_id,
      anchor: "https://europepmc.org/articles/" + target.pmc_id,
    });
    out.push({ url: "https://europepmc.org/articles/" + target.pmc_id + "?pdf=render" });
  }
  for (const u of target.urls || []) out.push({ url: u });
  const seen = new Set();
  return out
    .filter((c) => {
      const s = scoreUrl(c.url);
      if (s >= 90 || seen.has(c.url)) return false;
      seen.add(c.url);
      c.s = s;
      return true;
    })
    .sort((a, b) => a.s - b.s)
    .slice(0, 4);
}

const PAGE_INFO_EXPR = `(function(){
  var t = document.body ? document.body.innerText : "";
  var words = (t.match(/[A-Za-z\\u00c0-\\u017f]+/g) || []).length;
  var pdfs = [];
  try {
    pdfs = Array.prototype.slice.call(document.querySelectorAll("a[href]")).map(function(a){return a.href})
      .filter(function(h){ return /\\.pdf($|[?#])|pdf=render|citation_pdf|download\\/pdf|\\/doi\\/pdf\\//i.test(h) })
      .filter(function(h,i,arr){ return arr.indexOf(h) === i }).slice(0, 6);
  } catch (e) {}
  return JSON.stringify({ title: document.title, words: words, len: t.length, url: location.href, pdfs: pdfs, ct: document.contentType });
})()`;

// 先 HEAD 探 content-type，非 PDF 立刻放弃，避免把大页面 base64 化撑爆 IPC
const FETCH_PDF_EXPR = (url) => `(async()=>{
  try{
    var url=${JSON.stringify(url)};
    var probe=await fetch(url,{method:'HEAD',credentials:'omit'});
    var ct=(probe.headers.get('content-type')||'').toLowerCase();
    var len=Number(probe.headers.get('content-length')||0);
    if(!(probe.ok&&(ct.indexOf('application/pdf')>=0||(len>10240&&ct.indexOf('text/html')<0&&ct.indexOf('text/plain')<0))))return JSON.stringify({skip:1,status:probe.status,ct:ct});
    var r=await fetch(url,{credentials:'omit'});
    var b=await r.arrayBuffer(); var u=new Uint8Array(b);
    if(u.length<4096)return JSON.stringify({skip:1,reason:'tiny',bytes:u.length});
    if(!(u[0]===37&&u[1]===80&&u[2]===68&&u[3]===70))return JSON.stringify({skip:1,reason:'not_pdf',bytes:u.length});
    var bin=''; var CH=16384;
    for(var i=0;i<u.length;i+=CH){bin+=String.fromCharCode.apply(null,u.subarray(i,i+CH));}
    return JSON.stringify({ct:r.headers.get('content-type'),bytes:u.length,b64:btoa(bin)});
  }catch(e){return JSON.stringify({err:String(e).slice(0,200)});}
})()`;

export async function runOaRound2({ browserManager, targetsPath, statePath, pdfDir, maxPriority = 5, concurrency = 2, timeoutMs = 30000, minWords = 4000, offset = 0, limit = 0, chunkLabel = "" }) {
  const allTargets = await readJson(targetsPath, []);
  const saved = await readJson(statePath, {});
  const state = { ...saved };
  delete state._meta;
  const meta = { status: "running", chunk: chunkLabel, startedAt: new Date().toISOString(), processed: 0, success: 0, failed: 0, skipped: 0, total: 0, log: [] };
  const flush = async () => {
    const vals = Object.values(state).filter((s) => s && s.status);
    meta.success = vals.filter((s) => s.status === "downloaded").length;
    meta.failed = vals.filter((s) => s.status === "failed").length;
    meta.processedAt = new Date().toISOString();
    await writeJson(statePath, { ...state, _meta: meta });
  };
  const pushLog = (line) => { meta.log.push(line); if (meta.log.length > 60) meta.log.shift(); };

  // tabs.new 之后的所有 CDP 调用都要求先读过 confirmations 文档
  await browserManager.documentation.get("confirmations").catch(() => {});
  const browser = await browserManager.browsers.getDefault();

  const pending = allTargets
    .filter((t) => (t.priority ?? 30) <= maxPriority)
    .sort((a, b) => (a.priority ?? 30) - (b.priority ?? 30))
    .filter((t) => {
      const st = state[t.pmid];
      if (!st) return true;
      if (st.status === "downloaded") return false;
      return st.reason !== "preexisting";
    });
  const slice = limit > 0 ? pending.slice(offset, offset + limit) : pending.slice(offset);
  meta.total = slice.length;
  meta.retryable = [];
  await flush();

  const existingFile = async (pmid) => {
    for (const ext of [".pdf", ".xml", ".txt"]) {
      try {
        const st = await fs.stat(pdfDir + "/" + pmid + ext);
        if (st.size > MIN_VALID_BYTES) return ext;
      } catch {}
    }
    return null;
  };

  async function newTab() {
    const tab = await browser.tabs.new();
    const cdp = await tab.capabilities.get("cdp");
    await cdp.documentation();
    return { tab, cdp };
  }

  async function infoOf(cdp) {
    try {
      const r = await cdp.send("Runtime.evaluate", { expression: PAGE_INFO_EXPR, returnByValue: true });
      return JSON.parse(r.result?.value || "{}");
    } catch { return { title: "", words: 0, len: 0, url: "", pdfs: [], ct: "" }; }
  }

  // 在 anchor（同源 HTML 页）或目标 URL 自身所在页面里发 fetch
  async function grabPdf(ctx, cand) {
    const url = cand.url;
    const startUrl = cand.anchor || url;
    try { await ctx.tab.goto(startUrl, { waitUntil: "domcontentloaded", timeoutMs }); } catch {}
    let raw = null, v = null, lastErr = "";
    // iab 的 raw CDP 会间歇性不可用（"Browser Use is resolving a pause..."），与页面无关，
    // 重发同一表达式即可恢复，所以这里做退避重试而不是直接放弃该候选。
    for (let try_ = 0; try_ < 4; try_++) {
      raw = await ctx.cdp.send("Runtime.evaluate", { expression: FETCH_PDF_EXPR(url), returnByValue: true, awaitPromise: true })
        .catch((e) => ({ sendError: String(e?.message || e) }));
      lastErr = raw?.sendError || "";
      if (raw?.result?.value != null) break;
      await new Promise((r) => setTimeout(r, 1200 * (try_ + 1)));
    }
    try { v = JSON.parse(raw?.result?.value || "{}"); } catch {}
    if (v?.b64) {
      const buf = Buffer.from(v.b64, "base64");
      if (validPdf(buf)) return { ok: true, kind: "fetch", buf, url };
      return { ok: false, reason: "invalid_pdf", size: v.bytes, url };
    }
    const info = await infoOf(ctx.cdp);
    const titleLow = (info.title || "").toLowerCase();
    if (CHALLENGE_MARKERS.some((m) => titleLow.includes(m))) return { ok: false, reason: "challenge", url };
    if (v?.skip) return { ok: false, reason: "not_pdf", status: v.status, ct: v.ct, url };
    if (v?.err) return { ok: false, reason: "fetch_error", message: v.err, url };
    return { ok: false, reason: lastErr ? "eval_error" : info.len ? "no_pdf_bytes" : "empty_page",
      message: lastErr.slice(0, 200), url };
  }

  // 兜底打印：只有整页确实是全文才打，否则宁可失败
  async function tryPrint(ctx) {
    const info = await infoOf(ctx.cdp);
    const fulltextUrl = /(\/articles\/|\/doi\/full|\/pmc\/articles\/|\.full(\.pdf)?($|\?|#)|pdf=render)/i.test(info.url || "");
    if (!(info.words >= minWords || (fulltextUrl && info.words >= 900))) return { ok: false, reason: "below_minwords", words: info.words };
    try {
      const r = await ctx.cdp.send("Page.printToPDF", { printBackground: true, preferCSSPageSize: true });
      const buf = r && r.data ? Buffer.from(r.data, "base64") : null;
      if (validPdf(buf)) return { ok: true, kind: "print", buf, url: info.url };
      return { ok: false, reason: "print_invalid" };
    } catch (e) { return { ok: false, reason: "print_error", message: String(e?.message || e).slice(0, 120) }; }
  }

  async function attempt(ctx, cand) {
    const first = await grabPdf(ctx, cand);
    if (first.ok || first.reason === "challenge") return first;
    // anchor 页通常就是全文 HTML，取不到 PDF 字节时才允许打印兜底
    const printed = await tryPrint(ctx);
    if (printed.ok) return printed;
    const info = await infoOf(ctx.cdp);
    for (const purl of (info.pdfs || []).slice(0, 2)) {
      if (purl === cand.url || scoreUrl(purl) >= 90) continue;
      const second = await grabPdf(ctx, { url: purl });
      if (second.ok) return second;
    }
    return { ...first, printReason: printed.reason, words: info.words };
  }

  async function processTarget(ctx, target) {
    const pmid = target.pmid;
    const have = await existingFile(pmid);
    if (have) {
      state[pmid] = { status: "downloaded", kind: "preexisting", ext: have, ts: new Date().toISOString() };
      meta.skipped++;
      pushLog(pmid + " preexisting " + have);
      return;
    }
    const urls = candidatesFor(target);
    if (!urls.length) {
      state[pmid] = { status: "failed", reason: "no_url", priority: target.priority, ts: new Date().toISOString() };
      pushLog(pmid + " no_url");
      return;
    }
    let last = null;
    for (const cand of urls) {
      const res = await attempt(ctx, cand);
      if (res.ok) {
        await fs.writeFile(pdfDir + "/" + pmid + ".pdf", res.buf);
        state[pmid] = { status: "downloaded", kind: res.kind, size: res.buf.length, url: res.url, ts: new Date().toISOString() };
        pushLog(pmid + " OK " + res.kind + " " + Math.round(res.buf.length / 1024) + "KB");
        return;
      }
      last = res;
      if (res.reason === "challenge") break;
    }
    const transient = last?.reason === "eval_error" || last?.reason === "empty_page";
    state[pmid] = { status: "failed", reason: last?.reason || "unknown", detail: last?.printReason, firstDetail: last?.firstDetail, message: last?.message, words: last?.words, url: last?.url, ts: new Date().toISOString(), transient: transient || undefined };
    if (transient) meta.retryable.push(pmid);
    pushLog(pmid + " fail " + (last?.reason || "unknown") + (last?.printReason ? "/" + last.printReason : ""));
  }

  let idx = 0;
  async function workerLoop(workerId) {
    let ctx;
    try {
      ctx = await newTab();
    } catch (e) {
      pushLog("w" + workerId + " tab-open-fail " + String(e?.message || e).slice(0, 120));
      return;
    }
    while (idx < slice.length) {
      const target = slice[idx++];
      try {
        await processTarget(ctx, target);
      } catch (err) {
        state[target.pmid] = { status: "failed", reason: "worker_error", message: String(err?.message || err).slice(0, 200), ts: new Date().toISOString() };
        pushLog(target.pmid + " ERR " + String(err?.message || err).slice(0, 120));
      }
      meta.processed++;
      await flush();
    }
    await ctx.tab.close().catch(() => {});
  }

  const workers = [];
  for (let i = 0; i < concurrency; i++) workers.push(workerLoop(i));
  const results = await Promise.allSettled(workers);
  meta.errors = results.filter((r) => r.status === "rejected").map((r) => String(r.reason?.message || r.reason).slice(0, 200));
  meta.status = "finished";
  meta.finishedAt = new Date().toISOString();
  await flush();
  return { processed: meta.processed, success: meta.success, failed: meta.failed, total: meta.total, errors: meta.errors };
}
