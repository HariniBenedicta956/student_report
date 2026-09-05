const dropzone = document.getElementById("dropzone");
const fileInput = document.getElementById("file-input");
const dropzoneFilename = document.getElementById("dropzone-filename");
const generateBtn = document.getElementById("generate-btn");
const generateBtnText = document.getElementById("generate-btn-text");
const instructions = document.getElementById("instructions");
const errorBanner = document.getElementById("error-banner");
const selectCard = document.getElementById("student-select-card");
const selectCount = document.getElementById("select-count");
const checklist = document.getElementById("student-checklist");
const qbankDropzone = document.getElementById("qbank-dropzone");
const qbankFileInput = document.getElementById("qbank-file-input");
const qbankFilename = document.getElementById("qbank-filename");
const minCompletionInput = document.getElementById("min-completion");
const minScoreInput = document.getElementById("min-score");
const syncEligibilityBtn = document.getElementById("sync-eligibility-btn");
const eligibilitySummary = document.getElementById("eligibility-summary");

let uploadId = null;
let uploadedStudents = [];
let qbankText = null;    // extracted text from the uploaded question bank, if any
let lastCsvFile = null;  // kept so uploading a question bank AFTER the CSV can re-trigger mapping

function showError(message) {
  errorBanner.textContent = message;
  errorBanner.style.display = "block";
}

function clearError() {
  errorBanner.style.display = "none";
}

dropzone.addEventListener("dragover", (e) => e.preventDefault());
dropzone.addEventListener("drop", (e) => {
  e.preventDefault();
  if (e.dataTransfer.files.length) {
    fileInput.files = e.dataTransfer.files;
    handleFile(e.dataTransfer.files[0]);
  }
});

fileInput.addEventListener("change", () => {
  if (fileInput.files.length) handleFile(fileInput.files[0]);
});

async function handleFile(file) {
  clearError();
  lastCsvFile = file;
  dropzoneFilename.textContent = `Uploading ${file.name}...`;
  selectCard.style.display = "none";
  generateBtn.disabled = true;

  const formData = new FormData();
  formData.append("file", file);
  // Mapping is generated fresh from whatever question bank text is on hand
  // this run (core/mapping_inference.py) -- omitted entirely falls back to
  // the static section_mapping.json server-side, same as before this existed.
  if (qbankText) formData.append("question_bank_text", qbankText);

  try {
    const res = await fetch("/upload", { method: "POST", body: formData });
    const data = await res.json();
    if (!res.ok) {
      dropzoneFilename.textContent = "";
      showError(data.error || "Upload failed");
      return;
    }
    uploadId = data.upload_id;
    uploadedStudents = data.students;
    dropzone.classList.add("has-file");
    const mappingNote = data.mapping_source === "auto" ? " (auto-mapped)" : "";
    dropzoneFilename.textContent = `${data.filename} — ${data.student_count} students${mappingNote}`;
    renderChecklist();
    renderMappingReview(data);
    generateBtn.disabled = false;
  } catch (err) {
    dropzoneFilename.textContent = "";
    showError("Upload failed: " + err.message);
  }
}

// Columns the auto-mapper couldn't confidently place (or a low-confidence
// guess it used anyway) -- surfaced so an admin can sanity-check rather than
// trust a silent guess, and so a genuinely unmatched column is visible
// instead of just quietly not counting toward anyone's completion %.
const mappingReviewPanel = document.getElementById("mapping-review-panel");
const mappingReviewList = document.getElementById("mapping-review-list");

function renderMappingReview(data) {
  if (data.auto_map_error) {
    mappingReviewPanel.style.display = "block";
    mappingReviewList.innerHTML =
      `<li>Auto-mapping couldn't run: ${escapeHtmlLocal(data.auto_map_error)} — using the existing static mapping instead.</li>`;
    return;
  }
  const review = data.review_columns || [];
  if (data.mapping_source !== "auto" || review.length === 0) {
    mappingReviewPanel.style.display = "none";
    mappingReviewList.innerHTML = "";
    return;
  }
  mappingReviewPanel.style.display = "block";
  mappingReviewList.innerHTML = review.map((r) =>
    `<li><b>${escapeHtmlLocal(r.column)}</b> — ${escapeHtmlLocal(r.reason)} (best match confidence: ${Math.round((r.best_score || 0) * 100)}%)</li>`
  ).join("");
}

function escapeHtmlLocal(text) {
  const div = document.createElement("div");
  div.textContent = text == null ? "" : String(text);
  return div.innerHTML;
}

function renderChecklist() {
  checklist.innerHTML = "";
  eligibilitySummary.textContent = "";
  uploadedStudents.forEach((s) => {
    const row = document.createElement("label");
    row.className = "checklist-row";
    row.dataset.index = s.index;
    const scoreText = s.overall_score === null || s.overall_score === undefined
      ? ""
      : ` · score ${s.overall_score}%`;
    row.innerHTML = `
      <input type="checkbox" checked data-index="${s.index}">
      <span>${s.name || "(no name)"}</span>
      <span class="meta">${[s.branch, s.year].filter(Boolean).join(" / ")} · ${s.completion_pct}% complete${scoreText}</span>
      <div class="checklist-unanswered meta" style="display:none"></div>
    `;
    row.querySelector("input").addEventListener("change", updateSelectCount);
    checklist.appendChild(row);
  });
  selectCard.style.display = "block";
  updateSelectCount();
}

// Step 2 of the workflow: a real server-side computation against this
// upload's own completion%/score (see /sync-eligibility), which also writes
// eligible Yes/No per student to the database (core/db.py) -- not just a
// client-side filter. The server's response is the source of truth for the
// checklist below, not a local re-computation, so what's stored in the DB
// and what the admin sees always agree. Ineligible rows are unchecked and
// flagged, but stay editable so an admin can still override by hand.
syncEligibilityBtn.addEventListener("click", async () => {
  const minCompletion = parseFloat(minCompletionInput.value) || 0;
  const minScoreRaw = minScoreInput.value.trim();
  const minScore = minScoreRaw === "" ? null : parseFloat(minScoreRaw);

  syncEligibilityBtn.disabled = true;
  eligibilitySummary.textContent = "Syncing…";
  try {
    const res = await fetch("/sync-eligibility", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ upload_id: uploadId, threshold_pct: minCompletion, min_score: minScore }),
    });
    const data = await res.json();
    if (!res.ok) {
      showError(data.error || "Sync Eligibility failed");
      eligibilitySummary.textContent = "";
      return;
    }

    data.students.forEach((student) => {
      const row = checklist.querySelector(`.checklist-row[data-index="${student.index}"]`);
      if (!row) return;
      const checkbox = row.querySelector("input[type=checkbox]");
      const unansweredEl = row.querySelector(".checklist-unanswered");
      checkbox.checked = student.eligible;
      row.classList.toggle("ineligible", !student.eligible);
      if (student.eligible) {
        row.title = "";
        unansweredEl.style.display = "none";
      } else if (student.completion_pct < minCompletion) {
        // Visible, not just a hover tooltip -- "show ... exactly which
        // questions are unanswered" for an ineligible student, not just the %.
        const unanswered = student.unanswered_questions || [];
        const names = unanswered.map((u) => u.qid || u.question).join(", ");
        row.title = `Below ${minCompletion}% completion (has ${student.completion_pct}%)`;
        unansweredEl.textContent = `Unanswered (${unanswered.length}): ${names || "not recorded for this batch"}`;
        unansweredEl.style.display = "block";
      } else {
        row.title = `Below ${minScore}% score (${student.overall_score === null ? "no score" : student.overall_score + "%"})`;
        unansweredEl.style.display = "none";
      }
    });

    eligibilitySummary.textContent = `${data.eligible_count} of ${data.total} eligible — saved to database`;
    updateSelectCount();
  } catch (err) {
    showError("Sync Eligibility failed: " + err.message);
    eligibilitySummary.textContent = "";
  } finally {
    syncEligibilityBtn.disabled = false;
  }
});

function updateSelectCount() {
  const total = checklist.querySelectorAll("input[type=checkbox]").length;
  const checked = checklist.querySelectorAll("input[type=checkbox]:checked").length;
  selectCount.textContent = `${checked} of ${total} selected`;
}

document.getElementById("select-all-btn").addEventListener("click", () => {
  checklist.querySelectorAll("input[type=checkbox]").forEach((cb) => (cb.checked = true));
  updateSelectCount();
});

document.getElementById("select-none-btn").addEventListener("click", () => {
  checklist.querySelectorAll("input[type=checkbox]").forEach((cb) => (cb.checked = false));
  updateSelectCount();
});

qbankDropzone.addEventListener("dragover", (e) => e.preventDefault());
qbankDropzone.addEventListener("drop", (e) => {
  e.preventDefault();
  if (e.dataTransfer.files.length) {
    qbankFileInput.files = e.dataTransfer.files;
    handleQbankFile(e.dataTransfer.files[0]);
  }
});

qbankFileInput.addEventListener("change", () => {
  if (qbankFileInput.files.length) handleQbankFile(qbankFileInput.files[0]);
});

async function handleQbankFile(file) {
  clearError();
  qbankFilename.textContent = `Uploading ${file.name}...`;

  const formData = new FormData();
  formData.append("file", file);

  try {
    const res = await fetch("/upload-question-bank", { method: "POST", body: formData });
    const data = await res.json();
    if (!res.ok) {
      qbankFilename.textContent = "";
      showError(data.error || "Question bank upload failed");
      return;
    }
    qbankDropzone.classList.add("has-file");
    if (data.text) {
      qbankText = data.text;
      qbankFilename.textContent = `${data.filename} — will auto-map questions on upload/re-upload`;
      // The CSV was already uploaded (mapped against the static config, or no
      // question bank at all) -- re-run it now that there's text to auto-map
      // from, so the checklist reflects the real mapping instead of a stale one.
      if (lastCsvFile) handleFile(lastCsvFile);
    } else {
      qbankFilename.textContent = data.extraction_note
        ? `${data.filename} — saved, but ${data.extraction_note}`
        : `${data.filename} — saved`;
    }
  } catch (err) {
    qbankFilename.textContent = "";
    showError("Question bank upload failed: " + err.message);
  }
}

// Ollama falls back to CPU silently -- it does not error, it just runs many times
// slower. So the badge reports what the host is actually doing, refreshed before
// each run rather than only once at page load. Every field shown in the detail
// dropdown is either a real, current number or an explicit "not available" --
// never a placeholder standing in for one, per the rule this was built to: don't
// show "reachable"/"healthy" unless it's backed by a real check.
const gpuBadge = document.getElementById("gpu-badge");
const gpuBadgeText = document.getElementById("gpu-badge-text");
const gpuDetailPanel = document.getElementById("gpu-detail-panel");
const gpuDetailBody = document.getElementById("gpu-detail-body");
let gpuDetailExpanded = false;
let lastGpuStatus = null;

function detailRow(label, value, unavailable) {
  return `<div class="detail-row"><span class="label">${escapeHtmlLocal(label)}</span>` +
    `<span class="value${unavailable ? " unavailable" : ""}">${escapeHtmlLocal(value)}</span></div>`;
}

function renderGpuDetail(s) {
  const parts = ["<h4>Model host</h4>"];
  parts.push(detailRow("Host", s.host || "none configured", !s.host));
  parts.push(detailRow("Reachable", s.reachable ? "yes" : "no"));
  if (s.reachable) {
    parts.push(detailRow("Model", s.model || "none resident", !s.model));
    parts.push(detailRow("Processor", s.processor || "unknown (no model loaded)", !s.processor));
    if (s.size_vram_bytes != null && s.size_bytes) {
      parts.push(detailRow("VRAM", `${(s.size_vram_bytes / 1e9).toFixed(2)} GB of ${(s.size_bytes / 1e9).toFixed(2)} GB (model weights)`));
    } else {
      parts.push(detailRow("VRAM", "not available -- no model currently resident", true));
    }
  } else {
    parts.push(detailRow("Detail", s.detail || "unreachable", true));
  }

  parts.push("<h4>GPU compute / RAM</h4>");
  if (s.nvidia_smi && s.nvidia_smi.length) {
    s.nvidia_smi.forEach((gpu, i) => {
      parts.push(detailRow(`GPU ${i} utilization`, `${gpu.utilization_pct}%`));
      parts.push(detailRow(`GPU ${i} memory`, `${gpu.memory_used_mb} MB of ${gpu.memory_total_mb} MB`));
    });
  } else {
    parts.push(detailRow("GPU utilization", "not available -- this app doesn't run on the GPU host itself, so nvidia-smi can't be checked over the network", true));
  }
  parts.push(detailRow("System RAM", "not tracked -- VRAM above is what determines GPU acceleration for this model", true));

  parts.push("<h4>Recent calls (this session)</h4>");
  const calls = s.recent_calls || [];
  if (calls.length === 0) {
    parts.push(detailRow("Latency / tokens", "no calls made yet this session", true));
  } else {
    const last = calls[0];
    parts.push(detailRow("Last call", last.ok ? "succeeded" : `failed (${last.error_type || "error"})`));
    if (last.ok) {
      parts.push(detailRow("Latency", last.latency_s != null ? `${last.latency_s}s` : "not reported", last.latency_s == null));
      parts.push(detailRow("Tokens", last.prompt_tokens != null
        ? `${last.prompt_tokens} in / ${last.output_tokens} out (${last.tokens_per_sec ?? "?"} tok/s)`
        : "not reported", last.prompt_tokens == null));
    }
    const successRate = Math.round((calls.filter((c) => c.ok).length / calls.length) * 100);
    parts.push(detailRow(`Last ${calls.length} calls`, `${successRate}% succeeded`));
  }
  parts.push(detailRow("Cost", "not applicable -- self-hosted model, nothing metered or billed", true));

  gpuDetailBody.innerHTML = parts.join("");
}

function renderGpuBadge(s) {
  lastGpuStatus = s;
  gpuBadge.classList.remove("on-gpu", "on-cpu");
  if (s.on_gpu === true) {
    gpuBadgeText.textContent = `${s.processor} ✅`;
    gpuBadge.classList.add("on-gpu");
  } else if (s.on_gpu === false) {
    gpuBadgeText.textContent = `${s.processor} ⚠️`;
    gpuBadge.classList.add("on-cpu");
  } else if (s.reachable) {
    gpuBadgeText.textContent = "warming up…";
    gpuBadge.classList.add("on-cpu");
  } else {
    gpuBadgeText.textContent = "host unreachable ⚠️";
    gpuBadge.classList.add("on-cpu");
  }
  if (gpuDetailExpanded) renderGpuDetail(s);
}

async function refreshGpuBadge() {
  try {
    const res = await fetch("/gpu-status");
    const s = await res.json();
    renderGpuBadge(s);
    if (s.reachable && s.on_gpu == null) {
      // No model resident yet -- rather than sit on "unknown" until whoever's
      // turn it is happens to run a real generation, trigger a warm-up load
      // now and re-check once it's done, so the badge settles on a real answer.
      warmModelAndRecheck();
    }
  } catch (err) {
    gpuBadgeText.textContent = "unavailable";
    lastGpuStatus = null;
  }
}

let warming = false;
async function warmModelAndRecheck() {
  if (warming) return;  // a badge refresh already in flight -- don't stack calls
  warming = true;
  try {
    const res = await fetch("/warm-model", { method: "POST" });
    const s = await res.json();
    renderGpuBadge(s);
  } catch (err) {
    gpuBadgeText.textContent = "unavailable";
  } finally {
    warming = false;
  }
}

gpuBadge.addEventListener("click", () => {
  gpuDetailExpanded = !gpuDetailExpanded;
  gpuBadge.classList.toggle("expanded", gpuDetailExpanded);
  gpuDetailPanel.style.display = gpuDetailExpanded ? "block" : "none";
  if (gpuDetailExpanded && lastGpuStatus) renderGpuDetail(lastGpuStatus);
});

refreshGpuBadge();

generateBtn.addEventListener("click", async () => {
  if (!uploadId) return;
  clearError();
  refreshGpuBadge();

  const selectedIndices = Array.from(checklist.querySelectorAll("input[type=checkbox]:checked"))
    .map((cb) => parseInt(cb.dataset.index, 10));

  if (selectedIndices.length === 0) {
    showError("Select at least one student.");
    return;
  }

  generateBtn.disabled = true;
  generateBtnText.textContent = "Starting…";

  try {
    const res = await fetch("/generate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        upload_id: uploadId,
        instructions: instructions.value,
        selected_indices: selectedIndices,
      }),
    });
    const data = await res.json();
    if (!res.ok) {
      showError(data.error || "Generation failed");
      generateBtn.disabled = false;
      generateBtnText.textContent = "Generate reports";
      return;
    }
    window.location.href = `/batch/${data.batch_id}`;
  } catch (err) {
    showError("Generation failed: " + err.message);
    generateBtn.disabled = false;
    generateBtnText.textContent = "Generate reports";
  }
});
