const batchId = window.BATCH_ID;
const rows = document.getElementById("validate-rows");
const countEl = document.getElementById("validate-count");
const detailPanel = document.getElementById("detail-panel");
const detailTitle = document.getElementById("detail-title");
const detailJson = document.getElementById("detail-json");

function statusLabel(entry) {
  if (entry.status === "pending") return "Pending";
  if (entry.status === "running") return "Validating";
  if (entry.status === "error") return "Error";
  if (entry.status === "done") return entry.passed ? "Passed" : "Failed";
  return entry.status;
}

function statusClass(entry) {
  if (entry.status === "pending" || entry.status === "running") return entry.status;
  if (entry.status === "error") return "failed";
  return entry.passed ? "passed" : "failed";
}

function dots() {
  return `<span class="dots"><span>.</span><span>.</span><span>.</span></span>`;
}

function render(validation) {
  const students = validation.students || [];
  rows.innerHTML = "";
  let settledCount = 0;
  let passCount = 0;

  students.forEach((entry) => {
    const settled = entry.status === "done" || entry.status === "error";
    if (settled) settledCount++;
    if (entry.status === "done" && entry.passed) passCount++;

    const tr = document.createElement("tr");
    const cls = statusClass(entry);
    const showDots = entry.status === "pending" || entry.status === "running";
    tr.innerHTML = `
      <td class="name">${entry.name || entry.student_id}</td>
      <td class="status-cell"><span class="status ${cls}">${statusLabel(entry)}${showDots ? dots() : ""}</span></td>
      <td class="action-cell"><button class="link-btn detail-btn" data-student-id="${entry.student_id}" ${settled ? "" : "disabled"}>Details</button></td>
    `;
    rows.appendChild(tr);
  });

  if (!validation.started) {
    countEl.textContent = "Starting…";
  } else if (students.length === 0) {
    countEl.textContent = "No generated reports in this batch yet.";
  } else {
    countEl.textContent = `${students.length} reports — ${passCount} passed, ${settledCount} checked`;
  }

  return validation.finished_at != null;
}

rows.addEventListener("click", (e) => {
  const btn = e.target.closest(".detail-btn");
  if (!btn) return;
  fetch(`/batch/${batchId}/validate/status`)
    .then((r) => r.json())
    .then((validation) => {
      const entry = (validation.students || []).find((s) => s.student_id === btn.dataset.studentId);
      if (!entry) return;
      detailTitle.textContent = `details — ${entry.name || entry.student_id}`;
      detailPanel.style.display = "block";

      const lines = [];
      lines.push(`Overall: ${entry.status === "error" ? "error" : (entry.passed ? "passed" : "failed")}`);
      lines.push("");
      lines.push(`Structural check: ${entry.structural_ok ? "ok" : "failed"}`);
      (entry.structural_errors || []).forEach((err) => lines.push(`  - ${err}`));
      lines.push("");
      if (entry.content_checked) {
        lines.push(`Content/accuracy check: ${entry.content_ok ? "ok" : "failed"}`);
        (entry.content_errors || []).forEach((err) => lines.push(`  - ${err}`));
      } else if (entry.structural_errors && entry.structural_errors.length) {
        lines.push("Content/accuracy check: skipped — structural check failed first");
      } else {
        lines.push("Content/accuracy check: not available — source answers were not " +
          "saved for this batch (generated before this check existed). Regenerate " +
          "this batch to enable it.");
      }
      detailJson.textContent = lines.join("\n");
      detailPanel.scrollIntoView({ behavior: "smooth", block: "nearest" });
    });
});

async function ensureStarted() {
  const res = await fetch(`/batch/${batchId}/validate/status`);
  const validation = await res.json();
  if (!validation.started) {
    await fetch(`/batch/${batchId}/validate`, { method: "POST" });
  }
  poll();
}

async function poll() {
  try {
    const res = await fetch(`/batch/${batchId}/validate/status`);
    const validation = await res.json();
    const settled = render(validation);
    if (!settled) setTimeout(poll, 2000);
  } catch (err) {
    setTimeout(poll, 5000);
  }
}

ensureStarted();
