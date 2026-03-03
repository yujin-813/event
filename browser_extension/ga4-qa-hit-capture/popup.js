const ingestEl = document.getElementById("ingestUrl");
const sidEl = document.getElementById("sessionId");
const statusEl = document.getElementById("status");
const saveBtn = document.getElementById("saveBtn");

function setStatus(text) {
  statusEl.textContent = text || "";
}

chrome.runtime.sendMessage({ type: "qa_get_config" }, (resp) => {
  if (!resp || !resp.ok) return;
  ingestEl.value = resp.qa_ingest_url || "";
  sidEl.value = resp.qa_session_id || "";
});

saveBtn.addEventListener("click", () => {
  chrome.runtime.sendMessage(
    {
      type: "qa_set_config",
      qa_ingest_url: (ingestEl.value || "").trim(),
      qa_session_id: (sidEl.value || "").trim()
    },
    (resp) => {
      if (resp && resp.ok) setStatus("저장되었습니다.");
      else setStatus("저장 실패");
    }
  );
});
