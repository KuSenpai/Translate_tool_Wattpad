// Relays messages between the Wattpad page (content.js) / Novelpia chapter page (novelpia.js) and the local Novel Translator server.
// Only talks to the local app; never reads cookies or passwords.
const DEFAULT_APP = "http://127.0.0.1:8765";

async function appBase() {
  const { app } = await chrome.storage.local.get("app");
  return (app || DEFAULT_APP).replace(/\/+$/, "");
}

async function call(path, method = "GET", body) {
  const res = await fetch((await appBase()) + path, {
    method,
    headers: { "X-NT-Extension": "1", "X-NT-Version": chrome.runtime.getManifest().version, "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  let data = null;
  try { data = await res.json(); } catch { /* empty */ }
  if (!res.ok) throw new Error(data?.error?.message || `HTTP ${res.status}`);
  return data;
}

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  (async () => {
    switch (msg.type) {
      case "config": return call("/api/ext/config");
      case "task": return call("/api/ext/task?tab=" + encodeURIComponent(msg.tab || ""));
      case "event": return call(`/api/ext/task/${encodeURIComponent(msg.id)}/event`, "POST", msg.data);
      case "scan": return call("/api/ext/scan?tab=" + encodeURIComponent(msg.tab || ""));
      case "scan_chunk": return call(`/api/ext/scan/${encodeURIComponent(msg.id)}/chunk`, "POST", msg.data);
      case "scan_event": return call(`/api/ext/scan/${encodeURIComponent(msg.id)}/event`, "POST", msg.data);
      case "stories": return call("/api/ext/stories", "POST", msg.data);
      case "diag": return call("/api/ext/diag", "POST", msg.data);
      case "novelpia": return call("/api/ext/novelpia", "POST", msg.data);
      case "focus": { // a background tab is throttled: Wattpad then republished the old text or ignored Publish
        const tab = _sender.tab;
        if (!tab) return { ok: false };
        const win = await chrome.windows.get(tab.windowId);
        await chrome.windows.update(tab.windowId, win.state === "minimized" ? { state: "normal", focused: true } : { focused: true });
        await chrome.tabs.update(tab.id, { active: true });
        return { ok: true };
      }
      case "getApp": return { app: await appBase() };
      case "setApp": await chrome.storage.local.set({ app: msg.app }); return { ok: true };
      default: throw new Error("unknown message " + msg.type);
    }
  })().then((r) => sendResponse({ ok: true, data: r }), (e) => sendResponse({ ok: false, error: String(e.message || e) }));
  return true; // async response
});
