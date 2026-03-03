(function () {
  if (window.__qaHitCaptureExtInstalled) return;
  window.__qaHitCaptureExtInstalled = true;

  const SID_KEY = "qa_debug_session_id";

  function getSidFromQuery() {
    try {
      const q = new URLSearchParams(location.search);
      return String(q.get(SID_KEY) || "").trim();
    } catch (e) {
      return "";
    }
  }

  function normalizeBody(body) {
    try {
      if (!body) return "";
      if (typeof body === "string") return body;
      if (typeof URLSearchParams !== "undefined" && body instanceof URLSearchParams) return body.toString();
      if (typeof FormData !== "undefined" && body instanceof FormData) {
        const p = new URLSearchParams();
        body.forEach((v, k) => p.append(k, String(v)));
        return p.toString();
      }
      return "";
    } catch (e) {
      return "";
    }
  }

  function isCollectUrl(rawUrl) {
    try {
      const u = new URL(String(rawUrl || ""), location.href);
      const path = (u.pathname || "").toLowerCase();
      const q = u.searchParams;
      if (path.includes("/qa/collect")) return false;
      if (path.includes("/g/collect") || path.includes("/mp/collect")) return true;
      if (path.endsWith("/collect")) {
        return q.get("v") === "2" || q.has("en") || q.has("tid") || q.has("measurement_id");
      }
      return false;
    } catch (e) {
      return false;
    }
  }

  function hitVisualCue() {
    let el = document.getElementById("__qaHitIndicator");
    if (!el) {
      el = document.createElement("div");
      el.id = "__qaHitIndicator";
      el.style.position = "fixed";
      el.style.top = "14px";
      el.style.right = "14px";
      el.style.zIndex = "2147483647";
      el.style.padding = "8px 10px";
      el.style.borderRadius = "10px";
      el.style.background = "#111827";
      el.style.color = "#fff";
      el.style.font = "600 12px/1.2 -apple-system,BlinkMacSystemFont,Segoe UI,sans-serif";
      el.style.border = "2px solid transparent";
      document.documentElement.appendChild(el);
    }
    const n = Number(el.getAttribute("data-hit-count") || "0") + 1;
    el.setAttribute("data-hit-count", String(n));
    el.textContent = "QA HIT " + n;
    el.style.background = "#16a34a";
    el.style.borderColor = "rgba(255,255,255,.85)";
    clearTimeout(window.__qaHitIndicatorTimer);
    window.__qaHitIndicatorTimer = setTimeout(() => {
      el.style.background = "#111827";
      el.style.borderColor = "transparent";
    }, 300);
  }

  function sendCollect(rawUrl, method, body) {
    if (!isCollectUrl(rawUrl)) return;
    const sidFromQuery = getSidFromQuery();
    if (sidFromQuery) {
      try {
        sessionStorage.setItem(SID_KEY, sidFromQuery);
      } catch (e) {}
      chrome.runtime.sendMessage({ type: "qa_set_config", qa_session_id: sidFromQuery }, () => {});
    }
    const sid = String(sidFromQuery || sessionStorage.getItem(SID_KEY) || "").trim();
    if (!sid) return;
    hitVisualCue();
    chrome.runtime.sendMessage(
      {
        type: "qa_collect_hit",
        payload: {
          session_id: sid,
          request_url: String(rawUrl || ""),
          request_method: String(method || "GET").toUpperCase(),
          request_body: normalizeBody(body)
        }
      },
      () => {}
    );
  }

  if (typeof window.fetch === "function") {
    const originalFetch = window.fetch.bind(window);
    window.fetch = function (input, init) {
      const url = typeof input === "string" ? input : (input && input.url ? input.url : "");
      sendCollect(url, (init && init.method) || "GET", init && init.body);
      return originalFetch(input, init);
    };
  }

  if (typeof window.XMLHttpRequest !== "undefined") {
    const open = window.XMLHttpRequest.prototype.open;
    const send = window.XMLHttpRequest.prototype.send;
    window.XMLHttpRequest.prototype.open = function (method, url) {
      this.__qaUrl = url;
      this.__qaMethod = method;
      return open.apply(this, arguments);
    };
    window.XMLHttpRequest.prototype.send = function (body) {
      sendCollect(this.__qaUrl, this.__qaMethod || "GET", body);
      return send.apply(this, arguments);
    };
  }

  if (navigator && typeof navigator.sendBeacon === "function") {
    const beacon = navigator.sendBeacon.bind(navigator);
    navigator.sendBeacon = function (url, data) {
      sendCollect(url, "POST", data);
      return beacon(url, data);
    };
  }
})();
