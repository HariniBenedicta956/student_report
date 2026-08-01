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
  uploadedStudents.forEach((s) => {
    const row = document.createElement("label");
    row.className = "checklist-row";
    row.innerHTML = `
      <input type="checkbox" checked data-index="${s.index}">
      <span>${s.name || "(no name)"}</span>
      <span class="meta">${[s.branch, s.year].filter(Boolean).join(" / ")}</span>
    `;
    row.querySelector("input").addEventListener("change", updateSelectCount);
    checklist.appendChild(row);
  });
  selectCard.style.display = "block";
  updateSelectCount();
}

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

generateBtn.addEventListener("click", async () => {
  if (!uploadId) return;
  clearError();

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
