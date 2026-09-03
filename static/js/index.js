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
  dropzoneFilename.textContent = `Uploading ${file.name}...`;
  selectCard.style.display = "none";
  generateBtn.disabled = true;

  const formData = new FormData();
  formData.append("file", file);

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
    dropzoneFilename.textContent = `${data.filename} — ${data.student_count} students`;
    renderChecklist();
    generateBtn.disabled = false;
  } catch (err) {
    dropzoneFilename.textContent = "";
    showError("Upload failed: " + err.message);
  }
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
    `;
    row.querySelector("input").addEventListener("change", updateSelectCount);
    checklist.appendChild(row);
  });
  selectCard.style.display = "block";
  updateSelectCount();
}

// A database check only, no model call, per refinedversion.md's Sync Eligibility --
// this app has no database, so "the database" is the parsed CSV's own completion_pct
// / overall_score (real per-student figures from /upload, not a stub). Sync pre-checks
// only the students who clear both thresholds; ineligible rows are unchecked and
// flagged, but stay editable so an admin can still override by hand.
syncEligibilityBtn.addEventListener("click", () => {
  const minCompletion = parseFloat(minCompletionInput.value) || 0;
  const minScoreRaw = minScoreInput.value.trim();
  const minScore = minScoreRaw === "" ? null : parseFloat(minScoreRaw);

  let eligibleCount = 0;
  checklist.querySelectorAll(".checklist-row").forEach((row) => {
    const index = parseInt(row.dataset.index, 10);
    const student = uploadedStudents.find((s) => s.index === index);
    const checkbox = row.querySelector("input[type=checkbox]");

    const meetsCompletion = student.completion_pct >= minCompletion;
    const meetsScore =
      minScore === null ? true : student.overall_score !== null && student.overall_score >= minScore;
    const eligible = meetsCompletion && meetsScore;

    checkbox.checked = eligible;
    row.classList.toggle("ineligible", !eligible);
    row.title = eligible
      ? ""
      : !meetsCompletion
        ? `Below ${minCompletion}% completion (has ${student.completion_pct}%)`
        : `Below ${minScore}% score (${student.overall_score === null ? "no score" : student.overall_score + "%"})`;
    if (eligible) eligibleCount += 1;
  });

  eligibilitySummary.textContent = `${eligibleCount} of ${uploadedStudents.length} eligible`;
  updateSelectCount();
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
    qbankFilename.textContent = `${data.filename} — saved, mention it in chat to update the mapping`;
  } catch (err) {
    qbankFilename.textContent = "";
    showError("Question bank upload failed: " + err.message);
  }
}

// Ollama falls back to CPU silently -- it does not error, it just runs many times
// slower. So the badge reports what the host is actually doing, refreshed before
// each run rather than only once at page load.
const gpuBadge = document.getElementById("gpu-badge");
const gpuBadgeText = document.getElementById("gpu-badge-text");

async function refreshGpuBadge() {
  try {
    const res = await fetch("/gpu-status");
    const s = await res.json();
    gpuBadge.classList.remove("on-gpu", "on-cpu");
    if (s.on_gpu === true) {
      gpuBadgeText.textContent = `${s.processor} ✅`;
      gpuBadge.classList.add("on-gpu");
    } else if (s.on_gpu === false) {
      gpuBadgeText.textContent = `${s.processor} ⚠️`;
      gpuBadge.classList.add("on-cpu");
    } else if (s.reachable) {
      // No model resident yet -- rather than sit on "unknown" until whoever's
      // turn it is happens to click Generate, trigger a warm-up load now and
      // re-check once it's done, so the badge settles on a real answer.
      gpuBadgeText.textContent = "warming up…";
      warmModelAndRecheck();
    } else {
      gpuBadgeText.textContent = "host unreachable ⚠️";
      gpuBadge.classList.add("on-cpu");
    }
    gpuBadge.title = s.detail || "";
  } catch (err) {
    gpuBadgeText.textContent = "unavailable";
  }
}

let warming = false;
async function warmModelAndRecheck() {
  if (warming) return;  // a badge refresh already in flight -- don't stack calls
  warming = true;
  try {
    const res = await fetch("/warm-model", { method: "POST" });
    const s = await res.json();
    gpuBadge.classList.remove("on-gpu", "on-cpu");
    if (s.on_gpu === true) {
      gpuBadgeText.textContent = `${s.processor} ✅`;
      gpuBadge.classList.add("on-gpu");
    } else if (s.on_gpu === false) {
      gpuBadgeText.textContent = `${s.processor} ⚠️`;
      gpuBadge.classList.add("on-cpu");
    } else {
      gpuBadgeText.textContent = s.reachable ? "unknown — warm-up didn't load a model" : "host unreachable ⚠️";
      if (!s.reachable) gpuBadge.classList.add("on-cpu");
    }
    gpuBadge.title = s.detail || "";
  } catch (err) {
    gpuBadgeText.textContent = "unavailable";
  } finally {
    warming = false;
  }
}

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
