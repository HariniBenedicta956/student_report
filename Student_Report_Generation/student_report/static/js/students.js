const batchId = window.BATCH_ID;
const explorePanel = document.getElementById("explore-panel");
const exploreTitle = document.getElementById("explore-title");
const explorePerf = document.getElementById("explore-perf");
const exploreJson = document.getElementById("explore-json");
const explorePdf = document.getElementById("explore-pdf");
const exploreViewBtn = document.getElementById("explore-view-btn");
const exploreDownloadBtn = document.getElementById("explore-download-btn");
const studentCount = document.getElementById("student-count");

let openStudentId = null;
let openStudentFinalLoaded = false;

function rowStatus(studentId) {
  const row = document.querySelector(`#student-rows tr[data-student-id="${studentId}"]`);
  return row ? row.querySelector(".status").classList[1] : null;
}

document.getElementById("student-rows").addEventListener("click", (e) => {
  const btn = e.target.closest(".explore-btn");
  if (!btn) return;
  openExplore(btn.dataset.studentId, btn.dataset.name, true);
});

function openExplore(studentId, name, scroll) {
  openStudentId = studentId;
  openStudentFinalLoaded = false;
  exploreTitle.textContent = `explore panel — ${name}`;
  explorePanel.style.display = "block";
  if (scroll) explorePanel.scrollIntoView({ behavior: "smooth", block: "nearest" });

  const status = rowStatus(openStudentId);
  if (status === "done") {
    openStudentFinalLoaded = true;
    loadFinishedReport(openStudentId);
  } else if (status === "error") {
    showError();
  } else {
    renderLiveProgress({});
  }
}

// Visible from the moment the page loads -- before generation has even started for
// the first student -- straight through to the final result, not just while a click
// happens to have it open.
(function autoOpenFirstStudent() {
  const firstBtn = document.querySelector(".explore-btn");
  if (firstBtn) openExplore(firstBtn.dataset.studentId, firstBtn.dataset.name, false);
})();

async function loadFinishedReport(studentId) {
  exploreJson.style.display = "block";
  explorePdf.style.display = "none";
  exploreJson.textContent = "Loading…";
  const res = await fetch(`/batch/${batchId}/students/${studentId}`);
  const data = await res.json();
  exploreJson.textContent = JSON.stringify(data, null, 2);
  renderPerf(data._perf);
}

function renderPerf(perf) {
  if (!perf) {
    explorePerf.style.display = "none";
    return;
  }
  const s = perf.stages_s || {};
  const m = perf.ai_metrics || {};
  const stats = [
    ["Total", s.total_s != null ? `${s.total_s}s` : "—"],
    ["AI call", s.ai_call_s != null ? `${s.ai_call_s}s` : "—"],
    ["Attempts", `${perf.attempts}${perf.retries ? ` (${perf.retries} retry)` : ""}`],
    ["Tokens/sec", m.tokens_per_sec != null ? m.tokens_per_sec : "—"],
    ["Prompt tokens", m.prompt_tokens != null ? m.prompt_tokens : "—"],
    ["Output tokens", m.output_tokens != null ? m.output_tokens : "—"],
    ["Model load", m.model_load_s != null ? `${m.model_load_s}s` : "—"],
    ["PDF gen", s.pdf_generate_s != null ? `${s.pdf_generate_s}s` : "—"],
  ];
  explorePerf.innerHTML = stats
    .map(([label, value]) => `<span class="stat">${label}: <b>${value}</b></span>`)
    .join("");
  explorePerf.style.display = "flex";
}

function renderLiveProgress(student) {
  exploreJson.style.display = "none";
  explorePdf.style.display = "none";
  const stageLabel = student.current_stage_label || "Starting…";
  const elapsed = student.stage_elapsed_s != null ? `${student.stage_elapsed_s}s` : "0s";
  explorePerf.innerHTML = [
    ["Status", "Generating…"],
    ["Currently", stageLabel],
    ["Elapsed on this stage", elapsed],
  ].map(([label, value]) => `<span class="stat">${label}: <b>${value}</b></span>`).join("");
  explorePerf.style.display = "flex";
}

function showError() {
  explorePerf.style.display = "none";
  exploreJson.style.display = "block";
  explorePdf.style.display = "none";
  exploreJson.textContent = "Generation failed for this student -- check Explore's saved JSON (once available) or server logs for _generation_error.";
}

exploreViewBtn.addEventListener("click", () => {
  if (!openStudentId || rowStatus(openStudentId) !== "done") return;
  exploreJson.style.display = "none";
  explorePdf.style.display = "block";
  explorePdf.src = `/batch/${batchId}/students/${openStudentId}/pdf`;
});

exploreDownloadBtn.addEventListener("click", () => {
  if (!openStudentId || rowStatus(openStudentId) !== "done") return;
  window.location.href = `/batch/${batchId}/students/${openStudentId}/pdf?download=1`;
});

document.getElementById("download-zip-btn").addEventListener("click", () => {
  window.location.href = `/batch/${batchId}/zip`;
});

document.getElementById("download-all-btn").addEventListener("click", () => {
  document.querySelectorAll("#student-rows tr").forEach((row, i) => {
    if (row.querySelector(".status").classList[1] !== "done") return;
    const studentId = row.dataset.studentId;
    setTimeout(() => {
      const a = document.createElement("a");
      a.href = `/batch/${batchId}/students/${studentId}/pdf?download=1`;
      a.download = `${studentId}.pdf`;
      document.body.appendChild(a);
      a.click();
      a.remove();
    }, i * 400);
  });
});

function applyStatuses(students) {
  let allSettled = true;
  let doneCount = 0;
  students.forEach((s) => {
    const row = document.querySelector(`#student-rows tr[data-student-id="${s.student_id}"]`);
    if (row) {
      const statusEl = row.querySelector(".status");
      statusEl.className = `status ${s.status}`;
      const label = s.status.charAt(0).toUpperCase() + s.status.slice(1);
      statusEl.innerHTML = s.status === "pending"
        ? `${label}<span class="dots"><span>.</span><span>.</span><span>.</span></span>`
        : label;
    }
    if (s.status === "pending") allSettled = false;
    if (s.status === "done") doneCount++;

    if (s.student_id === openStudentId) {
      if (s.status === "pending") {
        renderLiveProgress(s);
      } else if (s.status === "done" && !openStudentFinalLoaded) {
        openStudentFinalLoaded = true;
        loadFinishedReport(openStudentId);
      } else if (s.status === "error" && !openStudentFinalLoaded) {
        openStudentFinalLoaded = true;
        showError();
      }
    }
  });
  studentCount.textContent = `${students.length} students — ${doneCount} done`;
  return allSettled;
}

async function pollStatuses() {
  try {
    const res = await fetch(`/batch/${batchId}/students`);
    const manifest = await res.json();
    const allSettled = applyStatuses(manifest.students);
    if (!allSettled) {
      setTimeout(pollStatuses, 2000);
    }
  } catch (err) {
    setTimeout(pollStatuses, 5000);
  }
}

pollStatuses();
