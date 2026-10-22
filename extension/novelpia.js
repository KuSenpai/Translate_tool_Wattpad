// Novel Translator – Novelpia: runs on chapter pages (novelpia.com/viewer/<id>) in the user's own logged-in browser
// and sends the text that page already shows to the local tool. Nothing is bypassed: a chapter this account cannot
// open (not logged in, no adult verification, not bought) has no text, and the script just says so and stops.
(() => {
  if (window.__ntNovelpia) return;
  window.__ntNovelpia = true;

  const send = (msg) => new Promise((res) => {
    try { chrome.runtime.sendMessage(msg, (r) => res(r || { ok: false, error: chrome.runtime.lastError?.message })); }
    catch (e) { res({ ok: false, error: String(e) }); }
  });
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const val = (sel) => { const e = document.querySelector(sel); return String(e?.value ?? e?.textContent ?? "").trim(); };
  // "Send until the end" survives the page loads of the following chapters in this tab only.
  const AUTO = "nt_novelpia_auto";
  const auto = {
    get() { try { return sessionStorage.getItem(AUTO) === "1"; } catch { return false; } },
    set(v) { try { v ? sessionStorage.setItem(AUTO, "1") : sessionStorage.removeItem(AUTO); } catch { /* unavailable */ } },
  };

  // One <font class="line"> per paragraph; blank paragraphs are "&nbsp;" only; the first one holds the cover image.
  const lines = () => [...document.querySelectorAll("#novel_drawing font.line")]
    .filter((l) => !l.querySelector(".cover-wrapper"))
    .map((l) => l.innerText.replace(/ /g, " ").trim());

  async function chapter() {
    for (let i = 0; i < 30 && !lines().some(Boolean); i++) await sleep(500); // text is loaded after the page
    const text = lines().join("\n").replace(/\n{3,}/g, "\n\n").trim();
    if (!text) return null;
    return {
      novel_no: val("#novel_no"),
      content_no: val("#content_no") || location.pathname.split("/").pop(),
      novel_title: document.title.split(" - ").slice(2).join(" - ").trim(), // "노벨피아 - 웹소설로 꿈꾸는 세상! - <novel>"
      number: Number(val(".menu-top-tag").match(/\d+/)?.[0] || 0),          // "EP.12"; "Prologue" -> 0
      title: val(".menu-top-title-text"),
      text,
    };
  }

  const box = document.createElement("div");
  box.style.cssText = "position:fixed;right:12px;bottom:90px;z-index:2147483647;max-width:280px;padding:8px 10px;"
    + "background:#fff;color:#222;border:1px solid #999;border-radius:8px;box-shadow:0 2px 8px #0003;font:13px/1.45 sans-serif";
  box.innerHTML = '<b>📖 Novel Translator</b><div style="margin:4px 0"><button data-a="one">Gửi chương này</button> '
    + '<button data-a="auto"></button></div><div data-st style="word-break:break-all"></div>';
  const status = box.querySelector("[data-st]");
  const autoBtn = box.querySelector('[data-a="auto"]');
  const say = (t) => { status.textContent = t; };
  const paint = () => { autoBtn.textContent = auto.get() ? "■ Dừng" : "▶ Gửi tới hết"; };
  const stop = (t) => { auto.set(false); paint(); say(t); };
  document.body.append(box);
  paint();

  let busy = false;
  async function run() {
    if (busy) return;
    busy = true;
    try {
      say("Đang đọc chương…");
      const ch = await chapter();
      if (!ch) return stop("Không thấy nội dung chương. Tài khoản của bạn đã mở được chương này chưa (đăng nhập, xác minh tuổi, mua chương)?");
      const r = await send({ type: "novelpia", data: ch });
      if (!r.ok) return stop("Không gửi được về tool: " + r.error);
      say(`Đã lưu chương ${ch.number} (${r.data.chapters} chương) → ${r.data.path}`);
      if (!auto.get()) return;
      const next = val("#next_epi_auto_url");
      if (!next) return stop(status.textContent + " — Hết chương.");
      await sleep(4000 + Math.random() * 3000); // read at a human pace, do not hammer the site
      if (auto.get()) location.href = next;
    } finally {
      busy = false;
    }
  }

  box.addEventListener("click", (e) => {
    const a = e.target.dataset?.a;
    if (a === "one") run();
    if (a === "auto") { auto.set(!auto.get()); paint(); if (auto.get()) run(); }
  });
  if (auto.get()) run();
})();
