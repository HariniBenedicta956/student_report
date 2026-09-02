const batchId = window.BATCH_ID;
const rows = document.getElementById("validate-rows");
const countEl = document.getElementById("validate-count");
const detailPanel = document.getElementById("detail-panel");
const detailTitle = document.getElementById("detail-title");
const detailMeta = document.getElementById("detail-meta");
const detailAttempts = document.getElementById("detail-attempts");
const breakerBanner = document.getElementById("circuit-breaker-banner");
const breakerText = document.getElementById("circuit-breaker-text");
const resumeSkippedBtn = document.getElementById("resume-skipped-btn");
const failureSummaryPanel = document.getElementById("failure-summary-panel");
const failureSummaryRows = document.getElementById("failure-summary-rows");
const retryFailedBtn = document.getElementById("retry-failed-btn");
const downloadZipBtn = document.getElementById("download-zip-btn");
const summaryPanel = document.getElementById("summary-panel");
const summaryLine = document.getElementById("summary-line");

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
    const hasPdf = entry.status === "done" && entry.passed;
    // The failed-reports list gets one short recurring-issue line here, not
    // the full per-attempt violation history -- that stays behind "Details".
    const failedSummary = entry.status === "done" && !entry.passed && entry.recurring_issue_summary
      ? `<div class="meta" style="margin-top:2px;max-width:480px;white-space:normal">${escapeHtml(entry.recurring_issue_summary)}</div>`
      : "";
    tr.innerHTML = `
      <td class="name">${escapeHtml(entry.name || entry.student_id)}${failedSummary}</td>
      <td class="status-cell"><span class="status ${cls}">${statusLabel(entry)}${showDots ? dots() : ""}</span></td>
      <td class="action-cell">
        <button class="link-btn detail-btn" data-student-id="${entry.student_id}" ${settled ? "" : "disabled"}>Details</button>
        ${hasPdf ? `<a class="link-btn" href="/batch/${batchId}/students/${encodeURIComponent(entry.student_id)}/pdf?download=1">PDF</a>` : ""}
      </td>
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
  downloadZipBtn.style.display = finished && passCount ? "" : "none";

  // The short summary this page ends with -- counts and a rate, not the trace.
  if (finished && students.length) {
    const failCount = settledCount - passCount;
    const rate = Math.round((passCount / students.length) * 100);
    summaryPanel.style.display = "";
    summaryLine.innerHTML =
      `${passCount} passed, ${failCount} failed — <span class="rate">${rate}% pass rate</span>`;
  } else {
    summaryPanel.style.display = "none";
  }

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

// One block per attempt, styled like the Execution Dashboard's Code/Input/
// Output layout (static/js/dashboard.js) -- "rules checked" stands in for
// Code, "JSON validated" for Input, "result" for Output, so validation gets
// the same trace visibility generation already has.
function attemptBlock(a, index) {
  const wasRegenFailure = a.report_json === null && a.structural_errors &&
    a.structural_errors[0] && a.structural_errors[0].startsWith("regeneration failed");
  const block = document.createElement("div");
  block.className = "detail-block";

  const passed = !wasRegenFailure && a.structural_ok && (!a.content_checked || a.content_ok);
  const meta = document.createElement("div");
  meta.className = "detail-meta";
  meta.innerHTML = `<span>Attempt <b>${a.attempt}</b> of up to <b>${window.VALIDATION_RETRY_CAP}</b></span>` +
    `<span>Result: <b>${wasRegenFailure ? "regeneration failed" : (passed ? "passed" : "failed")}</b></span>`;
  block.appendChild(meta);

  if (wasRegenFailure) {
    const h3 = document.createElement("h3");
    h3.textContent = "Error";
    const pre = document.createElement("pre");
    pre.className = "code-view";
    pre.textContent = a.structural_errors[0];
    block.append(h3, pre);
    return block;
  }

  const ranContent = a.content_checked;
  const rulesHeader = document.createElement("h3");
  rulesHeader.textContent = `Rules checked — structural${ranContent ? " + content" : ""}`;
  const rulesPre = document.createElement("pre");
  rulesPre.className = "code-view";
  let rulesText = (window.STRUCTURAL_RULES || []).map((r, i) => `${i + 1}. ${r}`).join("\n");
  if (ranContent) {
    rulesText += "\n\ncontent (model call against the student's own answers):\n" +
      (window.CONTENT_RUBRIC || []).map((r, i) => `${i + 1}. ${r}`).join("\n") +
      `\n\n${window.CONTENT_EXCLUSION || ""}`;
  } else if (!a.structural_ok) {
    rulesText += "\n\ncontent check: skipped — structural check failed first";
  }
  rulesPre.textContent = rulesText;
  block.append(rulesHeader, rulesPre);

  if (a.report_json) {
    const jsonHeader = document.createElement("h3");
    jsonHeader.textContent = "JSON validated";
    const jsonPre = document.createElement("pre");
    jsonPre.className = "json-view";
    jsonPre.textContent = JSON.stringify(a.report_json, null, 2);
    block.append(jsonHeader, jsonPre);
  }

  const resultHeader = document.createElement("h3");
  resultHeader.textContent = "Result";
  const resultPre = document.createElement("pre");
  resultPre.className = "json-view";
  const lines = [`structural: ${a.structural_ok ? "ok" : "failed"}`];
  (a.structural_errors || []).forEach((e) => lines.push(`  - ${e}`));
  if (ranContent) {
    lines.push(`content: ${a.content_ok ? "ok" : "failed"}`);
    (a.content_violations || []).forEach((v) =>
      lines.push(`  - [${v.category}] ${v.dimension || "(general)"}: ${v.detail}`)
    );
  }
  resultPre.textContent = lines.join("\n");
  block.append(resultHeader, resultPre);

  return block;
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
      const overall = entry.status === "error" ? "error" : (entry.passed ? "passed" : "failed");
      const metaBits = [`<span>Overall: <b>${overall}</b></span>`];
      if (attempts.length > 1) {
        metaBits.push(`<span>Resolved after <b>${attempts.length}</b> of up to <b>${window.VALIDATION_RETRY_CAP}</b> attempts</span>`);
      }
      if (entry.status === "error") {
        metaBits.push(`<span>${escapeHtml((entry.structural_errors || [])[0] || "report not found")}</span>`);
      } else if (!entry.content_checked && !(entry.structural_errors || []).length) {
        metaBits.push(`<span>Content check not available — source answers weren't saved for this batch</span>`);
      }
      detailMeta.innerHTML = metaBits.join("");

      detailAttempts.innerHTML = "";
      attempts.forEach((a, i) => detailAttempts.appendChild(attemptBlock(a, i)));

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
