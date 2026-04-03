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
- Conflict validation engine for:
  - class conflicts
  - faculty conflicts
  - room conflicts
  - batch conflicts
  - lab capacity issues
  - unassigned requirement detection
- Manual timetable slot editing
- Slot lock/unlock support
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
2. Add faculty and allowed years
3. Add subjects with year, weekly sessions, and continuous slots
4. Add rooms (especially lab capacities)
5. Generate timetable
6. Validate conflicts
7. Use manual edits/locking where needed
8. Regenerate unlocked slots if required
9. Export CSV/XLSX/PDF or print

## Key Endpoints

- `GET /dashboard`
- `GET /generate`
- `POST /regenerate_unlocked`
- `GET /validate`
- `GET /ai_suggestions?year=<n>` (uses OpenAI/Gemini API key if configured)
- `POST /ai_assistant` (question-based AI assistant for timetable improvements)
- `POST /import_timetable_file` (multipart: `file`, form `year`, optional `default_class_name`) — PDF or image timetable extraction via vision API
- `GET /get_timetable?year=<1|2|3|4>`
- `POST /manual_edit_slot`
- `POST /toggle_lock`
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
