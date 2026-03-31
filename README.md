# Smart Timetable Assistant

A Flask-based timetable generation system with year-wise scheduling, batch-wise lab allocation, manual edits, slot locking, validation, and exports.

## Features

- Dynamic timetable generation
- Year-wise timetable views (1st, 2nd, 3rd, 4th year)
- Batch-wise lab scheduling using:
  - division batches
  - lab room capacities
  - continuous slot duration
- Faculty-year assignment constraints
- Faculty unavailability constraints and validation
- Conflict validation engine for:
  - class conflicts
  - faculty conflicts
  - room conflicts
  - batch conflicts
  - lab capacity issues
  - theory classroom capacity issues
  - faculty scheduled during unavailable slots
  - unassigned requirement detection
- Manual timetable slot editing
- CRUD management for classes, batches, faculty, subjects, and rooms
- Slot lock/unlock support
- AI timetable import preview and correction before apply
- Timetable version history with rollback snapshots
- Partial regeneration of only unlocked entries
- Exports:
  - CSV
  - XLSX
  - PDF (year-specific timetable layout)
- Print-friendly timetable view
- Login-protected dashboard

## Tech Stack

- Python
- Flask
- DuckDB
- Bootstrap 5
- ReportLab (PDF export)
- OpenPyXL (XLSX export)

## Project Structure

- `app.py` - Flask app, scheduler, validation, exports, APIs
- `templates/` - UI templates (dashboard, timetable, forms, login)
- `database/` - DuckDB file storage
- `requirements.txt` - Python dependencies

## Installation

1. Clone or download this project.
2. Create and activate a virtual environment (recommended).
3. Install dependencies:

```bash
pip install -r requirements.txt
```

## Run

```bash
python app.py
```

Open: [http://127.0.0.1:5000](http://127.0.0.1:5000)

## Login

- Username: `admin`
- Password: `admin`

## Typical Workflow

1. Add classes/divisions and batches
2. Add faculty, allowed years, and unavailable slots
3. Add subjects with year, weekly sessions, and continuous slots
4. Add rooms (especially lab capacities)
5. Generate timetable
6. Validate conflicts
7. Preview or correct AI-imported timetable rows if needed
8. Use manual edits/locking where needed
9. Save a snapshot before major changes or roll back from version history
10. Regenerate unlocked slots if required
11. Export CSV/XLSX/PDF or print

## Key Endpoints

- `GET /dashboard`
- `GET /generate`
- `POST /regenerate_unlocked`
- `GET /validate`
- `GET /ai_suggestions?year=<n>` (uses OpenAI/Gemini API key if configured)
- `POST /ai_assistant` (question-based AI assistant for timetable improvements)
- `POST /preview_timetable_import` (multipart: `file`, form `year`, optional `default_class_name`) — extract and preview editable rows from a PDF/image
- `POST /apply_timetable_import` — apply the corrected preview rows to the selected year
- `POST /import_timetable_file` (legacy direct import)
- `GET /get_timetable?year=<1|2|3|4>`
- `POST /manual_edit_slot`
- `POST /toggle_lock`
- `POST /save_timetable_version`
- `GET /get_timetable_versions?year=<n>`
- `POST /rollback_timetable_version`
- `GET /reports`
- `GET /export_csv?year=<n>`
- `GET /export_xlsx`
- `GET /export_pdf?year=<n>`

## Notes

- The database is stored in `database/db.duckdb`.
- `locked` entries are preserved during partial regeneration.
- `.env`, database files, and cache folders are excluded via `.gitignore`.
- To enable AI suggestions, set at least one key in your environment:
  - `OPENAI_API_KEY`
  - `GEMINI_API_KEY`
- PDF/image import uses the same keys (vision). If `OPENAI_API_KEY` is set, it is preferred; otherwise Gemini is used. Install `pymupdf` and `Pillow` (see `requirements.txt`).
