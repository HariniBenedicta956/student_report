const batchId = window.BATCH_ID;
const studentId = window.STUDENT_ID;
const detailTitle = document.getElementById("detail-title");
const detailBody = document.getElementById("detail-body");
const subtitle = document.getElementById("dash-subtitle");

let selectedStep = null;
let userSelected = false;
let latestTrace = {};

document.getElementById("dash-steps").addEventListener("click", (e) => {
  const btn = e.target.closest(".dash-step");
  if (!btn) return;
  userSelected = true;
  selectStep(btn.dataset.step);
});

function selectStep(step) {
  selectedStep = step;
  document.querySelectorAll(".dash-step").forEach((b) => {
    b.classList.toggle("active", b.dataset.step === step);
  });
  renderDetail(step);
}

function statusClass(status) {
  if (status === "running") return "running";
  if (status === "done") return "done";
  if (status === "error") return "error";
  return "pending";
}

function updateStepList(trace) {
  Object.keys(trace).forEach((key) => {
    const entry = trace[key];
    const dot = document.getElementById(`dot-${key}`);
    if (dot) dot.className = `dash-step-dot ${statusClass(entry.status)}`;
    const timeEl = document.getElementById(`time-${key}`);
    if (!timeEl) return;
    if (entry.status === "running" && entry.elapsed_s != null) {
      timeEl.textContent = `${entry.elapsed_s}s`;
    } else if (entry.duration_s != null) {
      timeEl.textContent = `${entry.duration_s}s`;
    } else {
      timeEl.textContent = "";
    }
  });
}

function renderValue(value) {
  if (value == null) return "";
  if (typeof value === "string") return value;
  return JSON.stringify(value, null, 2);
}

function escapeHtml(str) {
  const div = document.createElement("div");
  div.textContent = str == null ? "" : str;
  return div.innerHTML;
}

function renderDetail(step) {
  const entry = latestTrace[step];
  if (!entry) {
    detailTitle.textContent = "Select a step";
    detailBody.innerHTML = "";
    return;
  }
  detailTitle.textContent = entry.label || step;

  const metaBits = [`Status: <b>${entry.status}</b>`];
  if (entry.status === "running" && entry.elapsed_s != null) {
    metaBits.push(`Elapsed: <b>${entry.elapsed_s}s</b>`);
  } else if (entry.duration_s != null) {
    metaBits.push(`Duration: <b>${entry.duration_s}s</b>`);
  }
  if (entry.extra) {
    Object.entries(entry.extra).forEach(([k, v]) => {
      if (v != null) metaBits.push(`${k}: <b>${v}</b>`);
    });
  }

  let html = `<div class="detail-meta">${metaBits.map((m) => `<span>${m}</span>`).join("")}</div>`;

  if (entry.status === "pending") {
    html += `<p class="meta">Not reached yet.</p>`;
  }
  if (entry.code) {
    html += `<div class="detail-block"><h3>Code</h3><pre class="code-view">${escapeHtml(entry.code)}</pre></div>`;
  }
  if (entry.input !== undefined) {
    html += `<div class="detail-block"><h3>Input</h3><pre class="json-view">${escapeHtml(renderValue(entry.input))}</pre></div>`;
  }
  if (entry.output !== undefined) {
    html += `<div class="detail-block"><h3>Output</h3><pre class="json-view">${escapeHtml(renderValue(entry.output))}</pre></div>`;
  }

  detailBody.innerHTML = html;
}

function pickAutoStep(trace) {
  const order = Object.keys(trace);
  const running = order.find((k) => trace[k].status === "running");
  if (running) return running;
  const errored = order.find((k) => trace[k].status === "error");
  if (errored) return errored;
  let lastDone = null;
  order.forEach((k) => { if (trace[k].status === "done") lastDone = k; });
  return lastDone || order[0];
}

async function pollTrace() {
  try {
    const res = await fetch(`/batch/${batchId}/students/${studentId}/trace`);
    const trace = await res.json();
    latestTrace = trace;
    updateStepList(trace);

    if (!userSelected) {
      selectStep(pickAutoStep(trace));
    } else if (selectedStep) {
      renderDetail(selectedStep);
    }

    const allSettled = Object.values(trace).every(
      (e) => e.status === "done" || e.status === "error"
    );
    if (!allSettled) {
      setTimeout(pollTrace, 1500);
    } else {
      subtitle.textContent = "Generation finished — this trace is now final.";
    }
  } catch (err) {
    setTimeout(pollTrace, 3000);
  }
}

pollTrace();
