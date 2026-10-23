// Word-like editing tools for the Vietnamese pane: Find / Replace (Ctrl+F, Ctrl+H) and a live word count.
// Self-contained: it only reads/edits #viEditor's DOM. Matches are shown with the CSS Custom Highlight API (no DOM
// changes), and app.js's undo history picks replacements up through the `input` event that execCommand fires.
// Undo/redo/chapter load swap innerHTML without that event, so a MutationObserver keeps matches + counts fresh.
const $ = (s) => document.querySelector(s);
const ed = $("#viEditor");
const bar = $("#findBar"), qIn = $("#findInput"), rIn = $("#replaceInput");
const HAS_HL = typeof CSS !== "undefined" && !!CSS.highlights && typeof Highlight !== "undefined";

const F = { matches: [], cur: -1 };

// ---------------------------------------------------------------- word count
const fmt = (n) => n.toLocaleString("vi-VN");
function countText(text) {
  const t = text.normalize("NFC");
  const flat = t.replace(/\n/g, "");
  return {
    words: (t.match(/\S+/g) || []).length,
    chars: [...flat].length,
    noSpace: [...flat.replace(/\s/g, "")].length,
  };
}
function editorText() { return [...ed.childNodes].map((n) => n.textContent.replace(/​/g, "")).join("\n"); }
function updateCount() {
  const all = countText(editorText());
  const paras = [...ed.childNodes].filter((n) => n.textContent.trim()).length;
  let html = `<span><b>${fmt(all.words)}</b> từ</span><span><b>${fmt(all.chars)}</b> ký tự</span>` +
    `<span>${fmt(all.noSpace)} không tính khoảng trắng</span><span>${fmt(paras)} đoạn</span>`;
  const sel = window.getSelection();
  if (sel.rangeCount && !sel.isCollapsed && ed.contains(sel.anchorNode) && ed.contains(sel.focusNode)) {
    const s = countText(sel.toString());
    if (s.words || s.chars) html += `<span class="sel">Đã chọn: <b>${fmt(s.words)}</b> từ · ${fmt(s.chars)} ký tự</span>`;
  }
  $("#wordCount").innerHTML = html;
}

// ---------------------------------------------------------------- find
function buildRegex() {
  const q = qIn.value;
  if (!q) return null;
  let src = $("#findRegex").checked ? q : q.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  if ($("#findWord").checked) src = `(?<![\\p{L}\\p{N}_])(?:${src})(?![\\p{L}\\p{N}_])`;
  try { return new RegExp(src, "gu" + ($("#findCase").checked ? "" : "i")); } catch { return false; }
}

function textUnits() { // one unit per top-level block; matches never cross a block boundary
  const units = [];
  for (const node of ed.childNodes) {
    const segs = []; let text = "";
    const add = (t) => { segs.push({ node: t, start: text.length }); text += t.textContent; };
    if (node.nodeType === Node.TEXT_NODE) add(node);
    else if (node.nodeType === Node.ELEMENT_NODE) {
      const w = document.createTreeWalker(node, NodeFilter.SHOW_TEXT);
      let t; while ((t = w.nextNode())) add(t);
    }
    if (text) units.push({ segs, text });
  }
  return units;
}
function pointAt(segs, off, end) {
  for (let i = 0; i < segs.length; i++) {
    const s = segs[i], len = s.node.textContent.length;
    if (off < s.start + len || (end && off === s.start + len && i === segs.length - 1)) return [s.node, off - s.start];
  }
  const last = segs[segs.length - 1]; return [last.node, last.node.textContent.length];
}

function collectMatches() {
  const re = buildRegex();
  const out = [];
  if (re) {
    for (const u of textUnits()) {
      re.lastIndex = 0;
      let m;
      while ((m = re.exec(u.text))) {
        if (!m[0].length) { re.lastIndex++; continue; }
        const r = document.createRange();
        r.setStart(...pointAt(u.segs, m.index, false));
        r.setEnd(...pointAt(u.segs, m.index + m[0].length, true));
        out.push({ range: r, text: m[0] });
      }
    }
  }
  return { re, out };
}

function paint() {
  if (!HAS_HL) return;
  CSS.highlights.delete("nt-find"); CSS.highlights.delete("nt-find-cur");
  if (bar.classList.contains("hidden") || !F.matches.length) return;
  CSS.highlights.set("nt-find", new Highlight(...F.matches.map((m) => m.range)));
  if (F.cur >= 0) CSS.highlights.set("nt-find-cur", new Highlight(F.matches[F.cur].range));
}

function scrollToCurrent() {
  const m = F.matches[F.cur]; if (!m) return;
  const r = m.range.getBoundingClientRect(), e = ed.getBoundingClientRect();
  if (r.top < e.top + 20 || r.bottom > e.bottom - 20) ed.scrollTop += r.top - e.top - e.height / 2;
}

function refresh({ keepCur = true, scroll = false } = {}) {
  const { re, out } = collectMatches();
  F.matches = out;
  F.cur = out.length ? (keepCur && F.cur >= 0 ? Math.min(F.cur, out.length - 1) : 0) : -1;
  qIn.classList.toggle("bad", re === false);
  $("#findCount").textContent = re === false ? "Regex không hợp lệ" : !qIn.value ? "" : out.length ? `${F.cur + 1}/${out.length}` : "Không có kết quả";
  for (const id of ["#findPrev", "#findNext", "#replaceOne", "#replaceAll"]) $(id).disabled = !out.length;
  paint();
  if (scroll) scrollToCurrent();
}

function step(dir) {
  if (!F.matches.length) return;
  F.cur = (F.cur + dir + F.matches.length) % F.matches.length;
  $("#findCount").textContent = `${F.cur + 1}/${F.matches.length}`;
  paint(); scrollToCurrent();
}

// ---------------------------------------------------------------- replace
function replacementFor(m, re) {
  if (!$("#findRegex").checked) return rIn.value;
  return m.text.replace(new RegExp(re.source, re.flags.replace("g", "")), rIn.value); // honours $1, $&
}
function replaceRange(range, text) { // via execCommand so the editor's native undo + `input` handler stay in play
  const sel = window.getSelection(); sel.removeAllRanges(); sel.addRange(range);
  if (text === "") document.execCommand("delete"); else document.execCommand("insertText", false, text);
}
function editable() {
  if (!ed.isContentEditable) { $("#replaceMsg").textContent = "Chương đang bị khoá — không thể thay thế."; return false; }
  return true;
}
function replaceOne() {
  const re = buildRegex(), m = F.matches[F.cur];
  if (!re || !m || !editable()) return;
  const keep = document.activeElement;
  ed.focus(); replaceRange(m.range, replacementFor(m, re));
  keep?.focus?.();
  refresh({ keepCur: true, scroll: true });
  $("#replaceMsg").textContent = "Đã thay 1.";
}
function replaceAll() {
  const re = buildRegex(); const { out } = collectMatches();
  if (!re || !out.length || !editable()) return;
  const keep = document.activeElement, top = ed.scrollTop, winY = window.scrollY;
  ed.focus();
  for (let i = out.length - 1; i >= 0; i--) replaceRange(out[i].range, replacementFor(out[i], re)); // back to front keeps earlier ranges valid
  keep?.focus?.({ preventScroll: true });
  window.getSelection().removeAllRanges();
  ed.scrollTop = top; window.scrollTo(window.scrollX, winY); // the last edit is the first match, which would yank the view to the top
  F.cur = 0; refresh({ keepCur: false });
  $("#replaceMsg").textContent = `Đã thay ${fmt(out.length)} chỗ (Ctrl+Z để hoàn tác).`;
}

// ---------------------------------------------------------------- bar open / close
function openBar(focusReplace = false) {
  const sel = window.getSelection();
  if (sel.rangeCount && !sel.isCollapsed && ed.contains(sel.anchorNode) && !sel.toString().includes("\n")) qIn.value = sel.toString();
  bar.classList.remove("hidden");
  $("#replaceMsg").textContent = "";
  refresh({ keepCur: false, scroll: true });
  const t = focusReplace ? rIn : qIn; t.focus(); t.select();
}
function closeBar() { bar.classList.add("hidden"); paint(); F.matches = []; F.cur = -1; ed.focus(); }

// ---------------------------------------------------------------- line spacing (view only: never written to the draft)
function initLineHeight() {
  const sel = $("#lineHeight");
  const apply = (v) => { document.documentElement.style.setProperty("--lh", v); sel.value = v; };
  let saved = null;
  try { saved = localStorage.getItem("nt.lineHeight"); } catch { /* storage blocked: keep default */ }
  apply([...sel.options].some((o) => o.value === saved) ? saved : "1.75");
  sel.addEventListener("change", () => {
    apply(sel.value);
    try { localStorage.setItem("nt.lineHeight", sel.value); } catch { /* ignore */ }
    if (!bar.classList.contains("hidden")) paint();
  });
}

function init() {
  initLineHeight();
  $("#findBtn").addEventListener("click", () => bar.classList.contains("hidden") ? openBar() : closeBar());
  $("#findClose").addEventListener("click", closeBar);
  $("#findNext").addEventListener("click", () => step(1));
  $("#findPrev").addEventListener("click", () => step(-1));
  $("#replaceOne").addEventListener("click", replaceOne);
  $("#replaceAll").addEventListener("click", replaceAll);
  qIn.addEventListener("input", () => { $("#replaceMsg").textContent = ""; refresh({ keepCur: false, scroll: true }); });
  for (const id of ["#findCase", "#findWord", "#findRegex"]) $(id).addEventListener("change", () => refresh({ keepCur: false, scroll: true }));
  bar.addEventListener("keydown", (ev) => {
    if (ev.key === "Escape") { ev.preventDefault(); closeBar(); }
    else if (ev.key === "Enter" && ev.target === qIn) { ev.preventDefault(); step(ev.shiftKey ? -1 : 1); }
    else if (ev.key === "Enter" && ev.target === rIn) { ev.preventDefault(); replaceOne(); }
  });
  document.addEventListener("keydown", (ev) => {
    if (!(ev.ctrlKey || ev.metaKey) || $("#viewEditor").classList.contains("hidden")) return;
    const k = ev.key.toLowerCase();
    if (k === "f" || k === "h") { ev.preventDefault(); openBar(k === "h"); }
  });

  let t = null; // debounce: typing, undo/redo, chapter load all mutate the editor DOM
  new MutationObserver(() => {
    clearTimeout(t);
    t = setTimeout(() => { updateCount(); if (!bar.classList.contains("hidden")) refresh(); }, 150);
  }).observe(ed, { childList: true, subtree: true, characterData: true });
  document.addEventListener("selectionchange", () => {
    const sel = window.getSelection();
    if (!sel.rangeCount || ed.contains(sel.anchorNode) || $("#wordCount").querySelector(".sel")) updateCount();
  });
  updateCount();
}
init();
