// Novel Translator – Wattpad Helper: runs on wattpad.com pages in the user's own browser.
//  • On My Works: sends the list of the user's stories to the local tool.
//  • When the tool has a scan task: reads the author's old published parts and hands the text to the tool (termbase).
//  • When the tool has a publish task: opens the story → New part → fills title + content → saves
//    → (publish mode) short countdown with a Cancel button → Publish → confirmation popup → reports the result.
//    A "batch" is just a sequence of such tasks; the tool hands them out one at a time.
// Progress is stored on the tool side (task.stage), so page navigations do not lose the flow.
(() => {
  if (window.__ntHelper) return;
  window.__ntHelper = true;

  const send = (msg) => new Promise((res) => {
    try { chrome.runtime.sendMessage(msg, (r) => res(r || { ok: false, error: chrome.runtime.lastError?.message })); }
    catch (e) { res({ ok: false, error: String(e) }); }
  });
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const norm = (s) => (s || "").replace(/\s+/g, " ").trim();
  const low = (s) => norm(s).toLowerCase();
  const reEsc = (s) => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");

  // Per-tab memory that survives navigations inside this tab (sessionStorage), never leaves the browser.
  const mem = {
    get(k) { try { return sessionStorage.getItem(k); } catch { return null; } },
    set(k, v) { try { sessionStorage.setItem(k, v); } catch { /* storage unavailable */ } },
  };
  // Identifies this tab to the tool: with several Wattpad tabs open, only one of them gets the task.
  const TAB = mem.get("nt_tab") || (() => {
    const t = Math.random().toString(36).slice(2, 12);
    mem.set("nt_tab", t);
    return t;
  })();

  let cfg = null;
  let busy = false;
  let lastStoriesKey = "";
  const handledCancel = new Set();
  const loginNoticeSent = new Set();
  const opening = new Map(); // task id -> { since, helpAsked } while waiting for Wattpad to open a new part
  const hiddenNoticeSent = new Set();

  // ------------------------------------------------------------------ page helpers
  function visible(el) {
    if (!el || !el.isConnected) return false;
    const r = el.getBoundingClientRect();
    const st = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && st.visibility !== "hidden" && st.display !== "none";
  }
  const CLICKABLE = "button, a, [role='button'], [role='menuitem'], input[type='button'], input[type='submit']";
  const clickables = (root = document) => [...root.querySelectorAll(CLICKABLE)].filter(visible);
  const labelOf = (el) => low(el.innerText || el.value || el.getAttribute("aria-label") || el.getAttribute("title"));
  const isEnabled = (el) => !el.disabled && el.getAttribute("aria-disabled") !== "true";

  // 2 = label equals one of `wants`; 1 = short label containing one of them as a whole word (not links,
  // not "unpublish"/"cancel"-like labels); 0 = no match.
  function tier(el, wants) {
    const l = labelOf(el);
    if (!l) return 0;
    if (wants.includes(l)) return 2;
    const bad = cfg.selectors.bad_label_words || [];
    if (el.tagName === "A" || l.length > 30 || bad.some((b) => l.includes(b))) return 0;
    return wants.some((w) => w.length >= 6 && new RegExp(`(^|[^\\p{L}\\p{N}])${reEsc(w)}($|[^\\p{L}\\p{N}])`, "u").test(l)) ? 1 : 0;
  }
  // Matching clickable elements in document order, best match tier only.
  function matches(texts, root = document, { enabledOnly = false } = {}) {
    const wants = (texts || []).map((t) => t.toLowerCase());
    const found = clickables(root).filter((el) => !enabledOnly || isEnabled(el)).map((el) => [el, tier(el, wants)]).filter(([, t]) => t > 0);
    const best = Math.max(0, ...found.map(([, t]) => t));
    return found.filter(([, t]) => t === best).map(([el]) => el);
  }
  const byText = (texts, root, opts) => matches(texts, root, opts)[0] || null;
  function first(selectors, { enabledOnly = false } = {}) {
    for (const s of selectors || []) {
      try {
        const el = [...document.querySelectorAll(s)].find((e) => visible(e) && (!enabledOnly || isEnabled(e)));
        if (el) return el;
      } catch { /* invalid selector */ }
    }
    return null;
  }
  function findBody(title) {
    const el = first(cfg.selectors.body_selectors);
    if (el) return el;
    // Fallback: the largest visible contenteditable that is not the title field.
    return [...document.querySelectorAll("[contenteditable='true']")]
      .filter((e) => visible(e) && e !== title && !e.contains(title))
      .sort((a, b) => b.getBoundingClientRect().height * b.getBoundingClientRect().width
        - a.getBoundingClientRect().height * a.getBoundingClientRect().width)[0] || null;
  }
  async function waitFor(fn, ms = 20000) {
    const end = Date.now() + ms;
    while (Date.now() < end) {
      const v = fn();
      if (v) return v;
      await sleep(400);
    }
    return null;
  }

  function setTitle(el, value) {
    el.focus();
    if (el.tagName === "INPUT" || el.tagName === "TEXTAREA") {
      const proto = el.tagName === "INPUT" ? HTMLInputElement.prototype : HTMLTextAreaElement.prototype;
      Object.getOwnPropertyDescriptor(proto, "value").set.call(el, value); // works with React-controlled inputs
      el.dispatchEvent(new Event("input", { bubbles: true }));
      el.dispatchEvent(new Event("change", { bubbles: true }));
    } else {
      document.execCommand("selectAll", false);
      document.execCommand("insertText", false, value);
      if (norm(el.innerText) !== norm(value)) el.textContent = value;
    }
    // Wattpad copies the title into its own model on key/blur events. In a background tab focus()/blur() fire no
    // events, so the text sat in the page while Wattpad saved "Untitled Part N": send the events ourselves.
    for (const type of ["input", "keyup", "change"]) el.dispatchEvent(new Event(type, { bubbles: true }));
    el.dispatchEvent(new FocusEvent("focusout", { bubbles: true }));
    el.dispatchEvent(new FocusEvent("blur"));
    el.blur();
  }
  function bodyMatches(el, text) {
    const got = norm(el.innerText), want = norm(text);
    return !!want && got.slice(0, 40) === want.slice(0, 40) && got.slice(-40) === want.slice(-40)
      && Math.abs(got.length - want.length) <= Math.max(20, want.length / 50);
  }
  function insertBody(el, html, text) {
    el.focus();
    document.execCommand("selectAll", false);
    document.execCommand("delete", false);
    const dt = new DataTransfer();
    dt.setData("text/html", html);
    dt.setData("text/plain", text);
    const ev = new ClipboardEvent("paste", { clipboardData: dt, bubbles: true, cancelable: true });
    if (el.dispatchEvent(ev)) document.execCommand("insertHTML", false, html); // editor ignored the paste
  }

  // ------------------------------------------------------------------ popups (the confirmation after "Publish")
  // Wattpad's confirmation popup is not guaranteed to carry role=dialog or a "modal" class, so it is found by
  // what it does to the page: a layer that now covers the Publish button, explicit dialog markup, or buttons that
  // were not there before the click. Roots that already existed before the click are ignored.
  // `anchor` describes the editor's own Publish button (it may be re-rendered, so also matched by label + spot).
  function anchorOf(btn) {
    const r = btn.getBoundingClientRect();
    const a = { el: btn, label: labelOf(btn), cx: r.left + r.width / 2, cy: r.top + r.height / 2 };
    a.is = (e) => {
      if (e === btn) return true;
      const q = e.getBoundingClientRect();
      return labelOf(e) === a.label && Math.abs(q.left + q.width / 2 - a.cx) < 6 && Math.abs(q.top + q.height / 2 - a.cy) < 6;
    };
    return a;
  }
  function popupRoots(anchor) {
    const btn = anchor && anchor.el;
    const roots = new Set();
    if (btn && btn.isConnected) {
      const top = document.elementFromPoint(anchor.cx, anchor.cy);
      if (top && !btn.contains(top) && !top.contains(btn)) {
        let n = top; // climb to the root of the branch that does not contain the button
        while (n.parentElement && !n.parentElement.contains(btn)) n = n.parentElement;
        roots.add(n);
      }
    }
    for (const sel of cfg.selectors.dialog_selectors || []) {
      try { for (const e of document.querySelectorAll(sel)) if (visible(e)) roots.add(e); } catch { /* invalid selector */ }
    }
    return [...roots].filter((e) => e !== document.body && e !== document.documentElement && !(btn && e.contains(btn)));
  }
  function findConfirm(before, anchor, beforeRoots, skip = () => false) {
    const isAnchor = (e) => !!anchor && anchor.is(e) || skip(e);
    const roots = popupRoots(anchor).filter((r) => !beforeRoots.has(r));
    const fresh = clickables().filter((e) => !before.has(e) && !isAnchor(e));
    let button = null;
    for (const s of cfg.selectors.confirm_selectors || []) {
      try { button = [...document.querySelectorAll(s)].find((e) => visible(e) && isEnabled(e) && !isAnchor(e)) || button; } catch { /* invalid */ }
      if (button) break;
    }
    if (!button) {
      const pool = roots.length ? roots.flatMap((r) => clickables(r)) : fresh;
      const wants = (cfg.selectors.confirm_texts || []).map((t) => t.toLowerCase());
      const scored = pool.filter((e) => !isAnchor(e) && isEnabled(e)).map((e) => [e, tier(e, wants)]).filter(([, t]) => t > 0);
      const best = Math.max(0, ...scored.map(([, t]) => t));
      button = scored.filter(([, t]) => t === best).map(([e]) => e).pop() || null; // confirm buttons come last
    }
    return { root: roots[roots.length - 1] || null, button };
  }
  const popupText = (root) => (root ? norm(root.innerText).slice(0, 300) : "");
  const hasErrorWords = (text) => (cfg.selectors.error_texts || []).some((w) => low(text).includes(w.toLowerCase()));

  // Text of visible toasts / alerts / popups (never the chapter text itself) that looks like an error.
  function errorText() {
    const sel = "[role='alert'], [role='status'], [class*='toast' i], [class*='snack' i], [class*='notification' i], [class*='error' i]";
    const words = (cfg.selectors.error_texts || []).map((t) => t.toLowerCase());
    for (const e of document.querySelectorAll(sel)) {
      if (!visible(e) || e.closest("[contenteditable='true']") || e.querySelector("[contenteditable='true']")) continue;
      const t = low(e.innerText);
      if (t && t.length < 400 && words.some((w) => t.includes(w))) return norm(e.innerText).slice(0, 200);
    }
    return "";
  }

  // ------------------------------------------------------------------ banner (visible status + Cancel)
  let bannerEl = null;
  let bannerSeq = 0;
  let bannerPrefix = "";
  function banner(text, withCancel = false, onCancel = null) {
    bannerSeq++;
    if (!bannerEl || !bannerEl.isConnected) {
      bannerEl = document.createElement("div");
      bannerEl.style.cssText = "position:fixed;z-index:2147483647;right:16px;bottom:16px;max-width:380px;padding:12px 14px;" +
        "background:#1f2330;color:#fff;border-radius:10px;font:14px/1.4 Segoe UI,Arial,sans-serif;box-shadow:0 8px 30px rgba(0,0,0,.35)";
      document.documentElement.appendChild(bannerEl);
    }
    bannerEl.innerHTML = "";
    const t = document.createElement("div");
    t.textContent = "📖 Novel Translator: " + bannerPrefix + text;
    bannerEl.appendChild(t);
    if (withCancel) {
      const b = document.createElement("button");
      b.textContent = "Huỷ";
      b.style.cssText = "margin-top:8px;padding:4px 14px;border:0;border-radius:6px;background:#e5484d;color:#fff;cursor:pointer";
      b.onclick = onCancel;
      bannerEl.appendChild(b);
    }
  }
  function hideBanner() { if (bannerEl) { bannerEl.remove(); bannerEl = null; } }
  function hideBannerLater(ms) { const seq = bannerSeq; setTimeout(() => { if (seq === bannerSeq) hideBanner(); }, ms); }

  // ------------------------------------------------------------------ diagnostics (page structure, no personal data)
  function layerSummaries() {
    const out = [];
    const seen = new Set();
    const add = (e) => {
      if (!e || seen.has(e) || e === document.body || !visible(e) || e.closest("[contenteditable='true']")) return;
      seen.add(e);
      out.push({ tag: e.tagName, cls: (e.className || "").toString().slice(0, 80), role: e.getAttribute("role"),
        text: low(e.innerText).slice(0, 300), buttons: clickables(e).slice(0, 12).map((b) => labelOf(b).slice(0, 40)) });
    };
    for (const sel of cfg?.selectors?.dialog_selectors || ["[role='dialog']"]) {
      try { document.querySelectorAll(sel).forEach(add); } catch { /* invalid selector */ }
    }
    for (const c of document.body ? [...document.body.children] : []) { // portal roots with fixed/absolute layers
      for (const e of [c, ...c.children]) {
        const p = getComputedStyle(e).position;
        if ((p === "fixed" || p === "absolute") && e.querySelector(CLICKABLE)) add(e);
      }
    }
    return out.slice(0, 8);
  }
  function collectDiag() {
    const pick = (els, f) => [...els].filter(visible).slice(0, 150).map(f);
    return {
      url: location.href,
      title: document.title,
      links: pick(document.querySelectorAll("a[href]"), (a) => [a.getAttribute("href"), low(a.innerText).slice(0, 60)]),
      buttons: pick(document.querySelectorAll("button, [role='button']"), (b) => [low(b.innerText || b.getAttribute("aria-label")).slice(0, 60),
        b.id, (b.className || "").toString().slice(0, 80), b.getAttribute("data-testid"), isEnabled(b)]),
      inputs: pick(document.querySelectorAll("input, textarea, [contenteditable='true']"), (i) => [i.tagName, i.id,
        i.getAttribute("placeholder") || i.getAttribute("data-placeholder") || "", (i.className || "").toString().slice(0, 80),
        i.getAttribute("data-testid")]),
      dialogs: pick(document.querySelectorAll("[role='dialog'], [role='alertdialog']"), (d) => low(d.innerText).slice(0, 200)),
      layers: layerSummaries(),
    };
  }
  chrome.runtime.onMessage.addListener((msg, _s, reply) => {
    if (msg.type !== "diag") return;
    send({ type: "diag", data: collectDiag() }).then((r) => reply(r.ok ? { ok: true, path: r.data.path } : r));
    return true;
  });

  // ------------------------------------------------------------------ talking to the tool
  async function event(task, data) {
    const r = await send({ type: "event", id: task.id, data: { ...data, tab: TAB } });
    return r.ok;
  }
  // Is `task` still the tool's current task? False after Cancel in the tool, a timeout, or when the tool is gone.
  async function stillCurrent(task) {
    const r = await send({ type: "task", tab: TAB });
    return !!(r.ok && r.data.task && r.data.task.id === task.id);
  }
  async function finish(task, data) {
    await event(task, { ...data, final: true });
    banner(data.ok ? (data.published ? "✔ Đã đăng xong." : "✔ Đã điền và lưu nháp.") : "✖ " + (data.message || "Thất bại."));
    hideBannerLater(8000);
  }
  async function fail(task, code, message) {
    await finish(task, { ok: false, error_code: code, message, diag: collectDiag() });
  }
  function aborted(task) {
    handledCancel.add(task.id);
    banner("Đã dừng — tool đã huỷ thao tác này (hoặc không còn kết nối).");
    hideBannerLater(6000);
  }

  // ------------------------------------------------------------------ My Works → story list
  function scrapeStories() {
    const re = new RegExp(cfg.story_link_regex);
    const ignore = new Set((cfg.story_title_ignore || []).map((s) => s.toLowerCase()));
    const stories = new Map();
    for (const a of document.querySelectorAll("a[href]")) {
      const m = (a.getAttribute("href") || "").match(re);
      if (!m) continue;
      const card = a.closest("li, article, [class*='story' i], [class*='work' i]");
      const heading = card && card.querySelector("h1, h2, h3, h4, [class*='title' i]");
      for (const cand of [norm(heading && heading.innerText), norm(a.innerText || a.getAttribute("title"))]) {
        if (cand && cand.length < 200 && !ignore.has(cand.toLowerCase()) && cand.length > (stories.get(m[1]) || "").length) {
          stories.set(m[1], cand);
        }
      }
      if (!stories.has(m[1])) stories.set(m[1], "");
    }
    return [...stories].map(([id, title]) => ({ id, title }));
  }
  async function maybeSendStories() {
    if (!/^\/myworks\/?$/.test(location.pathname)) return;
    const stories = scrapeStories();
    const key = JSON.stringify(stories);
    if (!stories.length || key === lastStoriesKey) return;
    lastStoriesKey = key;
    const r = await send({ type: "stories", data: { stories, page_url: location.href } });
    if (r.ok) { banner(`Đã gửi ${stories.length} truyện về tool.`); hideBannerLater(4000); }
  }

  // ------------------------------------------------------------------ publish task steps
  // The route, exactly as done by hand on Wattpad:
  //   story page → open the story's LATEST part, then
  //     • it is published           → editor's parts menu → "New Part" → fill the new, empty part
  //     • it is an empty draft      → fill that draft
  //     • it is a draft with text   → stop (someone's work: never overwrite it)
  // The story page's own "+ New Part" button is never used: it only adds a blank draft to the list and opens nothing.
  const storyUrl = (task) => cfg.base + cfg.story_path.replace("{story_id}", task.story_id);
  const partUrl = (task, partId) => cfg.base + cfg.part_edit_path.replace("{story_id}", task.story_id).replace("{part_id}", partId);
  const navKey = (task) => "nt_nav_" + task.id;
  const navCount = (task) => +(mem.get(navKey(task)) || 0);
  const bumpNav = (task) => mem.set(navKey(task), String(navCount(task) + 1));

  function onStoryPage(task) {
    const m = location.pathname.match(/\/myworks\/(\d+)/);
    return m && m[1] === task.story_id && !/\/write\//.test(location.pathname);
  }
  function writePartId() {
    const m = location.pathname.match(new RegExp(cfg.part_url_regex));
    return m ? m[1] : null;
  }
  const idOfHref = (href) => { const m = (href || "").match(new RegExp(cfg.part_url_regex)); return m ? m[1] : null; };
  // Links to this story's parts in page order (oldest first): the list on the story page, or the editor's parts menu.
  function storyPartLinks(task) {
    return [...document.querySelectorAll("a[href*='/write/']")].filter((a) => {
      const href = a.getAttribute("href") || "";
      const m = href.match(/\/myworks\/(\d+)/);
      return idOfHref(href) && (!m || m[1] === task.story_id);
    });
  }
  function latestPart(task) {
    const links = storyPartLinks(task);
    const a = links[links.length - 1];
    if (!a) return null;
    return { id: idOfHref(a.getAttribute("href")), url: new URL(a.getAttribute("href"), location.origin).href,
      text: norm(a.innerText).slice(0, 60), count: links.length };
  }
  // Wait until the part list stops growing (it renders progressively) so "last" really is the latest part.
  async function stableLatestPart(task, ms) {
    const end = Date.now() + ms;
    let count = -1, same = 0, latest = null;
    while (Date.now() < end) {
      latest = latestPart(task);
      same = latest && latest.count === count ? same + 1 : 0;
      count = latest ? latest.count : -1;
      if (latest && same >= 2) return latest;
      await sleep(500);
    }
    return latest;
  }
  // State of the part open in the editor, from its header: "published" | "draft" | null, and its word count.
  function partState() {
    const header = first(cfg.selectors.part_header_selectors);
    if (!header) return { state: null, words: null };
    const is = (list, t) => (list || []).some((w) => t === w.toLowerCase());
    let state = null;
    for (const e of header.querySelectorAll("span, small, p")) {
      const t = low(e.innerText);
      if (!t || t.length > 24) continue;
      if (is(cfg.selectors.published_texts, t)) state = "published";
      else if (is(cfg.selectors.draft_texts, t)) state = state || "draft";
    }
    const m = low(header.innerText).match(/\(\s*([\d.,]+)\s*(?:words?|từ)\s*\)/);
    return { state, words: m ? parseInt(m[1].replace(/\D/g, ""), 10) : null };
  }

  async function goStory(task) {
    if (navCount(task) >= 3) {
      return fail(task, "WATTPAD_UI_CHANGED", "Không mở được trang truyện / danh sách chương trên Wattpad sau vài lần thử. "
        + "Hãy bấm “Gửi chẩn đoán trang” của extension khi đang ở trang truyện rồi báo lại.");
    }
    bumpNav(task);
    banner("Đang mở trang truyện…", true, () => cancel(task));
    location.href = storyUrl(task);
  }

  // Story page: open the story's latest part.
  async function openLatestPart(task) {
    banner("Đang tìm chương mới nhất trên Wattpad…", true, () => cancel(task));
    const latest = await stableLatestPart(task, 25000);
    if (!latest) return fail(task, "WATTPAD_UI_CHANGED", "Không thấy danh sách chương trên trang truyện của Wattpad.");
    if (!(await stillCurrent(task))) return aborted(task);
    await event(task, { message: `Mở chương mới nhất (“${latest.text}”)…` });
    location.href = latest.url;
  }

  // Editor of a part we do not own yet: decide what to do from the part's state.
  async function startFromEditor(task, partId) {
    banner("Đang kiểm tra chương mới nhất trên Wattpad…", true, () => cancel(task));
    const title = await waitFor(() => first(cfg.selectors.title_selectors), 20000);
    const body = title && await waitFor(() => findBody(title), 10000);
    if (!title || !body) return fail(task, "WATTPAD_UI_CHANGED", "Không thấy trang soạn thảo của Wattpad.");
    await sleep(1200); // let the part's header and text settle before reading them
    const latest = latestPart(task);
    if (latest && latest.id !== partId) { // an older chapter is open: go to the newest one
      if (navCount(task) >= 3) return fail(task, "WATTPAD_UI_CHANGED", "Không mở được chương mới nhất của truyện trên Wattpad.");
      bumpNav(task);
      await event(task, { message: `Đang chuyển tới chương mới nhất (“${latest.text}”)…` });
      location.href = latest.url;
      return;
    }
    const { state, words } = partState();
    const empty = !norm(body.innerText) && !words;
    const name = norm(title.innerText).slice(0, 60);
    if (state === "published") return openNewPart(task, partId);
    if (state === "draft" && empty) { // an empty draft is exactly what a new part is: use it
      if (!(await stillCurrent(task))) return aborted(task);
      await event(task, { stage: "filling", part_id: partId, message: `Chương mới nhất (“${name}”) đang trống — điền vào chương đó…` });
      return runTask({ ...task, stage: "filling", part_id: partId });
    }
    if (state === "draft") {
      return fail(task, "WATTPAD_LATEST_DRAFT_NOT_EMPTY", `Chương mới nhất trên Wattpad (“${name}”) là bản nháp đã có nội dung `
        + `(${words ?? "?"} từ) — tool không ghi đè. Hãy đăng hoặc xoá bản nháp đó trên Wattpad rồi thử lại.`);
    }
    return fail(task, "WATTPAD_UI_CHANGED", "Không đọc được trạng thái (Draft / Published) của chương mới nhất trong editor Wattpad. "
      + "Hãy bấm “Gửi chẩn đoán trang” của extension khi đang ở trang soạn thảo rồi báo lại.");
  }

  // A click the way a mouse makes it (pointer/mouse down, up, click at the element's centre). Some Wattpad buttons
  // ignore a bare element.click(), which is what a script usually sends.
  function userClick(el) {
    const r = el.getBoundingClientRect();
    const base = { bubbles: true, cancelable: true, composed: true, view: window, button: 0,
      clientX: r.left + r.width / 2, clientY: r.top + r.height / 2 };
    const fire = (Type, type, extra) => el.dispatchEvent(new Type(type, { ...base, ...extra }));
    fire(PointerEvent, "pointerdown", { buttons: 1, pointerType: "mouse", isPrimary: true });
    fire(MouseEvent, "mousedown", { buttons: 1, detail: 1 });
    if (el.focus) el.focus({ preventScroll: true });
    fire(PointerEvent, "pointerup", { buttons: 0, pointerType: "mouse", isPrimary: true });
    fire(MouseEvent, "mouseup", { buttons: 0, detail: 1 });
    fire(MouseEvent, "click", { buttons: 0, detail: 1 });
  }
  function findNewPart() {
    const menu = first(cfg.selectors.parts_menu_roots);
    return first(cfg.selectors.new_part_selectors) || (menu && byText(cfg.selectors.new_part_texts, menu)) || null;
  }
  // Editor: open the parts menu (top left) and click "New Part" — Wattpad then opens the new, empty part.
  async function openNewPart(task, fromPart) {
    banner("Đang tạo phần mới…", true, () => cancel(task));
    let menuOpenedAt = 0;
    const link = await waitFor(() => {
      const l = findNewPart();
      if (l) return l;
      if (!menuOpenedAt) { // "New Part" sits in the parts dropdown, closed until its toggle is clicked (once)
        const toggle = first(cfg.selectors.parts_menu_toggles);
        if (toggle) {
          if (!first(cfg.selectors.parts_menu_roots)?.classList.contains("open")) userClick(toggle); // already open: leave it
          menuOpenedAt = Date.now();
        }
        return null;
      }
      // The menu is open but the button still does not count as visible: in a background tab Wattpad's CSS
      // transitions are frozen. The button itself works, so click it anyway (only the known New Part selectors).
      if (Date.now() - menuOpenedAt > 2500) {
        for (const sel of cfg.selectors.new_part_selectors || []) {
          const hidden = document.querySelector(sel);
          if (hidden) return hidden;
        }
      }
      return null;
    }, 12000);
    if (!link) return fail(task, "WATTPAD_UI_CHANGED", "Không tìm thấy nút “New Part” trong menu danh sách phần của editor Wattpad.");
    if (!(await stillCurrent(task))) return aborted(task); // cancelled in the tool: do not create a part
    await event(task, { stage: "opening", from_part: fromPart, message: "Đang mở trang soạn phần mới…" });
    opening.set(task.id, { since: Date.now(), helpAsked: false });
    const root = document.documentElement; // in case Wattpad asks something here with a browser popup too
    root.setAttribute("data-nt-auto-confirm", "1");
    setTimeout(() => root.removeAttribute("data-nt-auto-confirm"), 4000);
    userClick(link);
  }
  // After "New Part" was pressed: wait for Wattpad to open the new part. It can take a while. The button is never
  // pressed a second time (that could create two parts); if nothing happens for a few seconds the button is
  // highlighted and the user is asked to press it - the task carries on by itself as soon as the new part opens.
  async function waitOpening(task) {
    if (!opening.has(task.id)) opening.set(task.id, { since: Date.now(), helpAsked: false });
    const st = opening.get(task.id);
    const secs = Math.round((Date.now() - st.since) / 1000);
    if (secs < 10) {
      banner(`Đang chờ Wattpad mở phần mới… (${secs}s)`, true, () => cancel(task));
    } else {
      const btn = document.querySelector((cfg.selectors.new_part_selectors || [])[0] || "button.on-newpart");
      if (!st.helpAsked) {
        st.helpAsked = true;
        if (btn) btn.style.cssText += ";outline:4px solid #e5484d !important;outline-offset:3px";
        await event(task, { message: "Wattpad chưa mở phần mới sau khi extension bấm “New Part” — hãy tự bấm “New Part” (nút đang được tô viền đỏ); tool sẽ tự chạy tiếp." });
      }
      banner(`Wattpad chưa mở phần mới (${secs}s). Hãy bấm “New Part” giúp (nút viền đỏ) — tool sẽ tự tiếp tục.`, true, () => cancel(task));
    }
    if (secs > 180) {
      return fail(task, "WATTPAD_UI_CHANGED", "Đã bấm “New Part” nhưng Wattpad không mở trang soạn phần mới sau 3 phút.");
    }
  }

  async function saveNow() {
    const save = byText(cfg.selectors.save_texts);
    if (save && isEnabled(save)) { save.click(); await sleep(2500); } else await sleep(3000); // autosave
  }
  // The part's title as stored by Wattpad; null when it cannot be told (the check then never blocks publishing).
  async function savedTitle(task, partId) {
    try {
      const j = await getJson(`/api/v3/stories/${task.story_id}?fields=parts(id,title)`);
      const p = (j.parts || []).find((x) => String(x.id) === String(partId));
      return p && typeof p.title === "string" ? norm(p.title) : null;
    } catch { return null; }
  }

  async function fill(task, partId) {
    await event(task, { stage: "filling", part_id: partId, message: "Đang điền tiêu đề và nội dung…" });
    banner("Đang điền tiêu đề và nội dung…", true, () => cancel(task));
    const title = await waitFor(() => first(cfg.selectors.title_selectors));
    if (!title) return fail(task, "WATTPAD_UI_CHANGED", "Không tìm thấy ô tiêu đề chương trên trang soạn thảo.");
    const body = await waitFor(() => findBody(title));
    if (!body) return fail(task, "WATTPAD_UI_CHANGED", "Không tìm thấy vùng soạn nội dung.");
    if (!(await stillCurrent(task))) return aborted(task); // cancelled in the tool: do not touch the editor
    setTitle(title, task.title);
    await sleep(300);
    insertBody(body, task.html, task.text);
    await sleep(800);
    if (!bodyMatches(body, task.text)) { // plain-text fallback
      body.focus();
      document.execCommand("selectAll", false);
      document.execCommand("insertText", false, task.text);
      await sleep(800);
      if (!bodyMatches(body, task.text)) return fail(task, "WATTPAD_PUBLISH_FAILED", "Không chèn được nội dung vào editor Wattpad.");
    }
    await saveNow();
    for (let i = 0; ; i++) { // the title Wattpad itself has stored, not what the page shows
      const got = await savedTitle(task, partId);
      if (got == null || got === norm(task.title)) break;
      if (i === 1) return fail(task, "WATTPAD_TITLE_NOT_SAVED", `Wattpad vẫn lưu tiêu đề “${got}” thay vì “${task.title}”, `
        + "nên chưa bấm Publish. Hãy sửa tiêu đề trên Wattpad rồi tự bấm Publish.");
      await event(task, { message: `Wattpad chưa nhận tiêu đề (đang là “${got}”) — điền lại…` });
      setTitle(title, task.title);
      await saveNow();
    }
    task.stage = "filled";
    await event(task, { stage: "filled", message: "Đã điền và lưu." });
    return true;
  }

  async function countdown(task, seconds) {
    for (let s = seconds; s > 0; s--) {
      if (handledCancel.has(task.id)) return false;
      banner(`Sẽ bấm Publish sau ${s} giây…`, true, () => cancel(task));
      await sleep(1000);
    }
    return !handledCancel.has(task.id);
  }

  // Click Publish; the rest (confirmation popup, possibly several steps, and the verdict) happens in settle().
  async function publish(task) {
    if (!(await countdown(task, task.countdown ?? 5))) return;
    if (!(await stillCurrent(task))) return aborted(task); // cancelled in the tool while counting down
    const btn = await waitFor(() => first(cfg.selectors.publish_selectors, { enabledOnly: true })
      || byText(cfg.selectors.publish_texts, document, { enabledOnly: true }), 15000);
    if (!btn) {
      if (byText(cfg.selectors.unpublish_texts)) { // already live: the content was just updated and saved
        return finish(task, { ok: true, published: true, message: "Phần này đã được đăng sẵn; nội dung đã cập nhật." });
      }
      return fail(task, "WATTPAD_UI_CHANGED", "Không tìm thấy nút Publish / Xuất bản (hoặc nút đang bị khoá).");
    }
    const anchor = anchorOf(btn);
    const before = new Set(clickables());
    const beforeRoots = new Set(popupRoots(anchor));
    await event(task, { stage: "publishing", message: "Đang bấm Publish…" });
    banner("Đang đăng…");
    mem.set("nt_pub_" + task.id, String(Date.now()));
    // Wattpad may ask "publish?" with the browser's own confirm() popup, which a content script cannot click. The
    // page hook (page_hook.js) answers OK to dialogs, but only while this flag is set around the Publish click.
    const root = document.documentElement;
    root.removeAttribute("data-nt-last-dialog");
    root.setAttribute("data-nt-auto-confirm", "1");
    try {
      btn.click();
      const said = root.getAttribute("data-nt-last-dialog");
      if (said) await event(task, { message: `Popup của trình duyệt (${said}) → đã chọn OK.` });
      await settle(task, anchor, before, beforeRoots, 40000);
    } finally {
      root.removeAttribute("data-nt-auto-confirm");
    }
  }

  // After Publish was clicked: confirm any popup (one or more steps) and decide whether the part is live.
  async function settle(task, anchor, before, beforeRoots, ms) {
    const end = Date.now() + ms;
    const ok = () => finish(task, { ok: true, published: true, message: "Đã đăng lên Wattpad." });
    let clicks = 0, goneSince = 0, noButtonSince = 0;
    // The baseline (`before`, `beforeRoots`) stays what it was before Publish was clicked, so a second popup that
    // appears right after the first one closes still counts as new. A button is never clicked twice with the same label.
    const clicked = new WeakMap();
    const wasClicked = (e) => clicked.get(e)?.has(labelOf(e));
    while (Date.now() < end) {
      await sleep(500);
      if (handledCancel.has(task.id)) return;
      if (!/\/write\//.test(location.pathname)) return ok();   // left the editor: part page / story page
      if (byText(cfg.selectors.unpublish_texts)) return ok();   // the part now offers "Unpublish"

      const pop = findConfirm(before, anchor, beforeRoots, wasClicked);
      if (pop.root || pop.button) {
        const text = popupText(pop.root);
        if (pop.root && hasErrorWords(text)) return fail(task, "WATTPAD_PUBLISH_FAILED", `Wattpad không cho đăng: “${text}”`);
        if (!pop.button) { // a popup without a button we know; it may still be rendering
          noButtonSince ||= Date.now();
          if (Date.now() - noButtonSince < 3000) continue;
          return fail(task, "WATTPAD_CONFIRM_NOT_FOUND", "Wattpad hiện popup nhưng không có nút xác nhận mà extension nhận ra"
            + (text ? `. Nội dung popup: “${text}”` : "") + ". Hãy gửi chẩn đoán trang để cập nhật nhãn nút.");
        }
        noButtonSince = 0;
        if (++clicks > 4) return fail(task, "WATTPAD_CONFIRM_NOT_FOUND", "Popup xác nhận của Wattpad lặp lại quá nhiều bước.");
        if (!(await stillCurrent(task))) return aborted(task); // cancelled in the tool: do not confirm
        const label = labelOf(pop.button).slice(0, 30);
        banner(`Đang xác nhận popup (“${label}”)…`);
        await event(task, { message: `Đang bấm “${label}” trong popup xác nhận…` });
        clicked.set(pop.button, (clicked.get(pop.button) || new Set()).add(labelOf(pop.button)));
        pop.button.click();
        if (pop.root) await waitFor(() => !pop.root.isConnected || !visible(pop.root), 3000); // let it close first
        goneSince = 0;
        continue;
      }
      const err = errorText();
      if (err) return fail(task, "WATTPAD_PUBLISH_FAILED", `Wattpad báo lỗi khi đăng: “${err}”`);
      // the Publish button and every popup gone for a few seconds: the part is no longer a draft
      if (!byText(cfg.selectors.publish_texts) && !popupRoots(anchor).some((r) => !beforeRoots.has(r))) {
        goneSince ||= Date.now();
        if (Date.now() - goneSince > 4000) return ok();
      } else goneSince = 0;
    }
    return fail(task, "WATTPAD_PUBLISH_FAILED",
      "Đã bấm Publish nhưng không xác nhận được kết quả. Hãy kiểm tra trên Wattpad trước khi thử lại.");
  }

  async function cancel(task) {
    handledCancel.add(task.id);
    await finish(task, { ok: false, error_code: "CANCELLED", message: "Bạn đã huỷ trên Wattpad." });
  }

  async function runTask(task) {
    const path = location.pathname;
    const partId = writePartId();
    bannerPrefix = task.batch ? `[${task.batch.index}/${task.batch.total}] ` : "";
    if (document.hidden && !hiddenNoticeSent.has(task.id)) { // browsers slow down / freeze the page of a background tab
      hiddenNoticeSent.add(task.id);
      await event(task, { message: "Tab Wattpad đang ở chế độ nền — hãy chuyển sang tab đó (đừng thu nhỏ cửa sổ) để việc đăng chạy ổn định." });
    }

    if (task.stage === "publishing") { // Publish was clicked; we are on the page after it (or the editor still)
      if (partId) return settle(task, null, new Set(clickables()), new Set(), 15000);
      const at = +(mem.get("nt_pub_" + task.id) || 0);
      if (Date.now() - at < 120000) return finish(task, { ok: true, published: true, message: "Đã đăng lên Wattpad." });
      return fail(task, "WATTPAD_PUBLISH_FAILED", "Không xác nhận được kết quả đăng (trang đã đổi sau khi bấm Publish). "
        + "Hãy kiểm tra trên Wattpad trước khi thử lại.");
    }

    if (partId) {
      // Only ever fill a part this task owns: the one it created (opened from a different page than before) or
      // the one it reuses on a retry. Any other editor holds an existing chapter and must not be touched.
      const ours = task.part_id ? task.part_id === partId
        : task.stage === "opening" && partId !== (task.from_part || "");
      if (ours) {
        mem.set(navKey(task), "0");
        if (["pending", "opening", "filling"].includes(task.stage)) {
          if (!(await fill(task, partId))) return;
        }
        if (task.mode !== "publish") return finish(task, { ok: true, published: false, message: "Đã điền và lưu nháp trên Wattpad (chưa publish)." });
        return publish(task);
      }
      if (task.stage === "opening") return waitOpening(task);
      if (task.part_id) return reopenPart(task);
      if (task.stage === "pending") return startFromEditor(task, partId);
      return;
    }

    if (task.stage === "opening") return waitOpening(task);
    if (task.part_id) return reopenPart(task);
    if (task.stage !== "pending") return;
    if (onStoryPage(task)) return openLatestPart(task);
    if (path === "/" || /\/(login|signup)/.test(path)) {
      banner("Hãy đăng nhập Wattpad trong tab này (bật VPN nếu cần). Sau đó mở lại trang truyện — tool sẽ tiếp tục.");
      if (!loginNoticeSent.has(task.id)) {
        loginNoticeSent.add(task.id);
        await event(task, { message: "Wattpad chưa đăng nhập trong trình duyệt này — hãy đăng nhập trong tab Wattpad." });
      }
      return;
    }
    return goStory(task); // any other Wattpad page (My Works, a reader page, …)
  }

  // Retry of a chapter whose part already exists: open it again; if Wattpad no longer has it, create a new one.
  async function reopenPart(task) {
    if (navCount(task) >= 1) {
      mem.set(navKey(task), "0");
      await event(task, { part_gone: true, message: "Phần đã tạo trước đó không còn trên Wattpad — sẽ tạo phần mới." });
      return;
    }
    bumpNav(task);
    banner("Đang mở lại phần đã tạo trước đó…", true, () => cancel(task));
    await event(task, { stage: "pending" }); // the content is filled in again once the part is open
    location.href = partUrl(task, task.part_id);
  }

  // ------------------------------------------------------------------ read old chapters (termbase scan task)
  // The tool asks for the text of the author's published parts; this tab reads them through Wattpad's own
  // endpoints (it has the VPN and the login) and hands them over. Nothing is changed on Wattpad.
  let scanning = false;
  const scanMsg = (task, data) => send({ type: "scan_event", id: task.id, data: { ...data, tab: TAB } });
  const partNumber = (t) => { const m = /^\s*(?:EP\.?\s*\d+\s+)?(\d+)/i.exec(t || ""); return m ? +m[1] : null; };
  function htmlToText(html) {
    const doc = new DOMParser().parseFromString(html, "text/html");
    const ps = [...doc.querySelectorAll("p")].map((p) => p.textContent.trim());
    return (ps.length ? ps : (doc.body.textContent || "").split("\n").map((l) => l.trim())).filter(Boolean).join("\n");
  }
  async function getJson(url) {
    const r = await fetch(url, { credentials: "include", cache: "no-store" });
    if (!r.ok) throw new Error(`HTTP ${r.status} (${url.split("?")[0]})`);
    return r.json();
  }
  async function readPart(p) {
    for (let attempt = 0; attempt < 3; attempt++) {
      try {
        const r = await fetch(`/apiv2/storytext?id=${p.id}`, { credentials: "include" });
        if (r.ok) return { id: p.id, title: p.title, text: htmlToText(await r.text()) };
        if (r.status === 429) await sleep(3000 * (attempt + 1));
      } catch { await sleep(1000); }
    }
    return { id: p.id, title: p.title, text: "" };
  }
  async function runScan(task) {
    scanning = true;
    let cancel = false;
    try {
      for (const story of task.stories) {
        if (cancel) break;
        const name = story.title || story.id;
        let parts = [];
        try {
          const j = await getJson(`/api/v3/stories/${story.id}?fields=parts(id,title,draft)`);
          parts = (j.parts || []).filter((p) => !p.draft && partNumber(p.title) != null
            && (task.from == null || partNumber(p.title) >= task.from) && (task.to == null || partNumber(p.title) <= task.to));
        } catch (e) { await scanMsg(task, { error: `${name}: ${e.message}` }); }
        for (let i = 0; i < parts.length && !cancel; i += 4) {
          const out = await Promise.all(parts.slice(i, i + 4).map(readPart));
          const r = await send({ type: "scan_chunk", id: task.id, data: { parts: out } });
          if (!r.ok) { await scanMsg(task, { error: `Không gửi được về tool: ${r.error}`, done: true }); return; }
          cancel = !!r.data.cancel;
          banner(`Đang đọc “${name}”: ${Math.min(i + 4, parts.length)}/${parts.length} chương…`, true, () => { cancel = true; });
          await sleep(300);
        }
        await scanMsg(task, { story_done: true, message: `Đã đọc xong “${name}” (${parts.length} chương).` });
      }
      await scanMsg(task, { done: true });
      banner(cancel ? "Đã dừng đọc chương." : "✔ Đã đọc xong — tool đang so sánh thuật ngữ.");
      hideBannerLater(6000);
    } catch (e) {
      await scanMsg(task, { error: String(e.message || e), done: true });
    } finally { scanning = false; }
  }

  async function tick() {
    if (busy) return;
    busy = true;
    try {
      if (!cfg) {
        const r = await send({ type: "config" });
        if (!r.ok) return; // tool not running
        cfg = r.data;
      }
      if (location.origin !== new URL(cfg.base).origin) return;
      await maybeSendStories();
      if (!scanning) {
        const sc = await send({ type: "scan", tab: TAB });
        if (sc.ok && sc.data.task) runScan(sc.data.task);   // long job: not awaited, publishing keeps working
      }
      const r = await send({ type: "task", tab: TAB });
      const task = r.ok ? r.data.task : null;
      if (!task) bannerPrefix = "";
      if (!task || handledCancel.has(task.id)) return;
      await runTask(task);
    } catch (e) {
      console.warn("[Novel Translator]", e);
    } finally {
      busy = false;
    }
  }

  tick();
  setInterval(tick, 2000);
})();
