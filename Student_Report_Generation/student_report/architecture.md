## AGENT PROMPT: Simple Prototype with Gemini Flash

### Updated Context
You are building a **simple, functional prototype** for AI-powered student assessment report generation using:
- **Gemini Flash** (already available - cheaper/faster alternative to GPT models)
- **Vanilla JavaScript** for the frontend/web interface
- **Python** for backend API orchestration and PDF generation
- **Minimal design** - focus on functionality over aesthetics

---

### Tech Stack & Architecture

```
Frontend (Vanilla JS)
    ↓ (Upload JSON/CSV)
Python Backend (Flask/FastAPI)
    ↓
Gemini Flash API
    ↓ (Returns JSON)
Python PDF Generator (ReportLab)
    ↓
Downloadable PDF Report
```

---

### Simplified Prototype Requirements

#### 1. **Backend (Python)**
Create a **single Python file** `app.py` with:

```python
# Required: flask, reportlab, google-generativeai, python-dotenv

# Features:
1. /upload endpoint - Accept student data JSON
2. /generate-report endpoint - Process single student
3. /batch-generate endpoint - Process multiple students
4. Gemini Flash integration with compact prompt
5. PDF generation with ReportLab
```

**Key Functions:**
- `build_prompt(student_data)` → Returns compact prompt (~1800 tokens)
- `call_gemini_flash(prompt)` → Returns JSON narrative
- `generate_pdf(student_data, narrative_json, output_path)` → Creates PDF
- `estimate_tokens(text)` → Simple token counter

#### 2. **Frontend (Vanilla JS)**
Create a **single HTML file** `index.html` with embedded CSS/JS:

**Features:**
- 📤 File upload (JSON/CSV) for student data
- 📋 Display student list with checkboxes
- 🚀 "Generate Reports" button
- 📊 Progress indicator (1/5 processing...)
- 💰 Cost & token estimation display
- 📥 Download all PDFs as zip or individual

**UI Layout (Simple):**
```
+----------------------------------+
| 📊 Assessment Report Generator   |
+----------------------------------+
| [Upload Student Data] (JSON/CSV) |
|                                   |
| Students (5 loaded)              |
| ☑ Nishaleni    ⏳ Pending       |
| ☑ Maha Lakshmi ⏳ Pending       |
| ☑ Mohammed Nizamudeen ✅ Done   |
|                                   |
| [Generate Reports]               |
|                                   |
| 📈 Stats:                        |
| Tokens: 2,870                    |
| Cost: ₹1.95                      |
| Time: 12.3s                      |
+----------------------------------+
```

#### 3. **Prompt Template (Gemini Flash Optimized)**

```json
{
  "system_prompt": "You are an educational assessment expert. Generate a personalized student report in JSON format based on the student's performance data. Keep responses concise but insightful.",
  
  "user_prompt_template": "Student: {name}\nClass: {class}\nScores: {scores}\nPercentile: {percentile}\n\nGenerate JSON with these fields:\n- summary (2 sentences)\n- strengths (3 bullet points)\n- improvements (3 bullet points)\n- recommendations (3 actionable tips)\n- career_guidance (1-2 sentences)\n- overall_insight (1 sentence)\n\nOutput only valid JSON.",
  
  "example": {
    "student": "Nishaleni",
    "scores": {"Math": 85, "Science": 78, "English": 92},
    "output": {
      "summary": "Nishaleni demonstrates strong analytical skills...",
      "strengths": ["Excellent in mathematics", "Strong written communication"],
      "improvements": ["Needs more practice in science practicals"],
      "recommendations": ["Join advanced math club", "Weekly science experiments"],
      "career_guidance": "Consider STEM fields with focus on data analysis",
      "overall_insight": "Consistent performer with room for growth in sciences"
    }
  }
}
```

---

### Data Format

**Input JSON** (`students.json`):
```json
{
  "students": [
    {
      "id": "S001",
      "name": "Nishaleni",
      "class": "10A",
      "scores": {
        "Mathematics": 85,
        "Science": 78,
        "English": 92,
        "Social Studies": 81,
        "Computer Science": 88
      },
      "percentile": 87,
      "attendance": 95
    }
    // Add 4 more students
  ]
}
```

**Output JSON** (from Gemini):
```json
{
  "summary": "Nishaleni demonstrates strong analytical skills...",
  "strengths": ["...", "...", "..."],
  "improvements": ["...", "...", "..."],
  "recommendations": ["...", "...", "..."],
  "career_guidance": "...",
  "overall_insight": "..."
}
```

---

### Implementation Plan

#### Phase 1: Backend (30 min)
1. Create `app.py` with Flask endpoints
2. Implement Gemini Flash integration
3. Implement PDF generator using ReportLab
4. Add token counting & cost estimation

#### Phase 2: Frontend (20 min)
1. Create `index.html` with simple CSS
2. Implement file upload & parsing
3. Add "Generate Reports" button handler
4. Display progress & statistics

#### Phase 3: Integration (10 min)
1. Connect frontend to backend API
2. Handle batch processing (3-5 students)
3. Test with sample data

---

### Simplified PDF Design

Keep PDF design **minimal but professional**:

```
┌──────────────────────────────────────────────┐
│  📄 STUDENT ASSESSMENT REPORT               │
│  ─────────────────────────────────────────── │
│                                              │
│  Name: Nishaleni          Date: 21/07/2026  │
│  Class: 10A               ID: S001          │
│                                              │
│  📊 PERFORMANCE SUMMARY                     │
│  ─────────────────────────────────────────── │
│  Nishaleni demonstrates strong analytical   │
│  skills with exceptional performance in...  │
│                                              │
│  💪 STRENGTHS                               │
│  ─────────────────────────────────────────── │
│  • Excellent in mathematics                 │
│  • Strong written communication             │
│  • Good analytical thinking                 │
│                                              │
│  🎯 IMPROVEMENT AREAS                      │
│  ─────────────────────────────────────────── │
│  • Needs practice in science practicals    │
│                                              │
│  💡 RECOMMENDATIONS                         │
│  ─────────────────────────────────────────── │
│  • Join advanced math club                  │
│  • Weekly science experiments               │
│                                              │
│  🌟 CAREER GUIDANCE                         │
│  ─────────────────────────────────────────── │
│  Consider STEM fields with focus on...      │
│                                              │
│  💬 OVERALL INSIGHT                         │
│  ─────────────────────────────────────────── │
│  Consistent performer with room for...      │
│                                              │
│  Generated by AI Assessment System          │
└──────────────────────────────────────────────┘
```

---

### Sample Data (5 Students)

Create realistic data with varied performance:

| Name | Math | Science | English | Social | CS | Percentile |
|------|------|---------|---------|--------|----|------------|
| Nishaleni | 85 | 78 | 92 | 81 | 88 | 87 |
| Maha Lakshmi | 72 | 90 | 76 | 85 | 79 | 82 |
| Mohammed Nizamudeen | 63 | 55 | 71 | 68 | 65 | 45 |
| Priya Sharma | 94 | 91 | 88 | 92 | 96 | 95 |
| Arjun Kumar | 45 | 52 | 48 | 55 | 50 | 28 |

---

### Cost & Token Tracking (Automated)

In the UI, display:

```javascript
{
  "total_students": 5,
  "total_input_tokens": 8900,
  "total_output_tokens": 4800,
  "total_tokens": 13700,
  "estimated_cost_inr": "₹4.11",
  "per_student_cost": "₹0.82",
  "processing_time_seconds": 18.5
}
```

---

### Simple Error Handling

- If Gemini API fails → Show error but continue with remaining students
- If PDF generation fails → Log error and skip that student
- If JSON parsing fails → Use fallback template

---

### Deliverables Checklist

- [ ] `app.py` - Flask backend with Gemini Flash integration
- [ ] `index.html` - Simple frontend with upload & generation
- [ ] `students.json` - Sample data (5 students)
- [ ] `prompt_template.py` - Prompt configuration
- [ ] `requirements.txt` - Python dependencies
- [ ] `README.md` - Setup & usage instructions
- [ ] 5 generated PDF reports (sample output)
- [ ] Screenshot of UI

---

### Installation Commands

```bash
# Backend
pip install flask flask-cors google-generativeai reportlab python-dotenv

# Environment
GEMINI_API_KEY=your_api_key_here
```

---

### API Endpoints (Simple)

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/health` | GET | Check if API is running |
| `/upload` | POST | Upload student data |
| `/generate` | POST | Generate report for one student |
| `/batch` | POST | Generate reports for all students |
| `/download/<filename>` | GET | Download PDF report |

---

### Testing Scenario

1. Load `students.json` in frontend
2. Click "Generate All Reports"
3. See progress: "Processing 1/5..."
4. Each report takes ~2-3 seconds (Gemini Flash)
5. All 5 PDFs generate in ~15 seconds
6. Total cost displayed: ~₹4-5

---

### Success Criteria

- ✅ All 5 PDFs generated without errors
- ✅ Tokens counted and cost displayed
- ✅ UI shows progress and status
- ✅ PDFs are professional and readable
- ✅ Total cost matches estimates (₹0.25-0.40 per report)
- ✅ Report narratives are personalized (not generic)

---

### Start Here

**Confirm your understanding by:**
1. Listing the 3 main components (Backend, Frontend, PDF)
2. Estimating how many tokens each student will use
3. Describing how you'll handle API rate limits
4. Proposing a simple folder structure

Once confirmed, proceed with implementation. Keep it **simple** - focus on functionality over fancy design.

---

### Sample Folder Structure

```
assessment-report-generator/
├── backend/
│   ├── app.py              # Flask server
│   ├── prompt.py           # Prompt templates
│   ├── pdf_generator.py    # ReportLab implementation
│   ├── gemini_client.py    # Gemini Flash API wrapper
│   └── requirements.txt
├── frontend/
│   ├── index.html          # Single page UI
│   └── style.css           (embedded in HTML)
├── data/
│   └── students.json       # Sample student data
├── output/
│   └── pdfs/              # Generated PDFs go here
└── README.md
