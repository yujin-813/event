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

  function syncSidToBackground() {
    const sidFromQuery = getSidFromQuery();
    if (!sidFromQuery) return;
    try {
      sessionStorage.setItem(SID_KEY, sidFromQuery);
    } catch (e) {}
    chrome.runtime.sendMessage({ type: "qa_set_tab_session", session_id: sidFromQuery }, () => {});
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

  chrome.runtime.onMessage.addListener((msg) => {
    if (!msg || typeof msg !== "object") return;
    if (msg.type === "qa_collect_hit_seen") {
      hitVisualCue();
    }
  });

  syncSidToBackground();
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") syncSidToBackground();
  });
})();
