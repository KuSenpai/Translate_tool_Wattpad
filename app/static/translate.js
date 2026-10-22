// AI translation screen: upload a Korean novel → choose model/range/style → background job → bilingual file.
const $ = (s) => document.querySelector(s);
const esc = (t) => String(t ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const T = { info: null, source: null, pollTimer: null };

async function api(method, url, body, isForm = false) {
  const res = await fetch(url, {
    method, headers: body && !isForm ? { "Content-Type": "application/json" } : {},
    body: isForm ? body : body ? JSON.stringify(body) : undefined,
  });
  let data = null;
  try { data = await res.json(); } catch { /* empty */ }
  if (!res.ok) throw Object.assign(new Error(data?.error?.message || `HTTP ${res.status}`), { code: data?.error?.code });
  return data;
}
function toast(msg, kind = "") {
  const t = $("#toast");
  t.textContent = msg; t.className = `toast ${kind}`;
  clearTimeout(toast._t);
  toast._t = setTimeout(() => t.classList.add("hidden"), kind === "error" ? 9000 : 4000);
}
const fail = (e) => toast(`${e.code ? `[${e.code}] ` : ""}${e.message}`, "error");
const usd = (n) => (n == null ? "—" : `$${Number(n).toFixed(2)}`);
const STATUS = { queued: "chờ", running: "đang dịch", pausing: "đang dừng…", paused: "tạm dừng", done: "xong", error: "lỗi" };

function showView() {
  document.querySelectorAll(".view").forEach((v) => v.classList.toggle("hidden", v.id !== "viewTranslate"));
  loadInfo(); loadJobs();
}

async function loadInfo() {
  try {
    T.info = await api("GET", "/api/translate/info");
    $("#trModel").innerHTML = T.info.models.map((m) => `<option value="${esc(m.id)}" ${m.id === T.info.default_model ? "selected" : ""}>${esc(m.label)}</option>`).join("");
    if (!$("#trStyle").value) $("#trStyle").value = T.info.style;
    renderAuth();
    const projects = await api("GET", "/api/projects");
    // one entry per story (all its files share one termbase) + files that belong to no story
    const seenStory = new Set();
    const opts = [];
    for (const p of projects) {
      if (p.story) {
        if (seenStory.has(p.story)) continue;
        seenStory.add(p.story);
        opts.push(`<option value="${esc(p.id)}" data-story="${esc(p.story)}">📚 Truyện: ${esc(p.story)}</option>`);
      } else {
        opts.push(`<option value="${esc(p.id)}">${esc(p.name)}</option>`);
      }
    }
    $("#trGlossary").innerHTML = `<option value="">— không —</option>` + opts.join("");
    const sources = await api("GET", "/api/translate/sources");
    $("#trSources").innerHTML = `<option value="">— chọn —</option>` + sources.map((s) =>
      `<option value="${esc(s.sid)}">${esc(s.name)} · ${s.todo} chương cần dịch</option>`).join("");
    $("#trSourcesRow").classList.toggle("hidden", !sources.length);
    if (T.source) updateCost();
  } catch (e) { fail(e); }
}

function setSource(meta) {
  T.source = meta;
  $("#trSetup").classList.remove("hidden");
  $("#trSourceTitle").textContent = `📄 ${meta.name}`;
  const nums = meta.chapters.filter((c) => c.lang === "ko").map((c) => c.number);
  $("#trSourceInfo").innerHTML = `${meta.chapters.length} phần được nhận diện · <b>${meta.todo}</b> chương tiếng Hàn cần dịch` +
    (meta.already_translated ? ` · ${meta.already_translated} chương đã có bản Việt (bỏ qua)` : "") +
    (nums.length ? ` · chương ${Math.min(...nums)}–${Math.max(...nums)}` : "") + ` · ${meta.chars.toLocaleString("vi-VN")} ký tự`;
  $("#trSourceWarn").innerHTML = meta.warnings.length
    ? `<div class="alert"><ul>${meta.warnings.map((w) => `<li>${esc(w)}</li>`).join("")}</ul></div>` : "";
  const g = [...$("#trGlossary").options].find((o) => o.textContent.replace(/\.(docx|txt)$/i, "") === meta.name.replace(/\.(docx|txt)$/i, ""))
    || [...$("#trGlossary").options].find((o) => o.dataset.story);   // raw prepared for a story → its termbase
  if (g && !$("#trGlossary").value) $("#trGlossary").value = g.value;
  updateCost();
}

function selectedChapters() {
  const from = $("#trFrom").value ? +$("#trFrom").value : -Infinity, to = $("#trTo").value ? +$("#trTo").value : Infinity;
  return (T.source?.chapters || []).filter((c) => c.lang === "ko" && !c.has_translation && c.number >= from && c.number <= to);
}
const curModel = () => T.info?.models.find((x) => x.id === $("#trModel").value) || {};

// Antigravity (Gemini) status: CLI installed? signed in (does `agy models` answer)? which model will be used?
function antigravityNote(ag, tier) {
  if (!ag.installed) {
    return `<div class="alert error">Chưa cài Antigravity CLI (<code>agy</code>). Mở PowerShell và chạy
      <code>${esc(ag.install_cmd || "irm https://antigravity.google/cli/install.ps1 | iex")}</code>, sau đó chạy <code>agy</code>
      một lần để đăng nhập Google (cùng tài khoản dùng Antigravity), rồi chọn lại model này.</div>`;
  }
  if (!ag.checked) return `<div class="alert">Đang kiểm tra Antigravity CLI…</div>`;
  if (!ag.models?.length) {
    return `<div class="alert error">Antigravity CLI chưa sẵn sàng${ag.error ? `: ${esc(ag.error)}` : ""}.
      Mở terminal, chạy <code>agy</code> một lần để đăng nhập Google, rồi <button class="btn small" id="trAgRecheck">Kiểm tra lại</button></div>`;
  }
  const slug = ag.picked?.[tier];
  return `<div class="alert">✔ Dịch bằng <b>Gemini qua Antigravity</b>${slug ? ` (<code>${esc(slug)}</code>)` : ""} — dùng hạn mức tài khoản
    Google của Antigravity, không tính tiền API. Luật dịch riêng cho Gemini: <code>app/ai/gemini_rules.md</code>.
    Khi hết hạn mức, job tự tạm dừng; bấm Tiếp tục khi hạn mức được làm mới.
    <span class="muted">Gemini có bộ lọc an toàn riêng — vài cảnh 18+ có thể bị chặn (chương báo AI_REFUSED, dịch lại bằng Claude).</span></div>`;
}

async function checkAntigravity(refresh = false) {
  T.info.antigravity = { ...(T.info.antigravity || {}), checked: false };
  renderAuth();
  try { T.info.antigravity = await api("GET", `/api/translate/antigravity${refresh ? "?refresh=1" : ""}`); }
  catch (e) { T.info.antigravity = { ...(T.info.antigravity || {}), checked: true, models: [], error: e.message }; }
  renderAuth();
}

// Grok (xAI): XAI_API_KEY set? do the configured model slugs exist for this key?
function grokNote(g, tier) {
  if (!g.has_key) {
    return `<div class="alert error">Cần <b>XAI_API_KEY</b> trong <code>.env</code> (lấy ở console.x.ai, nạp credit) rồi khởi động lại tool.</div>`;
  }
  const slug = g.picked?.[tier];
  const warn = g.error ? `<div class="alert error">${esc(g.error)}${g.models?.length ? ` Model có sẵn: <code>${esc(g.models.join(", "))}</code>` : ""}
      <button class="btn small" id="trGkRecheck">Kiểm tra lại</button></div>` : "";
  return `<div class="alert">✔ Dịch bằng <b>Grok (xAI)</b>${slug ? ` (<code>${esc(slug)}</code>)` : ""} — tính tiền theo token trên tài khoản xAI.
    Grok dịch được cảnh 18+; đổi model bằng <code>XAI_MODEL</code> / <code>XAI_MODEL_FAST</code> trong <code>.env</code>.</div>${warn}`;
}

async function checkGrok(refresh = false) {
  try { T.info.grok = { ...(T.info.grok || {}), ...await api("GET", `/api/translate/grok${refresh ? "?refresh=1" : ""}`) }; }
  catch (e) { T.info.grok = { ...(T.info.grok || {}), checked: true, error: e.message }; }
  renderAuth();
}

// Grok CLI (`grok` headless): installed? signed in to grok.com?
function grokCliNote(g) {
  if (!g.installed) {
    return `<div class="alert error">Chưa cài Grok CLI (<code>grok</code>). Mở <b>PowerShell</b> và chạy
      <code>${esc(g.install_cmd || "irm https://x.ai/cli/install.ps1 | iex")}</code>, sau đó chạy <code>grok</code> một lần để đăng nhập grok.com,
      rồi <button class="btn small" id="trGcRecheck">Kiểm tra lại</button></div>`;
  }
  if (!g.checked) return `<div class="alert">Đang kiểm tra Grok CLI…</div>`;
  if (!g.logged_in) {
    return `<div class="alert error">Grok CLI chưa sẵn sàng${g.error ? `: ${esc(g.error)}` : ""}
      <button class="btn small" id="trGcRecheck">Kiểm tra lại</button></div>`;
  }
  const accs = g.accounts || [];
  const accLine = accs.length > 1
    ? `<br>Tài khoản (xoay vòng, tự chuyển khi bị giới hạn): ${accs.map((a) => `<b>${esc(a.name)}</b> ${a.logged_in ? "✔" : `✖ <span class="muted">(${esc(a.error || "chưa đăng nhập")})</span>`}`).join(" · ")}`
    : "";
  return `<div class="alert">✔ Dịch bằng <b>Grok CLI</b> (${esc((g.models || []).join(", ") || "model mặc định")}) — dùng tài khoản grok.com đã đăng nhập,
    không cần API key. Grok dịch được cảnh 18+. Đổi model bằng <code>GROK_CLI_MODEL</code>; thêm tài khoản bằng <code>GROK_CLI_HOMES</code> trong <code>.env</code>.
    Hết hạn mức thì job tự tạm dừng; bấm Tiếp tục khi được làm mới.${accLine}</div>`;
}

async function checkGrokCli(refresh = false) {
  T.info.grok_cli = { ...(T.info.grok_cli || {}), checked: false };
  renderAuth();
  try { T.info.grok_cli = await api("GET", `/api/translate/grok-cli${refresh ? "?refresh=1" : ""}`); }
  catch (e) { T.info.grok_cli = { ...(T.info.grok_cli || {}), checked: true, logged_in: false, error: e.message }; }
  renderAuth();
}

// OmniRoute (local OpenAI-compatible gateway): key set? reachable? does OMNIROUTE_MODEL exist?
function omniNote(o) {
  if (!o.has_key) {
    return `<div class="alert error">Cần <b>OMNIROUTE_API_KEY</b> (và <b>OMNIROUTE_MODEL</b>) trong <code>.env</code>: tạo API key trong dashboard OmniRoute
      (<code>${esc((o.base_url || "http://localhost:20128/v1").replace(/[/]v1$/, ""))}</code>) rồi dán vào .env và khởi động lại tool.</div>`;
  }
  const list = o.models?.length ? ` Model có sẵn: <code>${esc(o.models.slice(0, 40).join(", "))}${o.models.length > 40 ? ", …" : ""}</code>` : "";
  if (o.checked && o.error) {
    return `<div class="alert error">${esc(o.error)}${list} <button class="btn small" id="trOmRecheck">Kiểm tra lại</button></div>`;
  }
  if (!o.model) return `<div class="alert error">Chưa đặt <b>OMNIROUTE_MODEL</b> trong <code>.env</code>.${list}</div>`;
  return `<div class="alert">✔ Dịch bằng <b>OmniRoute</b> (<code>${esc(o.model)}</code>) qua <code>${esc(o.base_url)}</code> — chi phí / hạn mức tính theo nhà cung cấp
    bạn đã nối trong OmniRoute. ${o.checked ? "" : `<button class="btn small" id="trOmRecheck">Kiểm tra kết nối</button>`}</div>`;
}

async function checkOmniroute(refresh = false) {
  try { T.info.omniroute = { ...(T.info.omniroute || {}), ...await api("GET", `/api/translate/omniroute${refresh ? "?refresh=1" : ""}`) }; }
  catch (e) { T.info.omniroute = { ...(T.info.omniroute || {}), checked: true, error: e.message }; }
  renderAuth();
}

// Which way of calling the AI is ready: API key (billed per token), Claude Code or Antigravity (subscription / quota).
function renderAuth() {
  const i = T.info, cc = i.claude_code || {};
  const m = curModel(), sub = m.subscription;
  if (i.provider === "mock") {
    $("#trKeyWarn").innerHTML = `<div class="alert">Đang ở chế độ thử (TRANSLATE_PROVIDER=mock): không gọi Claude, bản dịch là văn bản giả.</div>`;
  } else if (m.provider === "omniroute") {
    $("#trKeyWarn").innerHTML = omniNote(i.omniroute || {});
    $("#trOmRecheck")?.addEventListener("click", () => checkOmniroute(true));
  } else if (m.provider === "grok_cli") {
    $("#trKeyWarn").innerHTML = grokCliNote(i.grok_cli || {});
    $("#trGcRecheck")?.addEventListener("click", () => checkGrokCli(true));
  } else if (m.provider === "xai") {
    $("#trKeyWarn").innerHTML = grokNote(i.grok || {}, m.id.split(":")[1]);
    $("#trGkRecheck")?.addEventListener("click", () => checkGrok(true));
  } else if (m.provider === "antigravity") {
    $("#trKeyWarn").innerHTML = antigravityNote(i.antigravity || {}, m.id.split(":")[1]);
    $("#trAgRecheck")?.addEventListener("click", () => checkAntigravity(true));
  } else if (sub) {
    $("#trKeyWarn").innerHTML = cc.logged_in
      ? `<div class="alert">✔ Dùng <b>gói Claude của bạn</b> qua Claude Code (${esc(cc.auth_method || "")}) — trừ vào lượt dùng của gói,
          không tính tiền API. Khi hết lượt, job tự tạm dừng; bấm Tiếp tục khi gói được làm mới.</div>`
      : `<div class="alert error">${cc.installed ? "Claude Code chưa đăng nhập." : "Chưa cài Claude Code."}
          Mở terminal và chạy <code>${cc.installed ? "claude auth login --claudeai" : "npm install -g @anthropic-ai/claude-code"}</code>
          ${cc.installed ? "(đăng nhập bằng tài khoản Claude có gói Pro/Max)" : "rồi <code>claude auth login --claudeai</code>"}, sau đó bấm lại model này.</div>`;
  } else {
    $("#trKeyWarn").innerHTML = i.has_key ? "" : `<div class="alert error">Model này tính tiền API: cần <b>ANTHROPIC_API_KEY</b> trong <code>.env</code>
      (console.anthropic.com). Hoặc chọn <b>Claude Code · …</b> (gói Claude) <b>Antigravity · …</b> (Gemini) hay <b>Grok</b> (XAI_API_KEY hoặc Grok CLI) hay <b>OmniRoute</b>.</div>`;
  }
}

function updateCost() {
  if (!T.source || !T.info) return;
  const chs = selectedChapters();
  const chars = chs.reduce((s, c) => s + c.chars, 0);
  const m = T.info.models.find((x) => x.id === $("#trModel").value);
  renderAuth();
  if (m.provider === "omniroute") {
    $("#trCost").innerHTML = `${chs.length} chương · <b>chi phí tính theo nhà cung cấp trong OmniRoute</b> <span class="muted">(tool không ước tính được)</span>`;
    $("#trStart").disabled = !chs.length || T.info.omniroute?.has_key === false;
    return;
  }
  if (m.provider === "grok_cli") {
    $("#trCost").innerHTML = `${chs.length} chương · <b>dùng tài khoản grok.com trong Grok CLI</b> <span class="muted">(không tính tiền API; mỗi chương là một lượt dài)</span>`;
    $("#trStart").disabled = !chs.length || T.info.grok_cli?.installed === false;
    return;
  }
  if (m.provider === "antigravity") {
    $("#trCost").innerHTML = `${chs.length} chương · <b>dùng hạn mức Gemini của Antigravity</b> <span class="muted">(không tính tiền API;
      mỗi chương là một lượt dài)</span>`;
    $("#trStart").disabled = !chs.length || T.info.antigravity?.installed === false;
    return;
  }
  if (m.subscription) {
    $("#trCost").innerHTML = `${chs.length} chương · <b>dùng lượt của gói Claude</b> <span class="muted">(không tính tiền API;
      mỗi chương là một lượt dài — gói Pro hết lượt nhanh hơn Max)</span>`;
    $("#trStart").disabled = !chs.length;
    return;
  }
  const [pin, pout] = m.price;
  const tin = chars * 1.0 + chs.length * 2500, tout = chars * 1.6 + chs.length * 600;
  const cost = (tin * pin + tout * pout) / 1e6;
  $("#trCost").innerHTML = `${chs.length} chương · ước tính <b>~${usd(cost)}</b> <span class="muted">(±50%, chi phí thật hiện trong lúc dịch)</span>`;
  $("#trStart").disabled = !chs.length || (m.provider === "xai" && T.info.grok?.has_key === false);
}

async function analyzeFile(file) {
  if (!file) return;
  if (!/\.(txt|docx)$/i.test(file.name)) return toast("Chỉ hỗ trợ .txt hoặc .docx", "error");
  const fd = new FormData(); fd.append("file", file);
  toast(`Đang phân tích ${file.name}…`);
  try { setSource(await api("POST", "/api/translate/analyze", fd, true)); toast("Đã phân tích xong", "ok"); } catch (e) { fail(e); }
}

async function start() {
  const chs = selectedChapters();
  const model = $("#trModel").selectedOptions[0].textContent;
  const m = curModel();
  if (!confirm(`Dịch ${chs.length} chương bằng ${model}?\n${$("#trCost").innerText}\n\n${m.provider === "omniroute" ? "Sẽ gọi OmniRoute — chi phí tính theo các nhà cung cấp bạn đã nối trong đó."
    : m.provider === "grok_cli" ? "Sẽ dùng tài khoản grok.com đăng nhập trong Grok CLI — không tính tiền API."
    : m.provider === "antigravity"
    ? "Sẽ dùng hạn mức Gemini của tài khoản Google trong Antigravity — không tính tiền API."
    : m.provider === "xai" ? "Chi phí API sẽ tính vào tài khoản xAI (Grok) của bạn."
    : m.subscription ? "Sẽ dùng lượt của gói Claude (Claude Code) — không tính tiền API."
    : "Chi phí API sẽ tính vào tài khoản Anthropic của bạn."}`)) return;
  try {
    await api("POST", "/api/translate/jobs", {
      sid: T.source.sid, model: $("#trModel").value, effort: $("#trEffort").value, concurrency: +$("#trConc").value,
      from_number: $("#trFrom").value ? +$("#trFrom").value : null, to_number: $("#trTo").value ? +$("#trTo").value : null,
      glossary_project: $("#trGlossary").value || null, style: $("#trStyle").value,
    });
    toast("Đã bắt đầu dịch — có thể để chạy nền.", "ok");
    loadJobs();
  } catch (e) { fail(e); }
}

function bar(job) {
  const c = job.counts, n = job.total || 1;
  const seg = (k, color) => c[k] ? `<span style="width:${(100 * c[k]) / n}%;background:${color}"></span>` : "";
  return `<div class="tr-bar">${seg("done", "#2e8b57")}${seg("running", "#3b82f6")}${seg("failed", "#e57373")}</div>`;
}

async function loadJobs() {
  let jobs = [];
  try { jobs = await api("GET", "/api/translate/jobs"); } catch (e) { return fail(e); }
  const box = $("#trJobs");
  if (!jobs.length) { box.innerHTML = "Chưa có."; return; }
  const details = await Promise.all(jobs.slice(0, 8).map((j) => api("GET", `/api/translate/jobs/${j.id}`).catch(() => j)));
  box.innerHTML = details.map((j) => {
    const c = j.counts || {};
    const busy = j.running || j.status === "running" || j.status === "pausing";
    const out = j.output || {};
    return `<div class="tr-job" data-id="${esc(j.id)}">
      <div class="head"><span class="name">${esc(j.name)}</span>
        <span class="badge">${esc(STATUS[j.status] || j.status)}</span>
        <span class="muted">${esc(j.model)} · ${esc(j.effort)} · ${c.done || 0}/${j.total} chương${c.failed ? ` · <b style="color:#c0392b">${c.failed} lỗi</b>` : ""}
          · đã tốn ${usd(j.cost_usd)}${j.projected_cost_usd ? ` · dự kiến tổng ${usd(j.projected_cost_usd)}` : ""}</span></div>
      ${bar(j)}
      <div class="muted">${esc(j.message || "")}</div>
      <div class="row" style="margin-top:6px">
        ${busy ? `<button class="small" data-act="pause">⏸ Tạm dừng</button>` : ""}
        ${!busy && (c.pending || 0) > 0 ? `<button class="small primary" data-act="resume">▶ Tiếp tục</button>` : ""}
        ${!busy && c.failed ? `<button class="small" data-act="retry-failed">↻ Dịch lại chương lỗi</button>` : ""}
        <button class="small" data-act="build">💾 Xuất file</button>
        ${out.docx ? `<a class="small" href="/api/translate/jobs/${esc(j.id)}/download?fmt=docx">⬇ .docx</a>` : ""}
        ${out.txt ? `<a class="small" href="/api/translate/jobs/${esc(j.id)}/download?fmt=txt">⬇ .txt</a>` : ""}
        ${out.docx ? `<button class="small" data-act="open">✏ Mở trong editor</button>` : ""}
        ${j.glossary_project && Object.keys(j.auto_terms || {}).length ? `<button class="small" data-act="save-terms"
          title="Các tên/thuật ngữ mới AI gặp khi dịch → danh sách chờ duyệt trong 📚 Thuật ngữ">📚 Đưa ${Object.keys(j.auto_terms).length} thuật ngữ mới vào kho</button>` : ""}
        ${!busy ? `<button class="small ghost" data-act="delete">Xoá</button>` : ""}
      </div>
      ${out.docx ? `<div class="muted">File: ${esc(out.docx)}</div>` : ""}
      <div class="tr-chips">${(j.chapters || []).map((ch) => `<span class="tr-chip ${ch.status}" data-idx="${ch.idx}"
          title="${esc(ch.heading)}${ch.error ? "\n" + esc(ch.error) : ""}${ch.status === "failed" ? "\n(bấm để dịch lại)" : ""}">${ch.number}</span>`).join("")}</div>
    </div>`;
  }).join("");
  box.querySelectorAll("[data-act]").forEach((b) => b.addEventListener("click", () => act(b.closest(".tr-job").dataset.id, b.dataset.act)));
  box.querySelectorAll(".tr-chip.failed").forEach((ch) => ch.addEventListener("click", () =>
    act(ch.closest(".tr-job").dataset.id, "retry-chapter", ch.dataset.idx)));
  clearTimeout(T.pollTimer);
  if (details.some((j) => j.running || ["running", "pausing", "queued"].includes(j.status)) && !$("#viewTranslate").classList.contains("hidden")) {
    T.pollTimer = setTimeout(loadJobs, 2500);
  } else if (details.some((j) => j.auto_resume_at) && !$("#viewTranslate").classList.contains("hidden")) {
    T.pollTimer = setTimeout(loadJobs, 60000);   // waiting for a usage limit to reset: notice the automatic resume
  }
}

async function act(id, action, idx) {
  try {
    if (action === "delete") {
      if (!confirm("Xoá lượt dịch này? (file kết quả đã xuất trong thư mục output vẫn được giữ)")) return;
      await api("DELETE", `/api/translate/jobs/${id}`);
    } else if (action === "open") {
      const r = await api("POST", `/api/translate/jobs/${id}/open`);
      try { localStorage.setItem("lastProject", r.project_id); } catch { /* storage unavailable */ }
      location.reload();
      return;
    } else {
      const r = await api("POST", `/api/translate/jobs/${id}/${action}${idx != null ? `?idx=${idx}` : ""}`);
      if (action === "build") toast("Đã xuất file", "ok");
      if (action === "save-terms") toast(`Đã đưa ${r.added} thuật ngữ mới vào danh sách chờ duyệt (📚 Thuật ngữ)`, "ok");
    }
    loadJobs();
  } catch (e) { fail(e); }
}

$("#navTranslate").addEventListener("click", showView);
$("#trSources").addEventListener("change", async (e) => {
  if (!e.target.value) return;
  try { setSource(await api("GET", `/api/translate/sources/${e.target.value}`)); } catch (err) { fail(err); }
});
$("#trBack").addEventListener("click", () => {
  clearTimeout(T.pollTimer);
  document.querySelectorAll(".view").forEach((v) => v.classList.toggle("hidden", v.id !== "viewHome"));
});
$("#trFile").addEventListener("change", (e) => analyzeFile(e.target.files[0]));
const dz = $("#trDrop");
dz.addEventListener("dragover", (e) => { e.preventDefault(); dz.classList.add("drag"); });
dz.addEventListener("dragleave", () => dz.classList.remove("drag"));
dz.addEventListener("drop", (e) => { e.preventDefault(); dz.classList.remove("drag"); analyzeFile(e.dataTransfer.files[0]); });
$("#trPathForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  try { setSource(await api("POST", "/api/translate/analyze-path", { path: $("#trPath").value })); } catch (err) { fail(err); }
});
for (const id of ["#trModel", "#trFrom", "#trTo"]) $(id).addEventListener("input", updateCost);
$("#trModel").addEventListener("change", async () => {   // re-check the CLI login when switching to it
  const m = curModel();
  if (m.provider === "antigravity") {
    if (!T.info.antigravity?.checked) await checkAntigravity();
  } else if (m.provider === "omniroute") {
    await checkOmniroute();
  } else if (m.provider === "grok_cli") {
    if (!T.info.grok_cli?.checked) await checkGrokCli();
  } else if (m.provider === "xai") {
    await checkGrok();
  } else if (m.subscription) {
    try { T.info = { ...await api("GET", "/api/translate/info"), grok: T.info.grok, grok_cli: T.info.grok_cli, omniroute: T.info.omniroute, antigravity: T.info.antigravity }; } catch { /* keep old */ }
  }
  renderAuth(); updateCost();
});
$("#trStart").addEventListener("click", start);
$("#trRefresh").addEventListener("click", loadJobs);
$("#trStyleSave").addEventListener("click", async () => {
  try { await api("PUT", "/api/translate/style", { style: $("#trStyle").value }); toast("Đã lưu hướng dẫn dịch mặc định", "ok"); } catch (e) { fail(e); }
});
$("#trStyleReset").addEventListener("click", () => { if (T.info) $("#trStyle").value = T.info.default_style; });
