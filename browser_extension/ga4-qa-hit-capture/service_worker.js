const DEFAULT_INGEST_URL = "https://asknuggetdata.com/qa/collect";
const SID_KEY = "qa_debug_session_id";
const tabSessionMap = new Map();

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

  if (msg.type === "qa_set_tab_session") {
    const sid = String(msg.session_id || "").trim();
    const tabId = sender && sender.tab ? sender.tab.id : -1;
    if (tabId >= 0 && sid) {
      tabSessionMap.set(tabId, sid);
      chrome.storage.local.set({ qa_session_id: sid }, () => sendResponse({ ok: true }));
      return true;
    }
    sendResponse({ ok: false, reason: "missing_tab_or_session" });
    return true;
  }

  sendResponse({ ok: false, reason: "unknown_type" });
  return false;
});

function safeParseUrl(rawUrl) {
  try {
    return new URL(String(rawUrl || ""));
  } catch (e) {
    return null;
  }
}

function isCollectUrl(rawUrl) {
  const u = safeParseUrl(rawUrl);
  if (!u) return false;
  const path = (u.pathname || "").toLowerCase();
  if (path.includes("/qa/collect")) return false;
  if (path.includes("/g/collect") || path.includes("/mp/collect")) return true;
  if (path.endsWith("/collect")) {
    return u.searchParams.get("v") === "2" || u.searchParams.has("en") || u.searchParams.has("tid") || u.searchParams.has("measurement_id");
  }
  return false;
}

function extractSidFromCollectUrl(rawUrl) {
  const u = safeParseUrl(rawUrl);
  if (!u) return "";
  const direct = String(u.searchParams.get(SID_KEY) || "").trim();
  if (direct) return direct;
  const ep = String(u.searchParams.get("ep." + SID_KEY) || "").trim();
  if (ep) return ep;
  return "";
}

function decodeRawBody(raw) {
  if (!raw || !raw.bytes) return "";
  try {
    const bytes = new Uint8Array(raw.bytes);
    return new TextDecoder("utf-8").decode(bytes);
  } catch (e) {
    return "";
  }
}

function requestBodyToText(body) {
  if (!body) return "";
  if (body.formData && typeof body.formData === "object") {
    const p = new URLSearchParams();
    for (const [k, arr] of Object.entries(body.formData)) {
      if (!Array.isArray(arr)) continue;
      for (const v of arr) p.append(k, String(v));
    }
    return p.toString();
  }
  if (Array.isArray(body.raw) && body.raw.length > 0) {
    return decodeRawBody(body.raw[0]);
  }
  return "";
}

function notifyTabHit(tabId) {
  if (typeof tabId !== "number" || tabId < 0) return;
  try {
    chrome.tabs.sendMessage(tabId, { type: "qa_collect_hit_seen" }, () => {});
  } catch (e) {}
}

function resolveSessionId(details, cfg) {
  const fromUrl = extractSidFromCollectUrl(details.url);
  if (fromUrl) return fromUrl;
  if (typeof details.tabId === "number" && details.tabId >= 0) {
    const mapped = String(tabSessionMap.get(details.tabId) || "").trim();
    if (mapped) return mapped;
  }
  return String(cfg.qa_session_id || "").trim();
}

function postCollectHit(details) {
  if (!isCollectUrl(details.url)) return;
  withConfig((cfg) => {
    const ingestUrl = cfg.qa_ingest_url || DEFAULT_INGEST_URL;
    const sessionId = resolveSessionId(details, cfg);
    const bodyText = requestBodyToText(details.requestBody);
    fetch(ingestUrl, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        session_id: sessionId,
        request_url: String(details.url || ""),
        request_method: String(details.method || "GET").toUpperCase(),
        request_body: bodyText
      })
    })
      .then(() => notifyTabHit(details.tabId))
      .catch(() => {});
  });
}

chrome.webRequest.onBeforeRequest.addListener(
  (details) => {
    postCollectHit(details);
  },
  { urls: ["<all_urls>"] },
  ["requestBody"]
);

chrome.tabs.onRemoved.addListener((tabId) => {
  tabSessionMap.delete(tabId);
});
