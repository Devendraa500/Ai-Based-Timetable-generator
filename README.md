# 📅 Smart Timetable Assistant (AI-Based Timetable Generator)

A Flask-based timetable generation system with year-wise scheduling, batch-wise lab allocation, manual edits, slot locking, validation, and exports.

---

## 🎓 Academic Details
- *Course:* Natural Language Processing (NLP)
- *Class:* Semester VI (Third Year Engineering)
- *College:* Pillai College of Engineering  
  Learn more: https://www.pce.ac.in/

---

## 📌 Overview
The Smart Timetable Assistant is an AI-based web application designed to automate the scheduling of academic timetables. It efficiently manages classes, faculty, subjects, and rooms while ensuring conflict-free scheduling using intelligent constraint handling.

---

## 🎯 Objective
- Automate timetable generation
- Reduce manual scheduling errors
- Handle constraints like:
  - Faculty availability
  - Room capacity
  - Batch allocation
- Provide a scalable and efficient scheduling solution

---

## 🚀 Features

- Dynamic timetable generation
- Year-wise timetable views (1st, 2nd, 3rd, 4th year)
- Batch-wise lab scheduling
- Faculty-year assignment constraints
- Faculty unavailability constraints
- Conflict validation engine for:
  - Class conflicts
  - Faculty conflicts
  - Room conflicts
  - Batch conflicts
  - Lab capacity issues
  - Classroom capacity issues
- Manual timetable slot editing
- Slot lock/unlock support
- AI timetable import preview & correction
- Timetable version history with rollback
- Partial regeneration of unlocked entries
- Export support:
  - CSV
  - XLSX
  - PDF
- Print-friendly UI
- Login-protected dashboard

---

## 🧠 Technologies Used
- Python (Flask)
- HTML, CSS, Bootstrap
- JavaScript
- DuckDB (Database)
- ReportLab (PDF Export)
- OpenPyXL (Excel Export)
- NLP Concepts (basic constraint parsing)
- OpenAI / Gemini APIs (optional AI features)

---

## 📊 Dataset
- Internally managed database using DuckDB
- Contains:
  - Faculty data
  - Subjects
  - Classes & batches
  - Rooms
  - Timetable entries

---

## 📂 Project Structure

- `app.py` – Flask app, scheduler, validation, APIs  
- `templates/` – UI pages  
- `database/` – DuckDB storage  
- `requirements.txt` – Dependencies  

---

## ⚙️ Installation

```bash
git clone https://github.com/Devendraa500/Ai-Based-Timetable-generator.git
cd Ai-Based-Timetable-generator
pip install -r requirements.txt
python app.py
