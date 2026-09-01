const batchId = window.BATCH_ID;
const rows = document.getElementById("validate-rows");
const countEl = document.getElementById("validate-count");
const detailPanel = document.getElementById("detail-panel");
const detailTitle = document.getElementById("detail-title");
const detailSummary = document.getElementById("detail-summary");
const detailAttempts = document.getElementById("detail-attempts");
const detailJsonLabel = document.getElementById("detail-json-label");
const detailJson = document.getElementById("detail-json");
const breakerBanner = document.getElementById("circuit-breaker-banner");
const breakerText = document.getElementById("circuit-breaker-text");
const resumeSkippedBtn = document.getElementById("resume-skipped-btn");
const failureSummaryPanel = document.getElementById("failure-summary-panel");
const failureSummaryRows = document.getElementById("failure-summary-rows");
const retryFailedBtn = document.getElementById("retry-failed-btn");

function statusLabel(entry) {
  if (entry.status === "pending") return "Pending";
  if (entry.status === "running") return "Validating";
  if (entry.status === "error") return "Error";
  if (entry.status === "skipped") return "Skipped";
  if (entry.status === "done") return entry.passed ? "Passed" : "Failed";
  return entry.status;
}

function statusClass(entry) {
  if (entry.status === "pending" || entry.status === "running") return entry.status;
  if (entry.status === "error" || entry.status === "skipped") return "failed";
  return entry.passed ? "passed" : "failed";
}

// Re-runs validation for just these student_ids (backs "re-validate failed
// only", "re-validate this group", and resuming past a tripped circuit
// breaker) -- everyone else's existing result is left untouched server-side.
async function triggerRevalidation(studentIds) {
  await fetch(`/batch/${batchId}/validate`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ student_ids: studentIds }),
  });
  poll();
}

function dots() {
  return `<span class="dots"><span>.</span><span>.</span><span>.</span></span>`;
}

// Student names come from uploaded CSV data and error signatures can include
// raw model-generated text -- neither is trusted, so anything derived from
// them gets escaped before landing in an innerHTML template string.
function escapeHtml(text) {
  const div = document.createElement("div");
  div.textContent = text == null ? "" : String(text);
  return div.innerHTML;
}

function render(validation) {
  const students = validation.students || [];
  rows.innerHTML = "";
  let settledCount = 0;
  let passCount = 0;
  const failedIds = [];
  const skippedIds = [];

  students.forEach((entry) => {
    const settled = entry.status === "done" || entry.status === "error" || entry.status === "skipped";
    if (settled) settledCount++;
    if (entry.status === "done" && entry.passed) passCount++;
    if (settled && !entry.passed && entry.status !== "skipped") failedIds.push(entry.student_id);
    if (entry.status === "skipped") skippedIds.push(entry.student_id);

    const tr = document.createElement("tr");
    const cls = statusClass(entry);
    const showDots = entry.status === "pending" || entry.status === "running";
    tr.innerHTML = `
      <td class="name">${escapeHtml(entry.name || entry.student_id)}</td>
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

  const finished = validation.finished_at != null;
  retryFailedBtn.style.display = finished && failedIds.length ? "" : "none";
  retryFailedBtn.onclick = () => triggerRevalidation(failedIds);

  if (validation.circuit_breaker_tripped && skippedIds.length) {
    breakerBanner.style.display = "block";
    breakerText.textContent = `Stopped early: several students in a row failed every attempt, which ` +
      `usually means a systemic prompt/rubric problem rather than isolated bad data. ` +
      `${skippedIds.length} student(s) were left unchecked to avoid spending more model calls on a ` +
      `likely-doomed run. Investigate the failures below, fix the underlying issue if needed, then resume.`;
    resumeSkippedBtn.onclick = () => triggerRevalidation(skippedIds);
  } else {
    breakerBanner.style.display = "none";
  }

  const summary = validation.failure_summary || [];
  failureSummaryPanel.style.display = summary.length ? "" : "none";
  failureSummaryRows.innerHTML = "";
  summary.forEach((group) => {
    const tr = document.createElement("tr");
    const names = group.student_ids
      .map((sid) => (students.find((s) => s.student_id === sid) || {}).name || sid)
      .join(", ");
    tr.innerHTML = `
      <td>${group.count}×</td>
      <td>${escapeHtml(group.signature)}<br><span class="criteria-line" style="margin:2px 0 0">${escapeHtml(names)}</span></td>
      <td class="action-cell"><button class="link-btn group-retry-btn">Re-validate this group</button></td>
    `;
    tr.querySelector(".group-retry-btn").onclick = () => triggerRevalidation(group.student_ids);
    failureSummaryRows.appendChild(tr);
  });

  return finished;
}

function attemptLine(a) {
  const bits = [];
  if (a.report_json === null && a.structural_errors && a.structural_errors[0] &&
      a.structural_errors[0].startsWith("regeneration failed")) {
    return `Attempt ${a.attempt}: ${a.structural_errors[0]}`;
  }
  bits.push(a.structural_ok ? "structural ok" : "structural failed");
  if (a.content_checked) bits.push(a.content_ok ? "content ok" : "content failed");
  const passed = a.structural_ok && (!a.content_checked || a.content_ok);
  const errs = [...(a.structural_errors || []), ...(a.content_errors || [])];
  let line = `Attempt ${a.attempt}: ${passed ? "passed" : "failed"} (${bits.join(", ")})`;
  if (!passed && errs.length) line += "\n  " + errs.join("\n  ");
  return line;
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

      const attempts = entry.attempts || [];
      const lines = [];
      lines.push(`Overall: ${entry.status === "error" ? "error" : (entry.passed ? "passed" : "failed")}`);
      if (attempts.length > 1) {
        lines.push(`Resolved after ${attempts.length} of up to ${window.VALIDATION_RETRY_CAP} attempts.`);
      }
      lines.push("");
      lines.push(`Structural check: ${entry.structural_ok ? "ok" : "failed"}`);
      (entry.structural_errors || []).forEach((err) => lines.push(`  - ${err}`));
      lines.push("");
      if (entry.content_checked) {
        lines.push(`Content/accuracy check: ${entry.content_ok ? "ok" : "failed"}`);
        (entry.content_errors || []).forEach((err) => lines.push(`  - ${err}`));
      } else if (entry.structural_errors && entry.structural_errors.length) {
        lines.push("Content/accuracy check: skipped — structural check failed first");
      } else if (entry.status !== "error") {
        lines.push("Content/accuracy check: not available — source answers were not " +
          "saved for this batch (generated before this check existed). Regenerate " +
          "this batch to enable it.");
      }
      detailSummary.textContent = lines.join("\n");

      if (attempts.length > 1) {
        const header = document.createElement("p");
        header.className = "section-label";
        header.textContent = "attempt history";
        const pre = document.createElement("pre");
        pre.className = "json-view";
        pre.textContent = attempts.map(attemptLine).join("\n\n");
        detailAttempts.replaceChildren(header, pre);
      } else {
        detailAttempts.replaceChildren();
      }

      if (entry.report_json) {
        detailJsonLabel.style.display = "";
        detailJson.style.display = "";
        detailJson.textContent = JSON.stringify(entry.report_json, null, 2);
      } else {
        detailJsonLabel.style.display = "none";
        detailJson.style.display = "none";
        detailJson.textContent = "";
      }

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
