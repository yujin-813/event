const DEFAULT_INGEST_URL = "https://asknuggetdata.com/qa/collect";

chrome.runtime.onInstalled.addListener(() => {
  chrome.storage.local.get(["qa_ingest_url"], (curr) => {
    if (!curr.qa_ingest_url) {
      chrome.storage.local.set({ qa_ingest_url: DEFAULT_INGEST_URL });
    }
  });
});

function withConfig(callback) {
  chrome.storage.local.get(["qa_ingest_url", "qa_session_id"], (cfg) => {
    callback({
      qa_ingest_url: (cfg.qa_ingest_url || DEFAULT_INGEST_URL).trim(),
      qa_session_id: (cfg.qa_session_id || "").trim()
    });
  });
}

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (!msg || typeof msg !== "object") {
    sendResponse({ ok: false, reason: "invalid_message" });
    return false;
  }

  if (msg.type === "qa_get_config") {
    withConfig((cfg) => sendResponse({ ok: true, ...cfg }));
    return true;
  }

  if (msg.type === "qa_set_config") {
    const next = {};
    if (typeof msg.qa_ingest_url === "string") next.qa_ingest_url = msg.qa_ingest_url.trim();
    if (typeof msg.qa_session_id === "string") next.qa_session_id = msg.qa_session_id.trim();
    chrome.storage.local.set(next, () => sendResponse({ ok: true }));
    return true;
  }

  if (msg.type === "qa_collect_hit") {
    const payload = msg.payload || {};
    withConfig((cfg) => {
      const ingestUrl = cfg.qa_ingest_url || DEFAULT_INGEST_URL;
      const sessionId = String(payload.session_id || cfg.qa_session_id || "").trim();
      const requestUrl = String(payload.request_url || "").trim();
      if (!sessionId || !requestUrl) {
        sendResponse({ ok: false, reason: "missing_fields" });
        return;
      }
      fetch(ingestUrl, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          session_id: sessionId,
          request_url: requestUrl,
          request_method: String(payload.request_method || "GET").toUpperCase(),
          request_body: String(payload.request_body || "")
        })
      })
        .then(() => sendResponse({ ok: true }))
        .catch((err) => sendResponse({ ok: false, reason: String(err || "fetch_failed") }));
    });
    return true;
  }

  sendResponse({ ok: false, reason: "unknown_type" });
  return false;
});
