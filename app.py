from flask import Flask, render_template, request, jsonify, send_file, redirect, url_for, session
import duckdb
import os
import json
import csv
import re
import math
from datetime import datetime
from functools import wraps
from pathlib import Path
from dotenv import load_dotenv
from llm import generate_response, extract_timetable_from_upload

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")
app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 10 * 1024 * 1024
app.secret_key = os.getenv("APP_SECRET", "smart-timetable-secret")
os.makedirs(BASE_DIR / "database", exist_ok=True)


def _connect_duckdb_with_recovery(db_path="database/db.duckdb"):
    db_file = Path(db_path)
    db_file.parent.mkdir(parents=True, exist_ok=True)
    wal_file = db_file.with_suffix(db_file.suffix + ".wal")
    try:
        return duckdb.connect(str(db_file))
    except Exception as exc:
        msg = str(exc)
        if "Failure while replaying WAL file" in msg and wal_file.exists():
            backup_name = wal_file.with_name(wal_file.name + f".corrupt_{datetime.now().strftime('%Y%m%d_%H%M%S')}")
            wal_file.replace(backup_name)
            print(f"[duckdb] WAL recovery failed. Backed up bad WAL to: {backup_name}")
            print("[duckdb] Reopening database without WAL. Recent uncheckpointed changes may be lost.")
            return duckdb.connect(str(db_file))
        raise


con = _connect_duckdb_with_recovery(str(BASE_DIR / "database" / "db.duckdb"))

DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]
SLOTS = [
    "09:00-10:00",
    "10:00-11:00",
    "11:00-12:00",
    "13:00-14:00",
    "14:00-15:00",
    "15:00-16:00",
]

BREAK_LABEL = "Lunch Break"
BREAK_DURATION_MINUTES = 60


def _parse_slot_bounds(label):
    m = re.match(r"\s*(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})\s*$", str(label or ""))
    if not m:
        return None
    start = int(m.group(1)) * 60 + int(m.group(2))
    end = int(m.group(3)) * 60 + int(m.group(4))
    if end <= start:
        return None
    return start, end


_SLOT_BOUNDS = [_parse_slot_bounds(label) for label in SLOTS]


def _slot_duration_minutes(slot_index):
    bounds = _SLOT_BOUNDS[slot_index] if 0 <= slot_index < len(_SLOT_BOUNDS) else None
    if bounds:
        return max(1, int(bounds[1] - bounds[0]))
    return 60


def _compute_break_slot_block():
    if not SLOTS:
        return []
    candidates = []
    midpoint = max(0, (len(SLOTS) - 1) / 2)
    for start in range(len(SLOTS)):
        total = 0
        end = start
        while end < len(SLOTS) and total < BREAK_DURATION_MINUTES:
            if end > start:
                prev_bounds = _SLOT_BOUNDS[end - 1]
                curr_bounds = _SLOT_BOUNDS[end]
                if prev_bounds and curr_bounds:
                    if prev_bounds[1] != curr_bounds[0]:
                        break
                elif end != start + 1:
                    break
            total += _slot_duration_minutes(end)
            end += 1
        if total >= BREAK_DURATION_MINUTES:
            block = list(range(start, end))
            center = (start + end - 1) / 2
            overage = total - BREAK_DURATION_MINUTES
            candidates.append((abs(center - midpoint), overage, len(block), block))
    if candidates:
        candidates.sort(key=lambda item: (item[0], item[1], item[2]))
        return candidates[0][3]
    fallback = len(SLOTS) // 2
    return [fallback]


BREAK_SLOT_BLOCK = _compute_break_slot_block()
BREAK_SLOT_SET = set(BREAK_SLOT_BLOCK)


def _is_break_slot(slot_index):
    return int(slot_index) in BREAK_SLOT_SET


def _break_label_for_slot(slot_index):
    if not _is_break_slot(slot_index):
        return ""
    if BREAK_SLOT_BLOCK and int(slot_index) == BREAK_SLOT_BLOCK[0]:
        return BREAK_LABEL
    return f"{BREAK_LABEL} (cont.)"


def _slots_are_clock_continuous(first_slot_index, second_slot_index):
    if not (0 <= first_slot_index < len(SLOTS) and 0 <= second_slot_index < len(SLOTS)):
        return False
    first_bounds = _SLOT_BOUNDS[first_slot_index]
    second_bounds = _SLOT_BOUNDS[second_slot_index]
    if not first_bounds or not second_bounds:
        return second_slot_index == first_slot_index + 1
    return first_bounds[1] == second_bounds[0]


def _block_is_clock_continuous(block):
    ordered = sorted(int(s) for s in block)
    if not ordered:
        return False
    if len(ordered) == 1:
        return True
    for prev, curr in zip(ordered, ordered[1:]):
        if curr != prev + 1:
            return False
        if not _slots_are_clock_continuous(prev, curr):
            return False
    return True


def _normalize_day(raw):
    if raw is None:
        return None
    s = str(raw).strip().lower().replace(".", "")
    if not s:
        return None
    aliases = {
        "mon": "Monday",
        "monday": "Monday",
        "tue": "Tuesday",
        "tues": "Tuesday",
        "tuesday": "Tuesday",
        "wed": "Wednesday",
        "wednesday": "Wednesday",
        "thu": "Thursday",
        "thur": "Thursday",
        "thurs": "Thursday",
        "thursday": "Thursday",
        "fri": "Friday",
        "friday": "Friday",
    }
    if s in aliases:
        return aliases[s]
    for d in DAYS:
        dl = d.lower()
        if dl == s or dl.startswith(s[:3]):
            return d
    return None


def _resolve_slot_index(entry):
    si = entry.get("slot_index")
    if si is not None and si != "":
        try:
            i = int(float(si))
            if 0 <= i < len(SLOTS):
                return i
        except (TypeError, ValueError):
            pass
    tl = (entry.get("time_label") or entry.get("time_slot") or str(entry.get("time") or "")).strip()
    for i, label in enumerate(SLOTS):
        if label == tl or (tl and label in tl):
            return i
        if tl and label.split("-")[0].strip() in tl:
            return i
    m = re.search(r"(\d{1,2})\s*:\s*(\d{2})", tl)
    if m:
        tmin = int(m.group(1)) * 60 + int(m.group(2))
        starts = [9 * 60, 10 * 60, 11 * 60, 13 * 60, 14 * 60, 15 * 60]
        best_i, best_dist = 0, 10**9
        for i, st in enumerate(starts):
            dist = abs(tmin - st)
            if dist < best_dist:
                best_dist = dist
                best_i = i
        if best_dist <= 45:
            return best_i
    return None


def _class_display_name_from_values(year, name, division):
    name = (name or "").strip()
    division = (division or "").strip()

    base = name or division or f"Class-{year}"
    if name and division:
        normalized_name = re.sub(r"[^a-z0-9]+", "", name.lower())
        normalized_division = re.sub(r"[^a-z0-9]+", "", division.lower())
        if normalized_division and normalized_division not in normalized_name:
            base = f"{name}-{division}"

    return f"Y{year}-{base}"


def _class_name_aliases_from_values(year, name, division, department=None):
    year_text = str(year).strip()
    year_num = int(year) if str(year).strip().isdigit() else year
    raw_name = (name or "").strip()
    raw_division = (division or "").strip()
    raw_department = (department or "").strip()

    aliases = set()

    def _add(value):
        value = (value or "").strip()
        if value:
            aliases.add(value)

    display_name = _class_display_name_from_values(year_num, raw_name, raw_division)
    _add(display_name)
    _add(f"Y{year_text}-{raw_name}" if raw_name else "")
    _add(f"Y{year_text}-{raw_division}" if raw_division else "")
    if raw_department and raw_division:
        _add(f"Y{year_text}-{raw_department}-{raw_division}")
        _add(f"Y{year_text}-{raw_department}{raw_division}")
    if raw_name:
        _add(raw_name)
    if raw_division:
        _add(raw_division)
    if raw_department and raw_division:
        _add(f"{raw_department}-{raw_division}")
        _add(f"{raw_department}{raw_division}")

    normalized = set()
    for alias in aliases:
        normalized.add(alias)
        normalized.add(alias.replace(" ", ""))
        normalized.add(alias.replace(" ", "-").replace("--", "-"))
        normalized.add(alias.replace("_", "-"))
    return {a for a in normalized if a}


def _parse_bool(value):
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "y", "lab"}


def _normalize_unavailable_slots(raw):
    items = _parse_json_array(raw, []) if isinstance(raw, str) else (raw or [])
    normalized = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        day = _normalize_day(item.get("day"))
        try:
            slot_index = int(item.get("slot_index"))
        except (TypeError, ValueError):
            continue
        if day in DAYS and 0 <= slot_index < len(SLOTS):
            normalized.add((day, slot_index))
    return normalized


def _normalize_import_entries(entries, default_class_name):
    warnings = []
    default_class_name = (default_class_name or "").strip()
    normalized = []
    for e in entries or []:
        if not isinstance(e, dict):
            continue
        class_name = (e.get("class_name") or "").strip() or default_class_name
        if not class_name:
            continue
        day = _normalize_day(e.get("day"))
        if not day:
            continue
        slot_index = _resolve_slot_index(e)
        if slot_index is None:
            continue
        subject = (e.get("subject") or "").strip()
        if not subject:
            continue
        normalized.append(
            {
                "class_name": class_name,
                "day": day,
                "slot_index": slot_index,
                "subject": subject,
                "faculty": (e.get("faculty") or "").strip(),
                "room": (e.get("room") or "").strip(),
                "batch_name": (e.get("batch_name") or "").strip(),
                "is_lab": _parse_bool(e.get("is_lab", False)),
            }
        )

    if not normalized:
        if not default_class_name:
            warnings.append("No rows imported. Set a default class name if the file has no class column.")
        else:
            warnings.append("No valid rows after parsing. Check day names and time slots.")
    return normalized, warnings


def _get_class_catalog():
    rows = con.execute("SELECT id,name,year,department,division,strength FROM classes ORDER BY year,division,name").fetchall()
    out = {}
    for class_id, name, year, department, division, strength in rows:
        display_name = _class_display_name_from_values(year, name, division)
        out[display_name] = {
            "id": class_id,
            "name": name,
            "year": year,
            "department": department,
            "division": division,
            "strength": int(strength or 0),
        }
    return out


def _get_room_catalog():
    rows = con.execute("SELECT id,name,type,capacity,department FROM rooms ORDER BY name").fetchall()
    return {
        row[1]: {
            "id": row[0],
            "name": row[1],
            "type": row[2],
            "capacity": int(row[3] or 0),
            "department": row[4],
        }
        for row in rows
    }


def _get_faculty_catalog():
    rows = con.execute("SELECT id,name,department,max_day,max_week,allowed_years,subjects,unavailable FROM faculty ORDER BY name").fetchall()
    out = {}
    for row in rows:
        out[row[1]] = {
            "id": row[0],
            "name": row[1],
            "department": row[2],
            "max_day": int(row[3] or 4),
            "max_week": int(row[4] or 20),
            "allowed_years": _parse_json_array(row[5], [1, 2, 3, 4]),
            "subjects": _parse_json_array(row[6], []),
            "unavailable": _normalize_unavailable_slots(row[7]),
        }
    return out


def _room_capacity_required(class_name, batch_name, is_lab):
    classes = _get_class_catalog()
    class_meta = classes.get(class_name)
    if not class_meta:
        return None
    if is_lab:
        if batch_name:
            row = con.execute(
                "SELECT size FROM batches WHERE class_id=? AND batch_name=?",
                [class_meta["id"], batch_name],
            ).fetchone()
            if row and row[0]:
                return int(row[0])
        return int(class_meta["strength"] or 0)
    return int(class_meta["strength"] or 0)


def _validate_single_timetable_entry(year, entry, class_catalog=None, room_catalog=None, faculty_catalog=None):
    class_catalog = class_catalog or _get_class_catalog()
    room_catalog = room_catalog or _get_room_catalog()
    faculty_catalog = faculty_catalog or _get_faculty_catalog()
    issues = []
    class_name = _clean_text(entry.get("class_name"))
    faculty = _clean_text(entry.get("faculty"))
    room = _clean_text(entry.get("room"))
    day = _normalize_day(entry.get("day"))
    try:
        slot_index = int(entry.get("slot_index"))
    except (TypeError, ValueError):
        slot_index = -1
    batch_name = _clean_text(entry.get("batch_name"))
    is_lab = _parse_bool(entry.get("is_lab", False))
    subject_is_break = _clean_text(entry.get("subject")).lower() in {"break", "lunch break"}

    if _is_break_slot(slot_index):
        if not subject_is_break:
            issues.append("Selected slot is reserved for Lunch Break")
        return issues

    class_meta = class_catalog.get(class_name)
    if not class_meta:
        issues.append(f"Unknown class '{class_name}'")
    elif int(class_meta["year"]) != int(year):
        issues.append(f"Class {class_name} belongs to year {class_meta['year']}, not year {year}")

    if faculty:
        faculty_meta = faculty_catalog.get(faculty)
        if not faculty_meta:
            issues.append(f"Unknown faculty '{faculty}'")
        else:
            if int(year) not in {int(y) for y in faculty_meta["allowed_years"]}:
                issues.append(f"Faculty {faculty} is not allowed for year {year}")
            if day in DAYS and 0 <= slot_index < len(SLOTS) and (day, slot_index) in faculty_meta["unavailable"]:
                issues.append(f"Faculty {faculty} is unavailable on {day} slot {slot_index + 1}")
    if room:
        room_meta = room_catalog.get(room)
        if not room_meta:
            issues.append(f"Unknown room '{room}'")
        else:
            expected_type = "Lab" if is_lab else "Classroom"
            if room_meta["type"] != expected_type:
                issues.append(f"Room {room} is a {room_meta['type']}, expected {expected_type}")
            required_capacity = _room_capacity_required(class_name, batch_name, is_lab)
            if required_capacity and int(room_meta["capacity"] or 0) < int(required_capacity):
                issues.append(f"Room {room} capacity {room_meta['capacity']} is below required {required_capacity}")
    if is_lab and not batch_name:
        issues.append("Lab entry requires a batch name")
    if day not in DAYS:
        issues.append("Invalid day")
    if not (0 <= slot_index < len(SLOTS)):
        issues.append("Invalid slot index")
    return issues


def _preview_import_entries(year, entries, default_class_name):
    normalized, warnings = _normalize_import_entries(entries, default_class_name)
    class_catalog = _get_class_catalog()
    room_catalog = _get_room_catalog()
    faculty_catalog = _get_faculty_catalog()
    preview_rows = []
    skipped = max(0, len(entries or []) - len(normalized))
    for idx, row in enumerate(normalized, start=1):
        entry_issues = _validate_single_timetable_entry(year, row, class_catalog, room_catalog, faculty_catalog)
        preview_rows.append({
            "row_id": idx,
            **row,
            "time_label": SLOTS[row["slot_index"]],
            "validation_issues": entry_issues,
        })
    return preview_rows, warnings, skipped


def _insert_timetable_rows(year, rows, is_manual=True):
    next_id = con.execute("SELECT COALESCE(MAX(id),0)+1 FROM timetable").fetchone()[0]
    inserted = 0
    for row in rows:
        con.execute(
            """
            INSERT INTO timetable (
                id,class,faculty,subject,room,day,time_slot,locked,year,class_name,batch_name,slot_index,slot_label,is_lab,is_manual
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            [
                next_id,
                row["class_name"],
                row.get("faculty", ""),
                row["subject"],
                row.get("room", ""),
                row["day"],
                SLOTS[row["slot_index"]],
                False,
                year,
                row["class_name"],
                row.get("batch_name", ""),
                row["slot_index"],
                SLOTS[row["slot_index"]],
                _parse_bool(row.get("is_lab", False)),
                bool(is_manual),
            ],
        )
        next_id += 1
        inserted += 1
    return inserted


def _save_timetable_version(scope_year=None, label="Snapshot", note=""):
    where = ""
    params = []
    if scope_year is not None:
        where = "WHERE year=?"
        params = [int(scope_year)]
    rows = con.execute(
        f"""
        SELECT class,faculty,subject,room,day,time_slot,locked,year,class_name,batch_name,slot_index,slot_label,is_lab,is_manual
        FROM timetable
        {where}
        ORDER BY year,class_name,day,slot_index
        """,
        params,
    ).fetchall()
    if not rows:
        return None
    version_id = con.execute("SELECT COALESCE(MAX(id),0)+1 FROM timetable_versions").fetchone()[0]
    created_at = datetime.now().isoformat(timespec="seconds")
    con.execute(
        "INSERT INTO timetable_versions (id,label,note,scope_year,created_at,entry_count) VALUES (?,?,?,?,?,?)",
        [version_id, label, note, scope_year, created_at, len(rows)],
    )
    for row in rows:
        con.execute(
            """
            INSERT INTO timetable_version_items (
                version_id,class,faculty,subject,room,day,time_slot,locked,year,class_name,batch_name,slot_index,slot_label,is_lab,is_manual
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            [version_id, *row],
        )
    return version_id


def _restore_timetable_version(version_id):
    version = con.execute(
        "SELECT id,label,note,scope_year,created_at,entry_count FROM timetable_versions WHERE id=?",
        [version_id],
    ).fetchone()
    if not version:
        raise ValueError("Selected version does not exist")
    rows = con.execute(
        """
        SELECT class,faculty,subject,room,day,time_slot,locked,year,class_name,batch_name,slot_index,slot_label,is_lab,is_manual
        FROM timetable_version_items
        WHERE version_id=?
        ORDER BY year,class_name,day,slot_index
        """,
        [version_id],
    ).fetchall()
    scope_year = version[3]
    if scope_year is None:
        con.execute("DELETE FROM timetable")
    else:
        con.execute("DELETE FROM timetable WHERE year=?", [scope_year])
    next_id = con.execute("SELECT COALESCE(MAX(id),0)+1 FROM timetable").fetchone()[0]
    for row in rows:
        con.execute(
            """
            INSERT INTO timetable (
                id,class,faculty,subject,room,day,time_slot,locked,year,class_name,batch_name,slot_index,slot_label,is_lab,is_manual
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            [next_id, *row],
        )
        next_id += 1
    return {
        "id": version[0],
        "label": version[1],
        "note": version[2],
        "scope_year": version[3],
        "created_at": version[4],
        "entry_count": version[5],
    }


def _apply_extracted_timetable(year, entries, default_class_name):
    preview_rows, warnings, skipped_parse = _preview_import_entries(year, entries, default_class_name)
    valid_rows = []
    locked_conflicts = 0
    for row in preview_rows:
        if row["validation_issues"]:
            continue
        if _is_parallel_batch_lab(row.get("batch_name"), row.get("is_lab")):
            locked = con.execute(
                """
                SELECT COUNT(*)
                FROM timetable
                WHERE year=? AND class_name=? AND day=? AND slot_index=? AND locked=TRUE
                  AND (COALESCE(batch_name,'')=? OR COALESCE(batch_name,'')='' OR UPPER(COALESCE(batch_name,''))='ALL' OR is_lab=FALSE)
                """,
                [year, row["class_name"], row["day"], row["slot_index"], row["batch_name"]],
            ).fetchone()[0]
        else:
            locked = con.execute(
                "SELECT COUNT(*) FROM timetable WHERE year=? AND class_name=? AND day=? AND slot_index=? AND locked=TRUE",
                [year, row["class_name"], row["day"], row["slot_index"]],
            ).fetchone()[0]
        if locked:
            locked_conflicts += 1
            continue
        valid_rows.append({
            "class_name": row["class_name"],
            "faculty": row["faculty"],
            "subject": row["subject"],
            "room": row["room"],
            "day": row["day"],
            "slot_index": row["slot_index"],
            "batch_name": row["batch_name"],
            "is_lab": row["is_lab"],
        })

    if locked_conflicts:
        warnings.append(f"Skipped {locked_conflicts} imported row(s) because the target slot is locked.")
    if not valid_rows:
        return 0, 0, skipped_parse, warnings

    by_key = {}
    for row in valid_rows:
        if _is_parallel_batch_lab(row.get("batch_name"), row.get("is_lab")):
            key = (row["class_name"], row["day"], row["slot_index"], row["batch_name"])
        else:
            key = (row["class_name"], row["day"], row["slot_index"])
        by_key[key] = row
    rows = list(by_key.values())
    touched_classes = sorted({row["class_name"] for row in rows})
    _save_timetable_version(scope_year=year, label=f"Backup before AI import Y{year}", note=", ".join(touched_classes[:4]))
    con.execute("DELETE FROM timetable WHERE locked=FALSE AND year=?", [year])
    inserted = _insert_timetable_rows(year, rows, is_manual=True)
    return inserted, len(rows), skipped_parse, warnings


def _split_imported_class_label(class_label, fallback_year):
    label = _clean_text(class_label)
    year = int(fallback_year)
    division = ""
    name = label or f"Year {year}"
    m = re.match(r"^\s*y\s*(\d)\s*[-_\s]+(.+)$", label, flags=re.IGNORECASE)
    if m:
        try:
            year = int(m.group(1))
        except (TypeError, ValueError):
            year = int(fallback_year)
        division = _clean_text(m.group(2))
        name = division or f"Year {year}"
    else:
        division = label
    if not division:
        division = name
    return year, name, division


def _get_or_create_class(imported_class_name, year, summary):
    _parsed_year, name, division = _split_imported_class_label(imported_class_name, year)
    target_year = int(year)
    requested_display = _class_display_name_from_values(target_year, name, division)
    requested_key = _normalized_key(requested_display)
    rows = con.execute("SELECT id,name,year,department,division,strength FROM classes").fetchall()
    for class_id, c_name, c_year, _dept, c_division, _strength in rows:
        display_name = _class_display_name_from_values(c_year, c_name, c_division)
        if _normalized_key(display_name) == requested_key:
            summary["classes"]["reused"] += 1
            return {
                "id": class_id,
                "year": int(c_year),
                "name": c_name,
                "division": c_division,
                "display_name": display_name,
            }

    next_id = con.execute("SELECT COALESCE(MAX(id),0)+1 FROM classes").fetchone()[0]
    con.execute(
        "INSERT INTO classes (id,name,year,department,division,strength) VALUES (?,?,?,?,?,?)",
        [next_id, name, target_year, "Imported", division, 60],
    )
    summary["classes"]["created"] += 1
    return {
        "id": next_id,
        "year": target_year,
        "name": name,
        "division": division,
        "display_name": _class_display_name_from_values(target_year, name, division),
    }


def _get_or_create_batch(class_id, batch_name, is_lab, summary):
    cleaned = _clean_text(batch_name)
    if not cleaned and not is_lab:
        return ""
    normalized = cleaned or "ALL"
    key = _normalized_key(normalized)
    rows = con.execute("SELECT id,batch_name,size FROM batches WHERE class_id=?", [class_id]).fetchall()
    for batch_id, existing_name, _size in rows:
        if _normalized_key(existing_name) == key:
            summary["batches"]["reused"] += 1
            return existing_name

    class_strength = con.execute("SELECT strength FROM classes WHERE id=?", [class_id]).fetchone()
    size = int((class_strength[0] if class_strength else 60) or 60)
    next_id = con.execute("SELECT COALESCE(MAX(id),0)+1 FROM batches").fetchone()[0]
    class_name = con.execute("SELECT name FROM classes WHERE id=?", [class_id]).fetchone()
    con.execute(
        "INSERT INTO batches (id,class,batch_name,size,class_id) VALUES (?,?,?,?,?)",
        [next_id, class_name[0] if class_name else "Imported", normalized, size, class_id],
    )
    summary["batches"]["created"] += 1
    return normalized


def _get_or_create_faculty(faculty_name, year, summary):
    cleaned = _clean_text(faculty_name) or "TBD Faculty"
    key = _normalized_key(cleaned)
    rows = con.execute("SELECT id,name,allowed_years FROM faculty").fetchall()
    for faculty_id, existing_name, allowed_years_raw in rows:
        if _normalized_key(existing_name) == key:
            allowed_years = sorted({int(y) for y in _parse_json_array(allowed_years_raw, [1, 2, 3, 4]) if str(y).strip()})
            if int(year) not in allowed_years:
                allowed_years.append(int(year))
                allowed_years = sorted(set(allowed_years))
                con.execute("UPDATE faculty SET allowed_years=? WHERE id=?", [json.dumps(allowed_years), faculty_id])
            summary["faculties"]["reused"] += 1
            return existing_name

    next_id = con.execute("SELECT COALESCE(MAX(id),0)+1 FROM faculty").fetchone()[0]
    con.execute(
        """
        INSERT INTO faculty (id,name,department,subjects,max_day,max_week,unavailable,allowed_years)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [next_id, cleaned, "Imported", json.dumps([]), 4, 20, json.dumps([]), json.dumps([int(year)])],
    )
    summary["faculties"]["created"] += 1
    return cleaned


def _subject_fallback_code(name):
    letters = re.sub(r"[^A-Za-z0-9]", "", _clean_text(name).upper())[:10] or "SUBJ"
    return f"IMP-{letters}"


def _get_or_create_subject(subject_name, year, faculty_name, is_lab, summary):
    cleaned = _clean_text(subject_name)
    key = _normalized_key(cleaned)
    rows = con.execute("SELECT id,name,year,type,faculty FROM subjects").fetchall()
    for subject_id, existing_name, existing_year, existing_type, existing_faculty in rows:
        if int(existing_year or 0) == int(year) and _normalized_key(existing_name) == key:
            updates = []
            params = []
            if _clean_text(existing_faculty) != _clean_text(faculty_name):
                updates.append("faculty=?")
                params.append(faculty_name)
            desired_type = "Lab" if is_lab else "Theory"
            if _clean_text(existing_type) != desired_type:
                updates.append("type=?")
                params.append(desired_type)
                updates.append("continuous_slots=?")
                params.append(2 if is_lab else 1)
                updates.append("duration=?")
                params.append("2hr" if is_lab else "1hr")
            if updates:
                params.append(subject_id)
                con.execute(f"UPDATE subjects SET {', '.join(updates)} WHERE id=?", params)
            summary["subjects"]["reused"] += 1
            return existing_name

    next_id = con.execute("SELECT COALESCE(MAX(id),0)+1 FROM subjects").fetchone()[0]
    subject_type = "Lab" if is_lab else "Theory"
    con.execute(
        """
        INSERT INTO subjects (id,name,code,type,weekly_lectures,lab_hours,duration,priority,faculty,year,weekly_sessions,continuous_slots)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            next_id,
            cleaned,
            _subject_fallback_code(cleaned),
            subject_type,
            1,
            2 if is_lab else 0,
            "2hr" if is_lab else "1hr",
            "Medium",
            faculty_name,
            int(year),
            1,
            2 if is_lab else 1,
        ],
    )
    summary["subjects"]["created"] += 1
    return cleaned


def _get_or_create_room(room_name, is_lab, summary):
    cleaned = _clean_text(room_name) or ("Imported Lab" if is_lab else "Imported Classroom")
    key = _normalized_key(cleaned)
    rows = con.execute("SELECT id,name,type,capacity FROM rooms").fetchall()
    for room_id, existing_name, existing_type, existing_capacity in rows:
        if _normalized_key(existing_name) == key:
            desired_type = _normalize_room_type(existing_name, fallback_lab=is_lab)
            if _clean_text(existing_type) != desired_type:
                con.execute("UPDATE rooms SET type=? WHERE id=?", [desired_type, room_id])
            if int(existing_capacity or 0) < 1:
                con.execute("UPDATE rooms SET capacity=? WHERE id=?", [60, room_id])
            summary["rooms"]["reused"] += 1
            return existing_name

    next_id = con.execute("SELECT COALESCE(MAX(id),0)+1 FROM rooms").fetchone()[0]
    con.execute(
        "INSERT INTO rooms (id,name,type,capacity,department) VALUES (?,?,?,?,?)",
        [next_id, cleaned, _normalize_room_type(cleaned, fallback_lab=is_lab), 60 if not is_lab else 40, "Imported"],
    )
    summary["rooms"]["created"] += 1
    return cleaned


def _resolve_import_rows_with_resources(year, entries):
    summary = {
        "classes": {"created": 0, "reused": 0},
        "batches": {"created": 0, "reused": 0},
        "faculties": {"created": 0, "reused": 0},
        "subjects": {"created": 0, "reused": 0},
        "rooms": {"created": 0, "reused": 0},
        "timetable_entries": {"inserted": 0, "unique_slots": 0, "skipped_locked": 0, "skipped_invalid": 0},
    }
    warnings = []
    resolved_rows = []
    for idx, row in enumerate(entries, start=1):
        class_name = _clean_text(row.get("class_name"))
        day = _normalize_day(row.get("day"))
        try:
            slot_index = int(row.get("slot_index"))
        except (TypeError, ValueError):
            slot_index = -1
        subject = _clean_text(row.get("subject"))
        is_lab = _parse_bool(row.get("is_lab", False))
        if not class_name or day not in DAYS or not (0 <= slot_index < len(SLOTS)) or not subject:
            summary["timetable_entries"]["skipped_invalid"] += 1
            warnings.append(f"Skipped row {idx}: missing class/day/slot/subject")
            continue

        if _is_break_slot(slot_index):
            if _normalized_key(subject) not in {"break", "lunchbreak"}:
                summary["timetable_entries"]["skipped_invalid"] += 1
                warnings.append(f"Skipped row {idx}: slot is reserved for {BREAK_LABEL}")
                continue
            resolved_rows.append(
                {
                    "class_name": class_name,
                    "faculty": "",
                    "subject": BREAK_LABEL,
                    "room": "",
                    "day": day,
                    "slot_index": slot_index,
                    "batch_name": "",
                    "is_lab": False,
                }
            )
            continue

        class_ref = _get_or_create_class(class_name, year, summary)
        batch_name = _get_or_create_batch(class_ref["id"], row.get("batch_name"), is_lab, summary)
        faculty_name = _get_or_create_faculty(row.get("faculty"), class_ref["year"], summary)
        subject_name = _get_or_create_subject(subject, class_ref["year"], faculty_name, is_lab, summary)
        room_name = _get_or_create_room(row.get("room"), is_lab, summary)

        resolved_rows.append(
            {
                "class_name": class_ref["display_name"],
                "faculty": faculty_name,
                "subject": subject_name,
                "room": room_name,
                "day": day,
                "slot_index": slot_index,
                "batch_name": batch_name,
                "is_lab": is_lab,
            }
        )
    return resolved_rows, summary, warnings


def _table_columns(table_name):
    return {row[1] for row in con.execute(f"PRAGMA table_info('{table_name}')").fetchall()}


def _ensure_column(table_name, column_name, column_type):
    if column_name not in _table_columns(table_name):
        con.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_type}")


def _clean_text(value):
    return str(value or "").strip()


def _normalized_key(value):
    return re.sub(r"[^a-z0-9]+", "", _clean_text(value).lower())


def _normalize_room_type(name, fallback_lab=False):
    text = _clean_text(name).lower()
    if fallback_lab or any(token in text for token in ["lab", "laboratory", "practical", "workshop"]):
        return "Lab"
    return "Classroom"


def _require_text(data, key, label):
    value = _clean_text(data.get(key))
    if not value:
        raise ValueError(f"{label} is required")
    return value


def _require_int(data, key, label, minimum=None, maximum=None):
    raw = data.get(key)
    if raw is None or str(raw).strip() == "":
        raise ValueError(f"{label} is required")
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise ValueError(f"{label} must be a whole number")
    if minimum is not None and value < minimum:
        raise ValueError(f"{label} must be at least {minimum}")
    if maximum is not None and value > maximum:
        raise ValueError(f"{label} must be at most {maximum}")
    return value


def _validate_subject_rules(subject_type, weekly_sessions, continuous_slots):
    if subject_type == "Lab":
        if continuous_slots != 2:
            raise ValueError("Lab duration must be exactly 2 continuous hours")
        if weekly_sessions < 1:
            raise ValueError("Lab sessions per week must be at least 1")
    else:
        if continuous_slots != 1:
            raise ValueError("Theory lecture duration must be exactly 1 hour")
        if weekly_sessions < 1:
            raise ValueError("Theory lectures per week must be at least 1")


def _subject_scheduled_same_day(class_name, subject, day, year, batch_name="", exclude_slot_index=None):
    query = """
        SELECT COUNT(*)
        FROM timetable
        WHERE year=? AND class_name=? AND subject=? AND day=? AND COALESCE(batch_name,'')=?
    """
    params = [year, class_name, subject, day, batch_name or ""]
    if exclude_slot_index is not None:
        query += " AND slot_index<>?"
        params.append(exclude_slot_index)
    return con.execute(query, params).fetchone()[0] > 0


def init_db():
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS classes(
            id INTEGER,
            name TEXT,
            year INTEGER,
            department TEXT
        )
        """
    )
    _ensure_column("classes", "division", "TEXT DEFAULT ''")
    _ensure_column("classes", "strength", "INTEGER DEFAULT 60")

    con.execute(
        """
        CREATE TABLE IF NOT EXISTS faculty(
            id INTEGER,
            name TEXT,
            department TEXT,
            subjects TEXT,
            max_day INTEGER,
            max_week INTEGER,
            unavailable TEXT
        )
        """
    )
    _ensure_column("faculty", "allowed_years", "TEXT DEFAULT '[1,2,3,4]'")

    con.execute(
        """
        CREATE TABLE IF NOT EXISTS subjects(
            id INTEGER,
            name TEXT,
            code TEXT,
            type TEXT,
            weekly_lectures INTEGER,
            lab_hours INTEGER,
            duration TEXT,
            priority TEXT,
            faculty TEXT
        )
        """
    )
    _ensure_column("subjects", "year", "INTEGER DEFAULT 1")
    _ensure_column("subjects", "weekly_sessions", "INTEGER DEFAULT 1")
    _ensure_column("subjects", "continuous_slots", "INTEGER DEFAULT 1")

    con.execute(
        """
        CREATE TABLE IF NOT EXISTS rooms(
            id INTEGER,
            name TEXT,
            type TEXT,
            capacity INTEGER,
            department TEXT
        )
        """
    )

    con.execute(
        """
        CREATE TABLE IF NOT EXISTS batches(
            id INTEGER,
            class TEXT,
            batch_name TEXT,
            size INTEGER
        )
        """
    )
    _ensure_column("batches", "class_id", "INTEGER")

    con.execute(
        """
        CREATE TABLE IF NOT EXISTS timetable(
            id INTEGER,
            class TEXT,
            faculty TEXT,
            subject TEXT,
            room TEXT,
            day TEXT,
            time_slot TEXT,
            locked BOOLEAN DEFAULT FALSE
        )
        """
    )
    _ensure_column("timetable", "year", "INTEGER DEFAULT 1")
    _ensure_column("timetable", "class_name", "TEXT DEFAULT ''")
    _ensure_column("timetable", "batch_name", "TEXT DEFAULT ''")
    _ensure_column("timetable", "slot_index", "INTEGER DEFAULT 0")
    _ensure_column("timetable", "slot_label", "TEXT DEFAULT ''")
    _ensure_column("timetable", "is_lab", "BOOLEAN DEFAULT FALSE")
    _ensure_column("timetable", "is_manual", "BOOLEAN DEFAULT FALSE")

    con.execute(
        """
        CREATE TABLE IF NOT EXISTS timetable_versions(
            id INTEGER,
            label TEXT,
            note TEXT,
            scope_year INTEGER,
            created_at TEXT,
            entry_count INTEGER
        )
        """
    )
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS timetable_version_items(
            version_id INTEGER,
            class TEXT,
            faculty TEXT,
            subject TEXT,
            room TEXT,
            day TEXT,
            time_slot TEXT,
            locked BOOLEAN DEFAULT FALSE,
            year INTEGER,
            class_name TEXT,
            batch_name TEXT,
            slot_index INTEGER,
            slot_label TEXT,
            is_lab BOOLEAN DEFAULT FALSE,
            is_manual BOOLEAN DEFAULT FALSE
        )
        """
    )
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS scheduler_rules(
            id INTEGER,
            text TEXT,
            rule_type TEXT,
            scope_year INTEGER,
            rule_json TEXT,
            created_at TEXT,
            active BOOLEAN DEFAULT TRUE
        )
        """
    )


def _parse_json_array(raw, fallback=None):
    if raw is None or raw == "":
        return fallback or []
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, list):
            return parsed
    except Exception:
        pass
    return fallback or []


def _class_display_name(c):
    return _class_display_name_from_values(c["year"], c["name"], c["division"])


def _faculty_allowed_for_year(faculty_name, year, faculty_meta):
    f = faculty_meta.get(faculty_name)
    if not f:
        return False
    return year in f["allowed_years"]


def _faculty_matches_class_department(faculty_name, class_department, faculty_meta):
    f = faculty_meta.get(faculty_name)
    if not f:
        return False
    faculty_department = _clean_text(f.get("department"))
    class_department = _clean_text(class_department)
    if not faculty_department or not class_department:
        return True
    return faculty_department.lower() in {class_department.lower(), "general", "common"}


def _eligible_faculty_for_subject(subject_name, preferred_faculty, class_year, class_department, faculty_meta):
    preferred_faculty = _clean_text(preferred_faculty)
    normalized_subject = _normalized_key(subject_name)
    eligible = []
    for faculty_name, meta in faculty_meta.items():
        if not _faculty_allowed_for_year(faculty_name, class_year, faculty_meta):
            continue
        if not _faculty_matches_class_department(faculty_name, class_department, faculty_meta):
            continue
        taught_subjects = {_normalized_key(s) for s in meta.get("subjects", []) if _clean_text(s)}
        if normalized_subject and normalized_subject not in taught_subjects and faculty_name != preferred_faculty:
            continue
        eligible.append(faculty_name)
    if preferred_faculty and preferred_faculty in faculty_meta and preferred_faculty not in eligible:
        if _faculty_allowed_for_year(preferred_faculty, class_year, faculty_meta) and _faculty_matches_class_department(preferred_faculty, class_department, faculty_meta):
            eligible.insert(0, preferred_faculty)
    if preferred_faculty and preferred_faculty in eligible:
        eligible = [preferred_faculty] + [f for f in eligible if f != preferred_faculty]
    return eligible


def _pick_room(rooms, room_type, required_capacity, occupied_rooms, day_idx, slot_block):
    for r in rooms:
        if r["type"] != room_type:
            continue
        if r["capacity"] < required_capacity:
            continue
        room_occupied = occupied_rooms.get(r["name"], set())
        if all((day_idx, slot) not in room_occupied for slot in slot_block):
            return r["name"]
    return None


def _is_parallel_batch_lab(batch_name, is_lab=True):
    batch = _clean_text(batch_name)
    return bool(is_lab and batch and batch.upper() != "ALL")

def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("auth"):
            return redirect(url_for("login_page"))
        return view(*args, **kwargs)

    return wrapped


def _seed_from_locked(occupied, faculty_day_load, faculty_week_load):
    rows = con.execute(
        """
        SELECT class_name,batch_name,faculty,subject,room,day,slot_index,is_lab
        FROM timetable
        WHERE locked=TRUE
        """
    ).fetchall()
    for class_name, batch_name, faculty, subject, room, day, slot_idx, is_lab in rows:
        day_idx = DAYS.index(day) if day in DAYS else -1
        if day_idx < 0:
            continue
        if _is_parallel_batch_lab(batch_name, is_lab):
            occupied["class_lab"].setdefault(class_name, set()).add((day_idx, slot_idx))
            occupied["batch"].setdefault((class_name, batch_name), set()).add((day_idx, slot_idx))
        else:
            occupied["class_full"].setdefault(class_name, set()).add((day_idx, slot_idx))
            if batch_name:
                occupied["batch"].setdefault((class_name, batch_name), set()).add((day_idx, slot_idx))
        occupied["faculty"].setdefault(faculty, set()).add((day_idx, slot_idx))
        if subject:
            occupied["subject_day"].setdefault((class_name, batch_name, day), set()).add(subject)
        if room:
            occupied["room"].setdefault(room, set()).add((day_idx, slot_idx))
        faculty_day_load.setdefault(faculty, {i: 0 for i in range(len(DAYS))})[day_idx] += 1
        faculty_week_load[faculty] = faculty_week_load.get(faculty, 0) + 1

def _build_requirements(scope_year=None):
    classes_rows = con.execute(
        "SELECT id,name,year,department,division,strength FROM classes ORDER BY year,division,name"
    ).fetchall()
    classes = [
        {
            "id": r[0],
            "name": r[1],
            "year": r[2],
            "department": r[3],
            "division": r[4],
            "strength": int(r[5] or 0),
        }
        for r in classes_rows
        if scope_year is None or r[2] == scope_year
    ]

    faculty_rows = con.execute(
        "SELECT name,department,max_day,max_week,allowed_years,subjects,unavailable FROM faculty ORDER BY name"
    ).fetchall()
    faculty_meta = {
        r[0]: {
            "department": r[1],
            "max_day": int(r[2] or 4),
            "max_week": int(r[3] or 20),
            "allowed_years": _parse_json_array(r[4], [1, 2, 3, 4]),
            "subjects": [str(x).strip() for x in _parse_json_array(r[5], []) if str(x).strip()],
            "unavailable": _normalize_unavailable_slots(r[6]),
        }
        for r in faculty_rows
    }

    subjects_rows = con.execute(
        "SELECT name,code,type,faculty,year,weekly_sessions,continuous_slots FROM subjects ORDER BY type DESC, year, name"
    ).fetchall()
    subjects = [
        {
            "name": r[0],
            "code": r[1],
            "type": r[2],
            "faculty": r[3],
            "year": r[4],
            "weekly_sessions": max(int(r[5] or 1), 1),
            "continuous_slots": max(int(r[6] or 1), 1),
        }
        for r in subjects_rows
        if scope_year is None or r[4] == scope_year
    ]

    rooms_rows = con.execute("SELECT name,type,capacity FROM rooms ORDER BY capacity DESC").fetchall()
    rooms = [{"name": r[0], "type": r[1], "capacity": int(r[2] or 0)} for r in rooms_rows]

    batches_rows = con.execute("SELECT class_id,batch_name,size FROM batches").fetchall()
    batches_by_class = {}
    for class_id, batch_name, size in batches_rows:
        batches_by_class.setdefault(class_id, []).append({"batch_name": batch_name, "size": int(size or 0)})

    demands = []
    requirement_counter = {}
    for c in classes:
        year_subjects = [
            s for s in subjects
            if s["year"] == c["year"]
            and _faculty_matches_class_department(s["faculty"], c["department"], faculty_meta)
        ]
        for s in year_subjects:
            eligible_faculty = _eligible_faculty_for_subject(s["name"], s["faculty"], c["year"], c["department"], faculty_meta)
            if not eligible_faculty:
                continue
            preferred_faculty = eligible_faculty[0]
            for session_idx in range(s["weekly_sessions"]):
                if s["type"] == "Lab":
                    class_batches = batches_by_class.get(c["id"], []) or [{"batch_name": "ALL", "size": c["strength"]}]
                    parallel_group = f"{c['id']}::{s['name']}::{preferred_faculty}::{session_idx}"
                    for b in class_batches:
                        d = {
                            "year": c["year"],
                            "class_name": _class_display_name(c),
                            "batch_name": b["batch_name"],
                            "batch_size": b["size"],
                            "subject": s["name"],
                            "faculty": preferred_faculty,
                            "preferred_faculty": preferred_faculty,
                            "faculty_options": [preferred_faculty],
                            "is_lab": True,
                            "duration": 2,
                            "parallel_group": parallel_group,
                        }
                        demands.append(d)
                        req_key = (d["year"], d["class_name"], d["batch_name"], d["subject"], d["faculty"], d["is_lab"], d["duration"])
                        requirement_counter[req_key] = requirement_counter.get(req_key, 0) + 1
                else:
                    d = {
                        "year": c["year"],
                        "class_name": _class_display_name(c),
                        "batch_name": "",
                        "batch_size": c["strength"],
                        "subject": s["name"],
                        "faculty": preferred_faculty,
                        "preferred_faculty": preferred_faculty,
                        "faculty_options": eligible_faculty,
                        "is_lab": False,
                        "duration": 1,
                        "parallel_group": None,
                    }
                    demands.append(d)
                    req_key = (d["year"], d["class_name"], d["batch_name"], d["subject"], d["faculty"], d["is_lab"], d["duration"])
                    requirement_counter[req_key] = requirement_counter.get(req_key, 0) + 1

    demands.sort(
        key=lambda d: (
            0 if d["is_lab"] else 1,
            d["year"],
            d["class_name"],
            d["subject"],
            d["faculty"],
            d["batch_name"],
            -d["duration"],
        )
    )
    return classes, faculty_meta, rooms, demands, requirement_counter


def _generation_readiness(scope_year=None):
    classes, faculty_meta, rooms, demands, requirement_counter = _build_requirements(scope_year)
    subjects_rows = con.execute(
        "SELECT name, faculty, year, type FROM subjects ORDER BY year, name"
    ).fetchall()
    if scope_year is not None:
        subjects_rows = [r for r in subjects_rows if int(r[2]) == int(scope_year)]
    missing = []
    if not classes:
        missing.append("classes")
    if not rooms:
        missing.append("rooms")
    if not subjects_rows:
        missing.append("subjects")
    if not faculty_meta:
        missing.append("faculty")

    invalid_subjects = []
    for name, faculty, year, subj_type in subjects_rows:
        if not faculty:
            invalid_subjects.append(f"{name} (Year {year}) has no faculty assigned")
            continue
        meta = faculty_meta.get(faculty)
        if not meta:
            invalid_subjects.append(f"{name} (Year {year}) references missing faculty '{faculty}'")
            continue
        if int(year) not in meta.get('allowed_years', []):
            invalid_subjects.append(f"{name} (Year {year}) uses faculty '{faculty}' but that year is not allowed")

    if subjects_rows and not demands:
        missing.append("schedulable subject mappings")

    return {
        "classes": classes,
        "faculty_meta": faculty_meta,
        "rooms": rooms,
        "demands": demands,
        "requirement_counter": requirement_counter,
        "subjects_count": len(subjects_rows),
        "missing": missing,
        "invalid_subjects": invalid_subjects[:15],
    }


def _solve_with_backtracking(demands, faculty_meta, rooms, scheduler_rules=None):
    scheduler_rules = scheduler_rules or []
    occupied = {"class_full": {}, "class_lab": {}, "batch": {}, "faculty": {}, "room": {}, "subject_day": {}}
    faculty_day_load = {f: {i: 0 for i in range(len(DAYS))} for f in faculty_meta}
    faculty_week_load = {f: 0 for f in faculty_meta}
    _seed_from_locked(occupied, faculty_day_load, faculty_week_load)
    placements = [None] * len(demands)
    nodes = {"count": 0}
    max_nodes = 2000000
    parallel_groups = {}
    for idx, demand in enumerate(demands):
        group_key = demand.get("parallel_group")
        if group_key and _is_parallel_batch_lab(demand.get("batch_name"), demand.get("is_lab")):
            parallel_groups.setdefault(group_key, []).append(idx)

    def _parallel_lab_alignment_score(d, day_idx, block):
        if not _is_parallel_batch_lab(d.get("batch_name"), d.get("is_lab")):
            return 0
        class_lab_slots = occupied["class_lab"].get(d["class_name"], set())
        overlap = sum(1 for s in block if (day_idx, s) in class_lab_slots)
        if overlap == len(block):
            aligned_batches = 0
            for (class_name, _batch_name), batch_slots in occupied["batch"].items():
                if class_name != d["class_name"]:
                    continue
                if all((day_idx, s) in batch_slots for s in block):
                    aligned_batches += 1
            return 30 + aligned_batches * 5
        if overlap == 0:
            return 0
        return -20

    def _available_lab_rooms(required_capacity, day_idx, block, reserved_rooms=None):
        reserved_rooms = reserved_rooms or set()
        candidates = []
        for room in rooms:
            if room["type"] != "Lab":
                continue
            if room["capacity"] < required_capacity:
                continue
            if room["name"] in reserved_rooms:
                continue
            room_occupied = occupied["room"].get(room["name"], set())
            if all((day_idx, slot) not in room_occupied for slot in block):
                candidates.append(room)
        candidates.sort(key=lambda r: (r["capacity"], r["name"]))
        return candidates

    def _parallel_group_feasible(group_key, day_idx, start):
        group_indices = parallel_groups.get(group_key, [])
        if not group_indices:
            return False, {}
        block = list(range(start, start + demands[group_indices[0]]["duration"]))
        if not _block_is_clock_continuous(block):
            return False, {}
        if any(_is_break_slot(slot_index) for slot_index in block):
            return False, {}
        day_name = DAYS[day_idx]
        assigned_rooms = {}
        reserved_rooms = set()
        temp_day_increment = {}
        temp_week_increment = {}
        temp_faculty_slots = set()
        placed_signature = None

        for gi in group_indices:
            gp = placements[gi]
            if not gp:
                continue
            current_signature = (gp["day_idx"], gp["start"])
            if placed_signature is None:
                placed_signature = current_signature
            elif placed_signature != current_signature:
                return False, {}
            if current_signature != (day_idx, start):
                return False, {}
            assigned_rooms[gi] = gp["room"]
            reserved_rooms.add(gp["room"])

        pending = sorted(
            [gi for gi in group_indices if not placements[gi]],
            key=lambda gi: (-int(demands[gi].get("batch_size") or 0), demands[gi].get("batch_name") or "")
        )

        for gi in pending:
            gd = demands[gi]
            if gd["faculty"] not in faculty_meta:
                return False, {}
            f_lim = faculty_meta[gd["faculty"]]
            projected_day_load = faculty_day_load[gd["faculty"]][day_idx] + temp_day_increment.get((gd["faculty"], day_idx), 0)
            projected_week_load = faculty_week_load[gd["faculty"]] + temp_week_increment.get(gd["faculty"], 0)
            if projected_day_load >= f_lim["max_day"]:
                return False, {}
            if projected_week_load >= f_lim["max_week"]:
                return False, {}
            if any((day_idx, s) in occupied["class_full"].get(gd["class_name"], set()) for s in block):
                return False, {}
            if gd["batch_name"] and any((day_idx, s) in occupied["batch"].get((gd["class_name"], gd["batch_name"]), set()) for s in block):
                return False, {}
            if any((day_idx, s) in occupied["faculty"].get(gd["faculty"], set()) for s in block):
                return False, {}
            if any((gd["faculty"], day_idx, s) in temp_faculty_slots for s in block):
                return False, {}
            class_day_subjects = occupied.setdefault("subject_day", {}).get((gd["class_name"], gd["batch_name"], day_name), set())
            if gd["subject"] in class_day_subjects:
                return False, {}
            if any((day_name, s) in f_lim.get("unavailable", set()) for s in block):
                return False, {}
            if _candidate_blocked_by_scheduler_rules(gd, day_name, block, scheduler_rules):
                return False, {}
            room_choices = _available_lab_rooms(gd["batch_size"], day_idx, block, reserved_rooms=reserved_rooms)
            if not room_choices:
                return False, {}
            chosen_room = room_choices[0]["name"]
            assigned_rooms[gi] = chosen_room
            reserved_rooms.add(chosen_room)
            temp_day_increment[(gd["faculty"], day_idx)] = temp_day_increment.get((gd["faculty"], day_idx), 0) + len(block)
            temp_week_increment[gd["faculty"]] = temp_week_increment.get(gd["faculty"], 0) + len(block)
            for s in block:
                temp_faculty_slots.add((gd["faculty"], day_idx, s))

        return True, assigned_rooms

    def candidate_slots(d, idx=None):
        cands = []
        faculty_options = d.get("faculty_options") or [d.get("faculty")]
        faculty_options = [f for f in faculty_options if f in faculty_meta]
        if not faculty_options:
            return cands
        parallel_lab = _is_parallel_batch_lab(d.get("batch_name"), d.get("is_lab"))
        parallel_group = d.get("parallel_group") if parallel_lab else None
        group_indices = parallel_groups.get(parallel_group, []) if parallel_group else []
        fixed_signature = None
        group_has_fallback_placement = False
        if group_indices:
            signatures = {
                (placements[gi]["day_idx"], placements[gi]["start"])
                for gi in group_indices
                if placements[gi] and placements[gi].get("sync_lock")
            }
            if len(signatures) > 1:
                return cands
            if signatures:
                fixed_signature = next(iter(signatures))
            group_has_fallback_placement = any(
                placements[gi] and not placements[gi].get("sync_lock")
                for gi in group_indices
            )
        strict_sync_candidates = []
        fallback_candidates = []
        for faculty_name in faculty_options:
            f_lim = faculty_meta[faculty_name]
            faculty_bonus = 5 if faculty_name == d.get("preferred_faculty") else 0
            for day_idx in range(len(DAYS)):
                if faculty_day_load[faculty_name][day_idx] >= f_lim["max_day"]:
                    continue
                if faculty_week_load[faculty_name] >= f_lim["max_week"]:
                    continue
                for start in range(0, len(SLOTS) - d["duration"] + 1):
                    if fixed_signature and (day_idx, start) != fixed_signature:
                        continue
                    block = list(range(start, start + d["duration"]))
                    if not _block_is_clock_continuous(block):
                        continue
                    if any(_is_break_slot(slot_index) for slot_index in block):
                        continue
                    if any((day_idx, s) in occupied["class_full"].get(d["class_name"], set()) for s in block):
                        continue
                    if not parallel_lab and any((day_idx, s) in occupied["class_lab"].get(d["class_name"], set()) for s in block):
                        continue
                    if d["batch_name"] and any((day_idx, s) in occupied["batch"].get((d["class_name"], d["batch_name"]), set()) for s in block):
                        continue
                    if any((day_idx, s) in occupied["faculty"].get(faculty_name, set()) for s in block):
                        continue
                    day_name = DAYS[day_idx]
                    class_day_subjects = occupied.setdefault("subject_day", {}).get((d["class_name"], d["batch_name"], day_name), set())
                    if d["subject"] in class_day_subjects:
                        continue
                    if any((day_name, s) in f_lim.get("unavailable", set()) for s in block):
                        continue
                    if _candidate_blocked_by_scheduler_rules(d, day_name, block, scheduler_rules):
                        continue

                    base_score = _candidate_scheduler_rule_score(d, day_name, block, scheduler_rules) + faculty_bonus
                    alignment_score = _parallel_lab_alignment_score(d, day_idx, block)

                    if parallel_group and not group_has_fallback_placement and faculty_name == d.get("preferred_faculty"):
                        feasible, assigned_rooms = _parallel_group_feasible(parallel_group, day_idx, start)
                        if feasible:
                            room = assigned_rooms.get(idx)
                            if room:
                                strict_sync_candidates.append((base_score + alignment_score + 200, day_idx, start, room, faculty_name, True))
                            continue

                    room = _pick_room(
                        rooms,
                        "Lab" if d["is_lab"] else "Classroom",
                        d["batch_size"],
                        occupied["room"],
                        day_idx,
                        block,
                    )
                    if not room:
                        continue
                    fallback_penalty = -50 if parallel_group else 0
                    fallback_candidates.append((base_score + alignment_score + fallback_penalty, day_idx, start, room, faculty_name, False))
        cands = strict_sync_candidates if strict_sync_candidates else fallback_candidates
        cands.sort(key=lambda x: (-x[0], x[1], x[2], x[3], x[4]))
        return [(day_idx, start, room, faculty_name, sync_lock) for _score, day_idx, start, room, faculty_name, sync_lock in cands]

    def apply_place(idx, cand):
        d = demands[idx]
        parallel_lab = _is_parallel_batch_lab(d.get("batch_name"), d.get("is_lab"))
        day_idx, start, room, faculty_name, sync_lock = cand
        block = list(range(start, start + d["duration"]))
        day_name = DAYS[day_idx]
        occupied["subject_day"].setdefault((d["class_name"], d["batch_name"], day_name), set()).add(d["subject"])
        for s in block:
            if parallel_lab:
                occupied["class_lab"].setdefault(d["class_name"], set()).add((day_idx, s))
                occupied["batch"].setdefault((d["class_name"], d["batch_name"]), set()).add((day_idx, s))
            else:
                occupied["class_full"].setdefault(d["class_name"], set()).add((day_idx, s))
                if d["batch_name"]:
                    occupied["batch"].setdefault((d["class_name"], d["batch_name"]), set()).add((day_idx, s))
            occupied["faculty"].setdefault(faculty_name, set()).add((day_idx, s))
            occupied["room"].setdefault(room, set()).add((day_idx, s))
            faculty_day_load[faculty_name][day_idx] += 1
            faculty_week_load[faculty_name] += 1
        placements[idx] = {"day_idx": day_idx, "start": start, "room": room, "faculty": faculty_name, "sync_lock": bool(sync_lock)}

    def undo_place(idx):
        p = placements[idx]
        if not p:
            return
        d = demands[idx]
        faculty_name = p.get("faculty") or d.get("faculty")
        parallel_lab = _is_parallel_batch_lab(d.get("batch_name"), d.get("is_lab"))
        block = list(range(p["start"], p["start"] + d["duration"]))
        day_name = DAYS[p["day_idx"]]
        subject_key = (d["class_name"], d["batch_name"], day_name)
        for s in block:
            if parallel_lab:
                occupied["class_lab"][d["class_name"]].discard((p["day_idx"], s))
                occupied["batch"][(d["class_name"], d["batch_name"])].discard((p["day_idx"], s))
            else:
                occupied["class_full"][d["class_name"]].discard((p["day_idx"], s))
                if d["batch_name"]:
                    occupied["batch"][(d["class_name"], d["batch_name"])] .discard((p["day_idx"], s))
            occupied["faculty"][faculty_name].discard((p["day_idx"], s))
            occupied["room"][p["room"]].discard((p["day_idx"], s))
            faculty_day_load[faculty_name][p["day_idx"]] -= 1
            faculty_week_load[faculty_name] -= 1
        still_present = any(
            placements[j] and j != idx and demands[j]["class_name"] == d["class_name"] and demands[j]["batch_name"] == d["batch_name"]
            and demands[j]["subject"] == d["subject"] and placements[j]["day_idx"] == p["day_idx"]
            for j in range(len(placements))
        )
        if not still_present:
            occupied["subject_day"].setdefault(subject_key, set()).discard(d["subject"])
        placements[idx] = None

    def choose_index(unassigned):
        best = None
        best_count = 10**9
        for i in unassigned:
            c = len(candidate_slots(demands[i], i))
            if c < best_count:
                best = i
                best_count = c
            if best_count == 0:
                break
        return best, best_count

    def dfs(unassigned):
        nodes["count"] += 1
        if nodes["count"] > max_nodes:
            return False
        if not unassigned:
            return True
        idx, cnt = choose_index(unassigned)
        next_unassigned = [u for u in unassigned if u != idx]
        if cnt == 0:
            return dfs(next_unassigned)
        cands = candidate_slots(demands[idx], idx)
        for cand in cands:
            apply_place(idx, cand)
            if dfs(next_unassigned):
                return True
            undo_place(idx)
        return dfs(next_unassigned)

    unassigned = list(range(len(demands)))
    solved = dfs(unassigned)
    placed, missing = [], []
    if solved:
        for i, p in enumerate(placements):
            if not p:
                missing.append(demands[i])
            else:
                placed.append((demands[i], p))
        return placed, missing

    for i, p in enumerate(placements):
        if p:
            placed.append((demands[i], p))
        else:
            missing.append(demands[i])
    return placed, missing
def _diagnose_unplaced_demand(demand, faculty_meta, rooms, existing_placements=None, scheduler_rules=None):
    scheduler_rules = scheduler_rules or []
    existing_placements = existing_placements or []
    occupied = {"class_full": {}, "class_lab": {}, "batch": {}, "faculty": {}, "room": {}, "subject_day": {}}
    faculty_day_load = {f: {i: 0 for i in range(len(DAYS))} for f in faculty_meta}
    faculty_week_load = {f: 0 for f in faculty_meta}
    _seed_from_locked(occupied, faculty_day_load, faculty_week_load)

    for placed_demand, placement in existing_placements:
        chosen_faculty = placement.get("faculty") or placed_demand.get("faculty")
        block = list(range(placement["start"], placement["start"] + placed_demand["duration"]))
        day_idx = placement["day_idx"]
        day_name = DAYS[day_idx]
        parallel_lab = _is_parallel_batch_lab(placed_demand.get("batch_name"), placed_demand.get("is_lab"))
        occupied["subject_day"].setdefault((placed_demand["class_name"], placed_demand["batch_name"], day_name), set()).add(placed_demand["subject"])
        for slot in block:
            if parallel_lab:
                occupied["class_lab"].setdefault(placed_demand["class_name"], set()).add((day_idx, slot))
                occupied["batch"].setdefault((placed_demand["class_name"], placed_demand["batch_name"]), set()).add((day_idx, slot))
            else:
                occupied["class_full"].setdefault(placed_demand["class_name"], set()).add((day_idx, slot))
                if placed_demand.get("batch_name"):
                    occupied["batch"].setdefault((placed_demand["class_name"], placed_demand["batch_name"]), set()).add((day_idx, slot))
            occupied["faculty"].setdefault(chosen_faculty, set()).add((day_idx, slot))
            occupied["room"].setdefault(placement["room"], set()).add((day_idx, slot))
            faculty_day_load.setdefault(chosen_faculty, {i: 0 for i in range(len(DAYS))})[day_idx] += 1
            faculty_week_load[chosen_faculty] = faculty_week_load.get(chosen_faculty, 0) + 1

    reasons = {}
    faculty_options = [f for f in (demand.get("faculty_options") or [demand.get("faculty")]) if f in faculty_meta]
    if not faculty_options:
        return "No eligible faculty mapped for this subject"

    any_candidate = False
    for faculty_name in faculty_options:
        meta = faculty_meta[faculty_name]
        if faculty_week_load.get(faculty_name, 0) >= meta.get("max_week", 20):
            reasons[f"Faculty {faculty_name} reached weekly limit"] = reasons.get(f"Faculty {faculty_name} reached weekly limit", 0) + 1
            continue
        faculty_found_slot = False
        for day_idx, day_name in enumerate(DAYS):
            if faculty_day_load.get(faculty_name, {}).get(day_idx, 0) >= meta.get("max_day", 4):
                reasons[f"Faculty {faculty_name} reached daily limit on {day_name}"] = reasons.get(f"Faculty {faculty_name} reached daily limit on {day_name}", 0) + 1
                continue
            for start in range(0, len(SLOTS) - demand["duration"] + 1):
                block = list(range(start, start + demand["duration"]))
                if not _block_is_clock_continuous(block):
                    reasons["Required slots are not continuous"] = reasons.get("Required slots are not continuous", 0) + 1
                    continue
                if any(_is_break_slot(slot_index) for slot_index in block):
                    reasons["Lunch break blocks the required slot"] = reasons.get("Lunch break blocks the required slot", 0) + 1
                    continue
                if any((day_idx, s) in occupied["class_full"].get(demand["class_name"], set()) for s in block):
                    reasons[f"Class {demand['class_name']} is already occupied"] = reasons.get(f"Class {demand['class_name']} is already occupied", 0) + 1
                    continue
                if not demand.get("is_lab") and any((day_idx, s) in occupied["class_lab"].get(demand["class_name"], set()) for s in block):
                    reasons[f"Class {demand['class_name']} already has a lab in that slot"] = reasons.get(f"Class {demand['class_name']} already has a lab in that slot", 0) + 1
                    continue
                if demand.get("batch_name") and any((day_idx, s) in occupied["batch"].get((demand["class_name"], demand["batch_name"]), set()) for s in block):
                    reasons[f"Batch {demand['batch_name']} is already occupied"] = reasons.get(f"Batch {demand['batch_name']} is already occupied", 0) + 1
                    continue
                if any((day_idx, s) in occupied["faculty"].get(faculty_name, set()) for s in block):
                    reasons[f"Faculty {faculty_name} is already occupied"] = reasons.get(f"Faculty {faculty_name} is already occupied", 0) + 1
                    continue
                class_day_subjects = occupied.get("subject_day", {}).get((demand["class_name"], demand["batch_name"], day_name), set())
                if demand["subject"] in class_day_subjects:
                    reasons[f"{demand['subject']} is already scheduled for {demand['class_name']} on {day_name}"] = reasons.get(f"{demand['subject']} is already scheduled for {demand['class_name']} on {day_name}", 0) + 1
                    continue
                if any((day_name, s) in meta.get("unavailable", set()) for s in block):
                    reasons[f"Faculty {faculty_name} is unavailable on {day_name}"] = reasons.get(f"Faculty {faculty_name} is unavailable on {day_name}", 0) + 1
                    continue
                if _candidate_blocked_by_scheduler_rules(demand, day_name, block, scheduler_rules):
                    reasons["Scheduler rules block all remaining slots"] = reasons.get("Scheduler rules block all remaining slots", 0) + 1
                    continue
                room = _pick_room(rooms, "Lab" if demand.get("is_lab") else "Classroom", demand.get("batch_size"), occupied["room"], day_idx, block)
                if not room:
                    room_type = "lab" if demand.get("is_lab") else "classroom"
                    reasons[f"No free {room_type} with enough capacity"] = reasons.get(f"No free {room_type} with enough capacity", 0) + 1
                    continue
                faculty_found_slot = True
                any_candidate = True
                break
            if faculty_found_slot:
                break
        if faculty_found_slot:
            break

    if any_candidate:
        return "Only preference rules prevented placement"
    if not reasons:
        return "No feasible slot found"
    return max(reasons.items(), key=lambda item: item[1])[0]


def _collect_validation_issues():
    issues = []
    faculty_catalog = _get_faculty_catalog()
    class_catalog = _get_class_catalog()
    room_catalog = _get_room_catalog()

    faculty_conflicts = con.execute(
        """
        SELECT faculty, day, slot_index, COUNT(*)
        FROM timetable
        WHERE COALESCE(faculty,'') <> ''
        GROUP BY faculty, day, slot_index
        HAVING COUNT(*) > 1
        """
    ).fetchall()
    for faculty, day, slot_index, count_rows in faculty_conflicts:
        issues.append(f"Faculty conflict: {faculty} at {day} slot {slot_index + 1} ({count_rows} entries)")

    room_conflicts = con.execute(
        """
        SELECT room, day, slot_index, COUNT(*)
        FROM timetable
        WHERE COALESCE(room,'') <> ''
        GROUP BY room, day, slot_index
        HAVING COUNT(*) > 1
        """
    ).fetchall()
    for room, day, slot_index, count_rows in room_conflicts:
        issues.append(f"Room conflict: {room} at {day} slot {slot_index + 1} ({count_rows} entries)")

    class_conflicts = con.execute(
        """
        SELECT
            class_name,
            day,
            slot_index,
            COUNT(*) AS total_rows,
            SUM(CASE WHEN is_lab=TRUE AND COALESCE(batch_name,'') <> '' AND UPPER(COALESCE(batch_name,'')) <> 'ALL' THEN 1 ELSE 0 END) AS parallel_lab_rows,
            COUNT(DISTINCT CASE WHEN is_lab=TRUE AND COALESCE(batch_name,'') <> '' AND UPPER(COALESCE(batch_name,'')) <> 'ALL' THEN batch_name END) AS distinct_parallel_batches,
            SUM(CASE WHEN is_lab=FALSE OR COALESCE(batch_name,'') = '' OR UPPER(COALESCE(batch_name,'')) = 'ALL' THEN 1 ELSE 0 END) AS whole_class_rows
        FROM timetable
        GROUP BY class_name, day, slot_index
        HAVING COUNT(*) > 1
        """
    ).fetchall()
    for class_name, day, slot_index, total_rows, parallel_lab_rows, distinct_parallel_batches, whole_class_rows in class_conflicts:
        if whole_class_rows == 0 and parallel_lab_rows == total_rows and distinct_parallel_batches == total_rows:
            continue
        issues.append(f"Class conflict: {class_name} at {day} slot {slot_index + 1} ({total_rows} entries)")

    break_slot_conflicts = con.execute(
        """
        SELECT class_name, day, slot_index, subject
        FROM timetable
        WHERE slot_index IN ({})
          AND LOWER(TRIM(COALESCE(subject,''))) NOT IN ('break', 'lunch break')
        ORDER BY class_name, day, slot_index
        """.format(",".join(str(i) for i in sorted(BREAK_SLOT_SET)) if BREAK_SLOT_SET else "-1"),
    ).fetchall()
    for class_name, day, slot_index, subject in break_slot_conflicts:
        issues.append(f"Break conflict: {class_name} has '{subject}' on {day} slot {slot_index + 1}, reserved for {BREAK_LABEL}")

    batch_conflicts = con.execute(
        """
        SELECT class_name, batch_name, day, slot_index, COUNT(*)
        FROM timetable
        WHERE batch_name <> ''
        GROUP BY class_name, batch_name, day, slot_index
        HAVING COUNT(*) > 1
        """
    ).fetchall()
    for class_name, batch_name, day, slot_index, count_rows in batch_conflicts:
        issues.append(f"Batch conflict: {class_name}-{batch_name} at {day} slot {slot_index + 1} ({count_rows} entries)")

    repeated_subject_rows = con.execute(
        """
        SELECT class_name, COALESCE(batch_name,''), subject, day, COUNT(DISTINCT slot_index)
        FROM timetable
        GROUP BY class_name, COALESCE(batch_name,''), subject, day
        HAVING COUNT(DISTINCT slot_index) > CASE WHEN MAX(CASE WHEN is_lab THEN 1 ELSE 0 END)=1 THEN 2 ELSE 1 END
        """
    ).fetchall()
    for class_name, batch_name, subject, day, count_slots in repeated_subject_rows:
        label = f"{class_name} {subject}" + (f" batch {batch_name}" if batch_name else "")
        issues.append(f"Repeated subject on same day: {label} on {day} uses {count_slots} slots")

    lab_slot_groups = con.execute(
        """
        SELECT class_name, COALESCE(batch_name,''), subject, day, slot_index
        FROM timetable
        WHERE is_lab=TRUE
        ORDER BY class_name, COALESCE(batch_name,''), subject, day, slot_index
        """
    ).fetchall()
    grouped_lab_slots = {}
    for class_name, batch_name, subject, day, slot_index in lab_slot_groups:
        grouped_lab_slots.setdefault((class_name, batch_name, subject, day), []).append(int(slot_index))
    for (class_name, batch_name, subject, day), slot_indices in grouped_lab_slots.items():
        label = f"{class_name} {subject}" + (f" batch {batch_name}" if batch_name else "")
        unique_slots = sorted(set(slot_indices))
        if len(unique_slots) != 2:
            issues.append(f"Invalid lab duration: {label} on {day} spans {len(unique_slots)} slot(s), expected 2")
            continue
        if not _block_is_clock_continuous(unique_slots):
            slot_labels = ", ".join(SLOTS[s] for s in unique_slots if 0 <= s < len(SLOTS))
            issues.append(f"Non-continuous lab: {label} on {day} uses {slot_labels}. Labs must be in back-to-back slots with no break.")

    timetable_rows = con.execute(
        """
        SELECT year,class_name,batch_name,faculty,subject,room,day,slot_index,is_lab
        FROM timetable
        ORDER BY year,class_name,day,slot_index
        """
    ).fetchall()
    for year, class_name, batch_name, faculty, subject, room, day, slot_index, is_lab in timetable_rows:
        class_meta = class_catalog.get(class_name)
        room_meta = room_catalog.get(room)
        if faculty:
            faculty_meta = faculty_catalog.get(faculty)
            if not faculty_meta:
                issues.append(f"Unknown faculty in timetable: {faculty} for {class_name} {subject}")
            elif (day, slot_index) in faculty_meta["unavailable"]:
                issues.append(f"Faculty unavailable: {faculty} scheduled for {class_name} on {day} slot {slot_index + 1}")
        if room_meta:
            expected_type = "Lab" if is_lab else "Classroom"
            if room_meta["type"] != expected_type:
                issues.append(f"Room type mismatch: {class_name} uses {room} ({room_meta['type']}) for {'lab' if is_lab else 'theory'}")
            required_capacity = _room_capacity_required(class_name, batch_name, bool(is_lab))
            if required_capacity and int(room_meta["capacity"] or 0) < int(required_capacity):
                label = f"{class_name} batch {batch_name}" if batch_name else class_name
                issues.append(f"Capacity conflict: {label} in room {room} needs {required_capacity}, available {room_meta['capacity']}")
        elif room:
            issues.append(f"Unknown room in timetable: {room} for {class_name} {subject}")
        if not class_meta:
            issues.append(f"Unknown class in timetable: {class_name}")

    _, _, _, _, req_counter = _build_requirements(None)
    rows = con.execute(
        """
        SELECT year,class_name,batch_name,subject,is_lab,COUNT(DISTINCT day || '-' || CAST(slot_index AS VARCHAR))
        FROM timetable
        GROUP BY year,class_name,batch_name,subject,is_lab
        """
    ).fetchall()
    scheduled_counter = {(r[0], r[1], r[2], r[3], r[4]): r[5] for r in rows}

    expected_counter = {}
    for req_key, expected_count in req_counter.items():
        aggregate_key = (req_key[0], req_key[1], req_key[2], req_key[3], req_key[5])
        expected_counter[aggregate_key] = expected_counter.get(aggregate_key, 0) + (expected_count * req_key[6])

    for req_key, expected_slots in expected_counter.items():
        scheduled = scheduled_counter.get(req_key, 0)
        if scheduled < expected_slots:
            year, class_name, batch_name, subject, is_lab = req_key
            session_size = 2 if is_lab else 1
            missing_slots = expected_slots - scheduled
            missing_sessions = (missing_slots + session_size - 1) // session_size
            batch_suffix = f" batch {batch_name}" if batch_name else ""
            issues.append(
                f"Unassigned requirement: Y{year} {class_name} {subject}{batch_suffix} ({missing_sessions}x{session_size} slots missing)"
            )
    return issues


def _normalize_phrase(value):
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()


def _daypart_for_slot(slot_index):
    return "morning" if int(slot_index) <= 2 else "afternoon"


def _is_llm_configured():
    return bool(os.getenv("OPENAI_API_KEY") or os.getenv("GEMINI_API_KEY"))


def _year_from_question(text, fallback=None):
    q = (text or "").lower()
    mapping = {"1st": 1, "first": 1, "2nd": 2, "second": 2, "3rd": 3, "third": 3, "4th": 4, "fourth": 4}
    for token, year in mapping.items():
        if f"{token} year" in q:
            return year
    m = re.search(r"\by\s*(\d)\b", q)
    if m:
        y = int(m.group(1))
        if 1 <= y <= 4:
            return y
    return fallback


def _subject_catalog_names():
    names = {r[0] for r in con.execute("SELECT name FROM subjects WHERE COALESCE(name,'') <> ''").fetchall()}
    names.update({r[0] for r in con.execute("SELECT DISTINCT subject FROM timetable WHERE COALESCE(subject,'') <> ''").fetchall()})
    return sorted(names, key=lambda x: (-len(x), x.lower()))


def _entity_matches_question(question, candidates):
    qn = _normalize_phrase(question)
    matches = []
    for candidate in candidates:
        cn = _normalize_phrase(candidate)
        if cn and cn in qn:
            matches.append(candidate)
    return matches


def _infer_class_targets(question, scope_year=None):
    catalog = _get_class_catalog()
    classes = []
    qn = _normalize_phrase(question)
    for display_name, meta in catalog.items():
        tokens = {_normalize_phrase(display_name), _normalize_phrase(meta.get("name")), _normalize_phrase(meta.get("division")), _normalize_phrase(meta.get("department"))}
        if any(tok and tok in qn for tok in tokens):
            classes.append(display_name)
    inferred_year = _year_from_question(question, scope_year)
    q = (question or "").lower()
    if inferred_year and ("year" in q or f"y{inferred_year}" in q):
        for display_name, meta in catalog.items():
            if int(meta["year"]) == int(inferred_year):
                dept = (meta.get("department") or "").lower()
                if dept and dept in q:
                    classes.append(display_name)
        if not classes:
            classes = [display_name for display_name, meta in catalog.items() if int(meta["year"]) == int(inferred_year)]
    ordered = []
    seen = set()
    for c in classes:
        if c not in seen and (scope_year is None or int(catalog[c]["year"]) == int(scope_year)):
            seen.add(c)
            ordered.append(c)
    return ordered


def _collect_timetable_entries(scope_year=None):
    where = "WHERE year=?" if scope_year is not None else ""
    params = [int(scope_year)] if scope_year is not None else []
    rows = con.execute(
        f"""
        SELECT year,class_name,batch_name,faculty,subject,room,day,slot_index,slot_label,is_lab
        FROM timetable
        {where}
        ORDER BY year,class_name,day,slot_index,batch_name,subject
        """,
        params,
    ).fetchall()
    return [{"year": r[0], "class_name": r[1], "batch_name": r[2], "faculty": r[3], "subject": r[4], "room": r[5], "day": r[6], "slot_index": r[7], "slot_label": r[8], "is_lab": bool(r[9])} for r in rows]


def _with_break_rows(entries):
    if not entries:
        return []
    out = [dict(item) for item in entries]
    occupied = {(e.get("class_name"), e.get("day"), int(e.get("slot_index", -1))) for e in out}
    class_names = sorted({e.get("class_name") for e in out if _clean_text(e.get("class_name"))})
    year_by_class = {}
    for item in out:
        if _clean_text(item.get("class_name")) and item.get("year") is not None:
            year_by_class[item.get("class_name")] = item.get("year")
    for class_name in class_names:
        for day in DAYS:
            for slot_index in BREAK_SLOT_BLOCK:
                key = (class_name, day, int(slot_index))
                if key in occupied:
                    continue
                out.append(
                    {
                        "year": year_by_class.get(class_name),
                        "class_name": class_name,
                        "batch_name": "",
                        "faculty": "",
                        "subject": BREAK_LABEL,
                        "room": "",
                        "day": day,
                        "slot_index": int(slot_index),
                        "slot_label": SLOTS[int(slot_index)],
                        "is_lab": False,
                        "locked": True,
                        "is_break": True,
                    }
                )
    out.sort(key=lambda e: (e.get("class_name", ""), DAYS.index(e.get("day")) if e.get("day") in DAYS else 99, int(e.get("slot_index", 0)), e.get("batch_name") or "", e.get("subject") or ""))
    return out


def _format_entry(entry):
    label = f"{entry['class_name']} · {entry['day']} {entry['slot_label']} · {entry['subject']} · {entry['faculty'] or '—'} · {entry['room'] or '—'}"
    if entry.get("batch_name"):
        label += f" · Batch {entry['batch_name']}"
    if entry.get("is_lab"):
        label += " · Lab"
    return label


def _free_slot_lines(entries, label_key, label_value, day=None):
    occupied = {(e["day"], int(e["slot_index"])) for e in entries if e.get(label_key) == label_value}
    lines = []
    days_to_check = [day] if day else DAYS
    for d in days_to_check:
        free = [f"{i + 1} ({SLOTS[i]})" for i in range(len(SLOTS)) if (d, i) not in occupied]
        if free:
            lines.append(f"- {d}: " + ", ".join(free))
    return lines


def _build_timetable_summary(scope_year=None):
    entries = _collect_timetable_entries(scope_year)
    if not entries:
        return {"scope_year": scope_year, "text": f"No timetable entries found for {'Year ' + str(scope_year) if scope_year else 'the current dataset' }.", "stats": {"total_entries": 0, "classes": 0, "faculty": 0, "issues": 0}}
    total_entries = len(entries)
    classes = sorted({e["class_name"] for e in entries})
    faculty_names = sorted({e["faculty"] for e in entries if e["faculty"]})
    labs = sum(1 for e in entries if e["is_lab"])
    theories = total_entries - labs
    by_day = {d: 0 for d in DAYS}
    for e in entries:
        by_day[e["day"]] = by_day.get(e["day"], 0) + 1
    busiest_day = max(by_day, key=by_day.get)
    class_load = {}
    for e in entries:
        class_load[e["class_name"]] = class_load.get(e["class_name"], 0) + 1
    faculty_load = {}
    for e in entries:
        if e["faculty"]:
            faculty_load[e["faculty"]] = faculty_load.get(e["faculty"], 0) + 1
    top_classes = sorted(class_load.items(), key=lambda x: (-x[1], x[0]))[:5]
    top_faculty = sorted(faculty_load.items(), key=lambda x: (-x[1], x[0]))[:5]
    issues = _filter_issues_for_year(_collect_validation_issues(), scope_year)
    lines = [
        f"Summary for {'Year ' + str(scope_year) if scope_year else 'all years'}:",
        f"- Total scheduled entries: {total_entries}",
        f"- Classes covered: {len(classes)} | Faculty involved: {len(faculty_names)}",
        f"- Theory slots: {theories} | Lab slots: {labs}",
        f"- Busiest day: {busiest_day} ({by_day[busiest_day]} scheduled entries)",
    ]
    if top_classes:
        lines.append("- Heaviest class schedules: " + ", ".join([f"{name} ({count})" for name, count in top_classes]))
    if top_faculty:
        lines.append("- Most loaded faculty: " + ", ".join([f"{name} ({count})" for name, count in top_faculty]))
    lines.append(f"- Current validation issues detected: {len(issues)}")
    return {"scope_year": scope_year, "text": "\n".join(lines), "stats": {"total_entries": total_entries, "classes": len(classes), "faculty": len(faculty_names), "issues": len(issues), "labs": labs, "theories": theories, "busiest_day": busiest_day}}


def _filter_issues_for_year(issues, scope_year=None):
    if scope_year is None:
        return list(issues)
    class_catalog = _get_class_catalog()
    class_names = [name for name, meta in class_catalog.items() if int(meta["year"]) == int(scope_year)]
    filtered = []
    for issue in issues:
        if f"Y{scope_year}" in issue or any(name in issue for name in class_names):
            filtered.append(issue)
    return filtered


def _explain_issue(issue):
    plain = issue
    fix = "Review the affected slot and move one of the entries to the nearest free alternative."
    if issue.startswith("Faculty conflict:"):
        plain = issue.replace("Faculty conflict:", "The same faculty member is assigned to more than one class at the same time:").strip()
        fix = "Move one of the overlapping classes or assign a different faculty member."
    elif issue.startswith("Room conflict:"):
        plain = issue.replace("Room conflict:", "The same room is double-booked:").strip()
        fix = "Move one class to another free room of the correct type and capacity."
    elif issue.startswith("Class conflict:"):
        plain = issue.replace("Class conflict:", "A class has more than one entry in the same slot:").strip()
        fix = "Keep only one subject in that slot or move the extra entry."
    elif issue.startswith("Batch conflict:"):
        plain = issue.replace("Batch conflict:", "The same batch is scheduled twice in one slot:").strip()
        fix = "Move one of the batch-level sessions to a different slot."
    elif issue.startswith("Repeated subject on same day:"):
        plain = issue.replace("Repeated subject on same day:", "The same subject appears too often on one day:").strip()
        fix = "Spread the subject across different days for better balance."
    elif issue.startswith("Invalid lab duration:"):
        plain = issue.replace("Invalid lab duration:", "A lab does not span the required two continuous slots:").strip()
        fix = "Reschedule the lab into two consecutive slots."
    elif issue.startswith("Faculty unavailable:"):
        plain = issue.replace("Faculty unavailable:", "A faculty member is scheduled during an unavailable slot:").strip()
        fix = "Move the class or update that faculty member's availability only if the current data is wrong."
    elif issue.startswith("Capacity conflict:"):
        plain = issue.replace("Capacity conflict:", "The assigned room is too small:").strip()
        fix = "Move the class to a larger room or split the section if possible."
    elif issue.startswith("Unassigned requirement:"):
        plain = issue.replace("Unassigned requirement:", "A required class could not be placed in the timetable:").strip()
        fix = "Relax constraints, add rooms, or increase faculty availability for this subject."
    elif issue.startswith("Room type mismatch:"):
        plain = issue.replace("Room type mismatch:", "The room type does not match the session type:").strip()
        fix = "Use a lab for lab sessions and a classroom for theory sessions."
    elif issue.startswith("Unknown"):
        fix = "Correct the master data so the timetable references existing classes, faculty, subjects, and rooms."
    return {"issue": issue, "plain": plain, "fix": fix}


def _build_conflict_explanation(scope_year=None):
    issues = _filter_issues_for_year(_collect_validation_issues(), scope_year)
    details = [_explain_issue(issue) for issue in issues]
    if not details:
        return {"scope_year": scope_year, "count": 0, "items": [], "text": f"No validation conflicts were found for {'Year ' + str(scope_year) if scope_year else 'the current timetable'}."}
    lines = [f"Conflict explanation for {'Year ' + str(scope_year) if scope_year else 'all years'}:"]
    for idx, item in enumerate(details[:12], start=1):
        lines.append(f"{idx}. {item['plain']}")
        lines.append(f"   Suggested fix: {item['fix']}")
    if len(details) > 12:
        lines.append(f"- {len(details) - 12} more issue(s) not shown in this explanation.")
    return {"scope_year": scope_year, "count": len(details), "items": details, "text": "\n".join(lines)}


def _match_subject_rule(rule_subject, demand_subject):
    left = _normalize_phrase(rule_subject)
    right = _normalize_phrase(demand_subject)
    return bool(left and right and (left in right or right in left))


def _parse_natural_language_rule(text, scope_year=None):
    raw = (text or "").strip()
    if not raw:
        raise ValueError("Rule text is required")
    lower = raw.lower().strip()
    day_pattern = r"(monday|tuesday|wednesday|thursday|friday)"
    part_pattern = r"(morning|afternoon)"
    subject_names = _subject_catalog_names()

    def find_subject(source):
        matches = _entity_matches_question(source, subject_names)
        return matches[0] if matches else None

    m = re.search(rf"\bavoid\s+(labs?|lab|theory(?: classes?)?|classes?)\s+on\s+{day_pattern}\s+{part_pattern}\b", lower)
    if m:
        applies_to_raw, day_raw, part = m.groups()
        applies_to = "lab" if "lab" in applies_to_raw else "theory" if "theory" in applies_to_raw else "any"
        return {"text": raw, "scope_year": scope_year, "rule_type": "avoid_daypart", "config": {"applies_to": applies_to, "day": _normalize_day(day_raw), "daypart": part, "strength": "hard"}, "summary": f"Avoid {applies_to} sessions on {_normalize_day(day_raw)} {part}."}

    m = re.search(rf"\bavoid\s+(labs?|lab|theory(?: classes?)?|classes?)\s+on\s+{day_pattern}\b", lower)
    if m:
        applies_to_raw, day_raw = m.groups()
        applies_to = "lab" if "lab" in applies_to_raw else "theory" if "theory" in applies_to_raw else "any"
        return {"text": raw, "scope_year": scope_year, "rule_type": "avoid_day", "config": {"applies_to": applies_to, "day": _normalize_day(day_raw), "strength": "hard"}, "summary": f"Avoid {applies_to} sessions on {_normalize_day(day_raw)}."}

    if any(word in lower for word in ["keep", "prefer"]):
        m = re.search(rf"\b(keep|prefer)\s+(.+?)\s+in\s+{part_pattern}(?:\s+slots?)?\b", lower)
        if m:
            _verb, subject_like, part = m.groups()
            if "theory" in subject_like:
                return {"text": raw, "scope_year": scope_year, "rule_type": "prefer_daypart", "config": {"applies_to": "theory", "daypart": part, "strength": "soft"}, "summary": f"Prefer theory sessions in the {part}."}
            if "lab" in subject_like:
                return {"text": raw, "scope_year": scope_year, "rule_type": "prefer_daypart", "config": {"applies_to": "lab", "daypart": part, "strength": "soft"}, "summary": f"Prefer lab sessions in the {part}."}
            subject_name = find_subject(subject_like) or subject_like.strip().title()
            return {"text": raw, "scope_year": scope_year, "rule_type": "subject_prefer_daypart", "config": {"subject": subject_name, "daypart": part, "strength": "soft"}, "summary": f"Prefer {subject_name} in the {part}."}

    if "avoid" in lower:
        m = re.search(rf"\bavoid\s+(.+?)\s+on\s+{day_pattern}\b", lower)
        if m:
            subject_like, day_raw = m.groups()
            if all(token not in subject_like for token in ["lab", "theory", "class"]):
                subject_name = find_subject(subject_like) or subject_like.strip().title()
                return {"text": raw, "scope_year": scope_year, "rule_type": "subject_avoid_day", "config": {"subject": subject_name, "day": _normalize_day(day_raw), "strength": "hard"}, "summary": f"Avoid scheduling {subject_name} on {_normalize_day(day_raw)}."}

    raise ValueError("Could not understand that rule. Try examples like: 'Avoid labs on Friday afternoon', 'Keep DBMS in morning slots', or 'Avoid Math on Monday'.")


def _load_scheduler_rules(scope_year=None):
    if scope_year is None:
        rows = con.execute("SELECT id,text,rule_type,scope_year,rule_json,created_at,active FROM scheduler_rules WHERE active=TRUE ORDER BY created_at DESC, id DESC").fetchall()
    else:
        rows = con.execute("SELECT id,text,rule_type,scope_year,rule_json,created_at,active FROM scheduler_rules WHERE active=TRUE AND (scope_year IS NULL OR scope_year=?) ORDER BY created_at DESC, id DESC", [scope_year]).fetchall()
    out = []
    for r in rows:
        try:
            config = json.loads(r[4]) if r[4] else {}
        except Exception:
            config = {}
        out.append({"id": r[0], "text": r[1], "rule_type": r[2], "scope_year": r[3], "config": config, "created_at": r[5], "active": bool(r[6]), "summary": config.get("summary") or r[1]})
    return out


def _rule_matches_demand(rule, demand):
    config = rule.get("config") or {}
    applies_to = config.get("applies_to")
    if applies_to == "lab" and not demand.get("is_lab"):
        return False
    if applies_to == "theory" and demand.get("is_lab"):
        return False
    if config.get("subject") and not _match_subject_rule(config.get("subject"), demand.get("subject")):
        return False
    rule_year = rule.get("scope_year")
    if rule_year is not None and int(rule_year) != int(demand.get("year")):
        return False
    return True


def _candidate_blocked_by_scheduler_rules(demand, day_name, block, scheduler_rules):
    for rule in scheduler_rules:
        if not _rule_matches_demand(rule, demand):
            continue
        config = rule.get("config") or {}
        daypart_hits = {_daypart_for_slot(slot_index) for slot_index in block}
        if rule.get("rule_type") == "avoid_daypart" and config.get("day") == day_name and config.get("daypart") in daypart_hits:
            return True
        if rule.get("rule_type") in {"avoid_day", "subject_avoid_day"} and config.get("day") == day_name:
            return True
    return False


def _candidate_scheduler_rule_score(demand, day_name, block, scheduler_rules):
    score = 0
    for rule in scheduler_rules:
        if not _rule_matches_demand(rule, demand):
            continue
        config = rule.get("config") or {}
        daypart_hits = {_daypart_for_slot(slot_index) for slot_index in block}
        if rule.get("rule_type") in {"prefer_daypart", "subject_prefer_daypart"}:
            if config.get("daypart") in daypart_hits:
                score += 3
            else:
                score -= 1
    return score


def _answer_natural_language_query(question, scope_year=None):
    raw = (question or "").strip()
    if not raw:
        raise ValueError("Question is required")
    effective_year = _year_from_question(raw, scope_year)
    entries = _collect_timetable_entries(effective_year)
    q = raw.lower()
    day = None
    for name in DAYS:
        if name.lower() in q:
            day = name
            break
    class_targets = _infer_class_targets(raw, effective_year)
    faculty_targets = _entity_matches_question(raw, [r[0] for r in con.execute("SELECT name FROM faculty").fetchall()])
    room_targets = _entity_matches_question(raw, [r[0] for r in con.execute("SELECT name FROM rooms").fetchall()])
    subject_targets = _entity_matches_question(raw, _subject_catalog_names())

    if any(word in q for word in ["summary", "summarize", "overview"]):
        summary = _build_timetable_summary(effective_year)
        return {"mode": "summary", "scope_year": effective_year, "answer": summary["text"]}

    if any(word in q for word in ["conflict", "issue", "problem", "validate", "explain"]):
        conflicts = _build_conflict_explanation(effective_year)
        return {"mode": "conflicts", "scope_year": effective_year, "answer": conflicts["text"]}

    if "free" in q and "slot" in q:
        if faculty_targets:
            target = faculty_targets[0]
            lines = _free_slot_lines(entries, "faculty", target, day)
            answer = f"Free slots for {target}" + (f" on {day}" if day else "") + ":\n" + ("\n".join(lines) if lines else "- No free slots found.")
            return {"mode": "free_slots", "scope_year": effective_year, "answer": answer}
        if room_targets:
            target = room_targets[0]
            lines = _free_slot_lines(entries, "room", target, day)
            answer = f"Free slots for room {target}" + (f" on {day}" if day else "") + ":\n" + ("\n".join(lines) if lines else "- No free slots found.")
            return {"mode": "free_slots", "scope_year": effective_year, "answer": answer}
        if class_targets:
            target = class_targets[0]
            lines = _free_slot_lines(entries, "class_name", target, day)
            answer = f"Free slots for {target}" + (f" on {day}" if day else "") + ":\n" + ("\n".join(lines) if lines else "- No free slots found.")
            return {"mode": "free_slots", "scope_year": effective_year, "answer": answer}

    if any(word in q for word in ["who has", "who teaches", "who is teaching", "which class"]):
        filtered = entries
        if subject_targets:
            filtered = [e for e in filtered if any(_match_subject_rule(subject, e["subject"]) for subject in subject_targets)]
        if day:
            filtered = [e for e in filtered if e["day"] == day]
        if class_targets:
            filtered = [e for e in filtered if e["class_name"] in class_targets]
        if filtered:
            lines = [f"Matches for '{raw}':"] + [f"- {_format_entry(e)}" for e in filtered[:12]]
            if len(filtered) > 12:
                lines.append(f"- {len(filtered) - 12} more result(s) not shown.")
            return {"mode": "lookup", "scope_year": effective_year, "answer": "\n".join(lines)}

    if q.startswith("when") or "schedule" in q or "show" in q or "timetable" in q:
        filtered = entries
        if class_targets:
            filtered = [e for e in filtered if e["class_name"] in class_targets]
        if faculty_targets:
            filtered = [e for e in filtered if e["faculty"] in faculty_targets]
        if room_targets:
            filtered = [e for e in filtered if e["room"] in room_targets]
        if subject_targets:
            filtered = [e for e in filtered if any(_match_subject_rule(subject, e["subject"]) for subject in subject_targets)]
        if day:
            filtered = [e for e in filtered if e["day"] == day]
        if filtered:
            label = class_targets[0] if class_targets else faculty_targets[0] if faculty_targets else room_targets[0] if room_targets else subject_targets[0] if subject_targets else raw
            lines = [f"Schedule results for {label}:"] + [f"- {_format_entry(e)}" for e in filtered[:15]]
            if len(filtered) > 15:
                lines.append(f"- {len(filtered) - 15} more result(s) not shown.")
            return {"mode": "schedule", "scope_year": effective_year, "answer": "\n".join(lines)}

    examples = ["- Who has DBMS on Monday?", "- Show free slots for Y2-A on Thursday.", "- Summarize Year 3 timetable.", "- Explain conflicts for this year."]
    return {"mode": "fallback", "scope_year": effective_year, "answer": "I could not map that question to a direct timetable lookup. Try one of these:\n" + "\n".join(examples)}


init_db()

@app.route("/")
def root():
    if session.get("auth"):
        return redirect(url_for("dashboard"))
    return redirect(url_for("login_page"))


@app.route("/login", methods=["GET"])
def login_page():
    if session.get("auth"):
        return redirect(url_for("dashboard"))
    return render_template("login.html")


@app.route("/login", methods=["POST"])
def login():
    data = request.json or {}
    if data.get("u") == "admin" and data.get("p") == "admin":
        session["auth"] = True
        return jsonify({"success": True})
    return jsonify({"success": False})


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login_page"))


@app.route("/dashboard")
@login_required
def dashboard():
    return render_template("dashboard.html")


@app.route("/classes")
@login_required
def classes_page():
    return render_template("classes.html")


@app.route("/faculty")
@login_required
def faculty_page():
    return render_template("faculty.html")


@app.route("/subjects")
@login_required
def subjects_page():
    return render_template("subjects.html")


@app.route("/rooms")
@login_required
def rooms_page():
    return render_template("rooms.html")


@app.route("/timetable")
@login_required
def timetable_page():
    years_from_classes = [r[0] for r in con.execute("SELECT DISTINCT year FROM classes WHERE year IS NOT NULL ORDER BY year").fetchall()]
    years_from_timetable = [r[0] for r in con.execute("SELECT DISTINCT year FROM timetable WHERE year IS NOT NULL ORDER BY year").fetchall()]
    initial_years = sorted({int(y) for y in years_from_classes + years_from_timetable if y is not None}) or [1]
    initial_departments = [
        r[0]
        for r in con.execute(
            "SELECT DISTINCT department FROM classes WHERE department IS NOT NULL AND TRIM(department) <> '' ORDER BY department"
        ).fetchall()
    ]
    return render_template(
        "timetable.html",
        initial_years=initial_years,
        initial_departments=initial_departments,
    )


@app.route("/stats")
@login_required
def stats():
    years_present = [r[0] for r in con.execute("SELECT DISTINCT year FROM classes ORDER BY year").fetchall()]
    return jsonify(
        {
            "Classes": con.execute("SELECT COUNT(*) FROM classes").fetchone()[0],
            "Faculty": con.execute("SELECT COUNT(*) FROM faculty").fetchone()[0],
            "Subjects": con.execute("SELECT COUNT(*) FROM subjects").fetchone()[0],
            "Rooms": con.execute("SELECT COUNT(*) FROM rooms").fetchone()[0],
            "Years configured": years_present,
        }
    )


@app.route("/add_class", methods=["POST"])
@login_required
def add_class():
    data = request.json or {}
    try:
        name = _require_text(data, "name", "Class label")
        year = _require_int(data, "year", "Year", 1, 4)
        division = _require_text(data, "division", "Division")
        strength = _require_int(data, "strength", "Division strength", 1)
        department = _require_text(data, "department", "Department")
    except ValueError as e:
        return jsonify({"status": "error", "message": str(e)}), 400
    new_id = con.execute("SELECT COALESCE(MAX(id),0)+1 FROM classes").fetchone()[0]
    con.execute(
        "INSERT INTO classes (id,name,year,department,division,strength) VALUES (?,?,?,?,?,?)",
        [new_id, name, year, department, division, strength],
    )
    return jsonify({"status": "ok"})


@app.route("/get_classes")
@login_required
def get_classes():
    rows = con.execute(
        "SELECT id, name, year, department, division, strength FROM classes ORDER BY year, division, name"
    ).fetchall()
    out = []
    for r in rows:
        out.append(
            {
                "id": r[0],
                "name": r[1],
                "year": r[2],
                "department": r[3],
                "division": r[4],
                "strength": r[5],
                "display_name": f"Y{r[2]}-{r[4] or r[1]}",
            }
        )
    return jsonify(out)



@app.route("/update_class", methods=["POST"])
@login_required
def update_class():
    data = request.json or {}
    try:
        class_id = _require_int(data, "id", "Class", 1)
        name = _require_text(data, "name", "Class label")
        year = _require_int(data, "year", "Year", 1, 4)
        division = _require_text(data, "division", "Division")
        strength = _require_int(data, "strength", "Division strength", 1)
        department = _require_text(data, "department", "Department")
    except ValueError as e:
        return jsonify({"status": "error", "message": str(e)}), 400
    old = con.execute("SELECT name, year, division FROM classes WHERE id=?", [class_id]).fetchone()
    if not old:
        return jsonify({"status": "error", "message": "Class not found"}), 404
    old_display = _class_display_name_from_values(old[1], old[0], old[2])
    new_display = _class_display_name_from_values(year, name, division)
    con.execute(
        "UPDATE classes SET name=?, year=?, department=?, division=?, strength=? WHERE id=?",
        [name, year, department, division, strength, class_id],
    )
    con.execute("UPDATE timetable SET class=?, class_name=?, year=? WHERE class_name=?", [new_display, new_display, year, old_display])
    return jsonify({"status": "ok"})


@app.route("/delete_class", methods=["POST"])
@login_required
def delete_class():
    data = request.json or {}
    try:
        class_id = _require_int(data, "id", "Class", 1)
    except ValueError as e:
        return jsonify({"status": "error", "message": str(e)}), 400
    old = con.execute("SELECT name, year, division FROM classes WHERE id=?", [class_id]).fetchone()
    if not old:
        return jsonify({"status": "error", "message": "Class not found"}), 404
    display_name = _class_display_name_from_values(old[1], old[0], old[2])
    con.execute("DELETE FROM batches WHERE class_id=?", [class_id])
    con.execute("DELETE FROM timetable WHERE class_name=?", [display_name])
    con.execute("DELETE FROM classes WHERE id=?", [class_id])
    return jsonify({"status": "ok", "message": f"Deleted class {display_name}"})


@app.route("/add_faculty", methods=["POST"])
@login_required
def add_faculty():
    data = request.json or {}
    try:
        name = _require_text(data, "name", "Faculty name")
        department = _require_text(data, "department", "Department")
        subjects = [str(x).strip() for x in data.get("subjects", []) if str(x).strip()]
        max_day = _require_int(data, "max_day", "Max slots/day", 1, len(SLOTS))
        max_week = _require_int(data, "max_week", "Max slots/week", 1, len(SLOTS) * len(DAYS))
        allowed_years = sorted({int(y) for y in data.get("allowed_years", []) if str(y).strip()})
        if not allowed_years:
            raise ValueError("Select at least one allowed year")
    except ValueError as e:
        return jsonify({"status": "error", "message": str(e)}), 400
    new_id = con.execute("SELECT COALESCE(MAX(id),0)+1 FROM faculty").fetchone()[0]
    con.execute(
        """
        INSERT INTO faculty (id,name,department,subjects,max_day,max_week,unavailable,allowed_years)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [new_id, name, department, json.dumps(subjects), max_day, max_week, json.dumps(data.get("unavailable", [])), json.dumps(allowed_years)],
    )
    return jsonify({"status": "success"})


@app.route("/get_faculty")
@login_required
def get_faculty():
    rows = con.execute(
        "SELECT id, name, department, max_day, max_week, allowed_years, subjects, unavailable FROM faculty ORDER BY name"
    ).fetchall()
    return jsonify(
        [
            {
                "id": r[0],
                "name": r[1],
                "department": r[2],
                "max_day": r[3],
                "max_week": r[4],
                "allowed_years": _parse_json_array(r[5], [1, 2, 3, 4]),
                "subjects": _parse_json_array(r[6], []),
                "unavailable": [
                    {"day": day, "slot_index": slot_index, "slot_label": SLOTS[slot_index]}
                    for day, slot_index in sorted(_normalize_unavailable_slots(r[7]), key=lambda x: (DAYS.index(x[0]), x[1]))
                ],
            }
            for r in rows
        ]
    )


@app.route("/update_faculty", methods=["POST"])
@login_required
def update_faculty():
    data = request.json or {}
    try:
        faculty_id = _require_int(data, "id", "Faculty", 1)
        name = _require_text(data, "name", "Faculty name")
        department = _require_text(data, "department", "Department")
        subjects = [str(x).strip() for x in data.get("subjects", []) if str(x).strip()]
        max_day = _require_int(data, "max_day", "Max slots/day", 1, len(SLOTS))
        max_week = _require_int(data, "max_week", "Max slots/week", 1, len(SLOTS) * len(DAYS))
        allowed_years = sorted({int(y) for y in data.get("allowed_years", []) if str(y).strip()})
        if not allowed_years:
            raise ValueError("Select at least one allowed year")
    except ValueError as e:
        return jsonify({"status": "error", "message": str(e)}), 400
    old = con.execute("SELECT name FROM faculty WHERE id=?", [faculty_id]).fetchone()
    if not old:
        return jsonify({"status": "error", "message": "Faculty not found"}), 404
    old_name = old[0]
    con.execute(
        """
        UPDATE faculty
        SET name=?, department=?, subjects=?, max_day=?, max_week=?, unavailable=?, allowed_years=?
        WHERE id=?
        """,
        [name, department, json.dumps(subjects), max_day, max_week, json.dumps(data.get("unavailable", [])), json.dumps(allowed_years), faculty_id],
    )
    if old_name != name:
        con.execute("UPDATE subjects SET faculty=? WHERE faculty=?", [name, old_name])
        con.execute("UPDATE timetable SET faculty=? WHERE faculty=?", [name, old_name])
    return jsonify({"status": "success"})


@app.route("/delete_faculty", methods=["POST"])
@login_required
def delete_faculty():
    data = request.json or {}
    try:
        faculty_id = _require_int(data, "id", "Faculty", 1)
    except ValueError as e:
        return jsonify({"status": "error", "message": str(e)}), 400
    old = con.execute("SELECT name FROM faculty WHERE id=?", [faculty_id]).fetchone()
    if not old:
        return jsonify({"status": "error", "message": "Faculty not found"}), 404
    name = old[0]
    con.execute("DELETE FROM faculty WHERE id=?", [faculty_id])
    con.execute("DELETE FROM subjects WHERE faculty=?", [name])
    con.execute("DELETE FROM timetable WHERE faculty=?", [name])
    return jsonify({"status": "success", "message": f"Deleted faculty {name} and dependent subjects/timetable rows"})


@app.route("/add_subject", methods=["POST"])
@login_required
def add_subject():
    data = request.json or {}
    try:
        name = _require_text(data, "name", "Subject name")
        code = _require_text(data, "code", "Subject code")
        subject_type = _clean_text(data.get("type") or "Theory") or "Theory"
        if subject_type not in {"Theory", "Lab"}:
            raise ValueError("Subject type must be Theory or Lab")
        faculty = _require_text(data, "faculty", "Faculty")
        year = _require_int(data, "year", "Year", 1, 4)
        weekly = _require_int(data, "weekly", "Sessions per week", 1)
        continuous_slots = _require_int(data, "continuous_slots", "Continuous slots", 1, 2)
        _validate_subject_rules(subject_type, weekly, continuous_slots)
    except ValueError as e:
        return jsonify({"status": "error", "message": str(e)}), 400
    new_id = con.execute("SELECT COALESCE(MAX(id),0)+1 FROM subjects").fetchone()[0]
    con.execute(
        """
        INSERT INTO subjects (id,name,code,type,weekly_lectures,lab_hours,duration,priority,faculty,year,weekly_sessions,continuous_slots)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [new_id, name, code, subject_type, weekly, 2 if subject_type == "Lab" else 0, "2hr" if subject_type == "Lab" else "1hr", data.get("priority", "Medium"), faculty, year, weekly, 2 if subject_type == "Lab" else 1],
    )
    return jsonify({"status": "success"})


@app.route("/get_subjects")
@login_required
def get_subjects():
    rows = con.execute(
        "SELECT id, name, code, type, faculty, year, weekly_sessions, continuous_slots FROM subjects ORDER BY year, name"
    ).fetchall()
    return jsonify(
        [
            {
                "id": r[0],
                "name": r[1],
                "code": r[2],
                "type": r[3],
                "faculty": r[4],
                "year": r[5],
                "weekly_sessions": r[6],
                "continuous_slots": r[7],
            }
            for r in rows
        ]
    )


@app.route("/update_subject", methods=["POST"])
@login_required
def update_subject():
    data = request.json or {}
    try:
        subject_id = _require_int(data, "id", "Subject", 1)
        name = _require_text(data, "name", "Subject name")
        code = _require_text(data, "code", "Subject code")
        subject_type = _clean_text(data.get("type") or "Theory") or "Theory"
        if subject_type not in {"Theory", "Lab"}:
            raise ValueError("Subject type must be Theory or Lab")
        faculty = _require_text(data, "faculty", "Faculty")
        year = _require_int(data, "year", "Year", 1, 4)
        weekly = _require_int(data, "weekly", "Sessions per week", 1)
        continuous_slots = _require_int(data, "continuous_slots", "Continuous slots", 1, 2)
        _validate_subject_rules(subject_type, weekly, continuous_slots)
    except ValueError as e:
        return jsonify({"status": "error", "message": str(e)}), 400
    old = con.execute("SELECT name, faculty, year FROM subjects WHERE id=?", [subject_id]).fetchone()
    if not old:
        return jsonify({"status": "error", "message": "Subject not found"}), 404
    con.execute(
        """
        UPDATE subjects
        SET name=?, code=?, type=?, weekly_lectures=?, lab_hours=?, duration=?, priority=?, faculty=?, year=?, weekly_sessions=?, continuous_slots=?
        WHERE id=?
        """,
        [name, code, subject_type, weekly, 2 if subject_type == "Lab" else 0, "2hr" if subject_type == "Lab" else "1hr", data.get("priority", "Medium"), faculty, year, weekly, 2 if subject_type == "Lab" else 1, subject_id],
    )
    con.execute("UPDATE timetable SET subject=?, faculty=?, is_lab=?, year=? WHERE year=? AND subject=? AND faculty=?", [name, faculty, subject_type == "Lab", year, old[2], old[0], old[1]])
    return jsonify({"status": "success"})


@app.route("/delete_subject", methods=["POST"])
@login_required
def delete_subject():
    data = request.json or {}
    try:
        subject_id = _require_int(data, "id", "Subject", 1)
    except ValueError as e:
        return jsonify({"status": "error", "message": str(e)}), 400
    old = con.execute("SELECT name, year FROM subjects WHERE id=?", [subject_id]).fetchone()
    if not old:
        return jsonify({"status": "error", "message": "Subject not found"}), 404
    con.execute("DELETE FROM subjects WHERE id=?", [subject_id])
    con.execute("DELETE FROM timetable WHERE year=? AND subject=?", [old[1], old[0]])
    return jsonify({"status": "success"})


@app.route("/add_room", methods=["POST"])
@login_required
def add_room():
    data = request.json or {}
    try:
        name = _require_text(data, "name", "Room name")
        room_type = _clean_text(data.get("type") or "Classroom") or "Classroom"
        if room_type not in {"Classroom", "Lab"}:
            raise ValueError("Room type must be Classroom or Lab")
        capacity = _require_int(data, "capacity", "Capacity", 1)
        department = "General"  # Default department
    except ValueError as e:
        return jsonify({"status": "error", "message": str(e)}), 400
    new_id = con.execute("SELECT COALESCE(MAX(id),0)+1 FROM rooms").fetchone()[0]
    con.execute("INSERT INTO rooms VALUES (?, ?, ?, ?, ?)", [new_id, name, room_type, capacity, department])
    return jsonify({"status": "success"})


@app.route("/get_rooms")
@login_required
def get_rooms():
    rows = con.execute("SELECT id, name, type, capacity FROM rooms ORDER BY name").fetchall()
    return jsonify(
        [{"id": r[0], "name": r[1], "type": r[2], "capacity": r[3]} for r in rows]
    )


@app.route("/update_room", methods=["POST"])
@login_required
def update_room():
    data = request.json or {}
    try:
        room_id = _require_int(data, "id", "Room", 1)
        name = _require_text(data, "name", "Room name")
        room_type = _clean_text(data.get("type") or "Classroom") or "Classroom"
        if room_type not in {"Classroom", "Lab"}:
            raise ValueError("Room type must be Classroom or Lab")
        capacity = _require_int(data, "capacity", "Capacity", 1)
        department = "General"  # Default department
    except ValueError as e:
        return jsonify({"status": "error", "message": str(e)}), 400
    old = con.execute("SELECT name FROM rooms WHERE id=?", [room_id]).fetchone()
    if not old:
        return jsonify({"status": "error", "message": "Room not found"}), 404
    old_name = old[0]
    con.execute("UPDATE rooms SET name=?, type=?, capacity=?, department=? WHERE id=?", [name, room_type, capacity, department, room_id])
    if old_name != name:
        con.execute("UPDATE timetable SET room=? WHERE room=?", [name, old_name])
    return jsonify({"status": "success"})


@app.route("/delete_room", methods=["POST"])
@login_required
def delete_room():
    data = request.json or {}
    try:
        room_id = _require_int(data, "id", "Room", 1)
    except ValueError as e:
        return jsonify({"status": "error", "message": str(e)}), 400
    old = con.execute("SELECT name FROM rooms WHERE id=?", [room_id]).fetchone()
    if not old:
        return jsonify({"status": "error", "message": "Room not found"}), 404
    con.execute("DELETE FROM rooms WHERE id=?", [room_id])
    con.execute("DELETE FROM timetable WHERE room=?", [old[0]])
    return jsonify({"status": "success"})


@app.route("/add_batch", methods=["POST"])
@login_required
def add_batch():
    data = request.json or {}
    try:
        class_id = _require_int(data, "class_id", "Class", 1)
        batch_name = _require_text(data, "batch_name", "Batch name")
        size = _require_int(data, "size", "Batch size", 1)
    except ValueError as e:
        return jsonify({"status": "error", "message": str(e)}), 400
    new_id = con.execute("SELECT COALESCE(MAX(id),0)+1 FROM batches").fetchone()[0]
    class_name = con.execute("SELECT name FROM classes WHERE id=?", [class_id]).fetchone()
    if not class_name:
        return jsonify({"status": "error", "message": "Selected class does not exist"}), 400
    con.execute(
        "INSERT INTO batches (id,class,batch_name,size,class_id) VALUES (?,?,?,?,?)",
        [new_id, class_name[0], batch_name, size, class_id],
    )
    return jsonify({"status": "ok"})


@app.route("/get_batches")
@login_required
def get_batches():
    rows = con.execute(
        """
        SELECT b.id, b.class_id, c.year, c.division, c.name, c.department, b.batch_name, b.size
        FROM batches b
        LEFT JOIN classes c ON c.id=b.class_id
        ORDER BY c.year, c.division, b.batch_name
        """
    ).fetchall()
    return jsonify(
        [
            {
                "id": r[0],
                "class_id": r[1],
                "year": r[2],
                "division": r[3],
                "class_name": r[4],
                "department": r[5],
                "batch_name": r[6],
                "size": r[7],
            }
            for r in rows
        ]
    )


@app.route("/update_batch", methods=["POST"])
@login_required
def update_batch():
    data = request.json or {}
    try:
        batch_id = _require_int(data, "id", "Batch", 1)
        class_id = _require_int(data, "class_id", "Class", 1)
        batch_name = _require_text(data, "batch_name", "Batch name")
        size = _require_int(data, "size", "Batch size", 1)
    except ValueError as e:
        return jsonify({"status": "error", "message": str(e)}), 400
    class_row = con.execute("SELECT name, year, division FROM classes WHERE id=?", [class_id]).fetchone()
    old = con.execute("SELECT class_id, batch_name FROM batches WHERE id=?", [batch_id]).fetchone()
    if not class_row or not old:
        return jsonify({"status": "error", "message": "Batch or class not found"}), 404
    old_class = con.execute("SELECT name, year, division FROM classes WHERE id=?", [old[0]]).fetchone()
    old_display = _class_display_name_from_values(old_class[1], old_class[0], old_class[2]) if old_class else None
    new_display = _class_display_name_from_values(class_row[1], class_row[0], class_row[2])
    con.execute("UPDATE batches SET class=?, batch_name=?, size=?, class_id=? WHERE id=?", [class_row[0], batch_name, size, class_id, batch_id])
    if old_display:
        con.execute("UPDATE timetable SET class_name=?, class=?, batch_name=? WHERE class_name=? AND batch_name=?", [new_display, new_display, batch_name, old_display, old[1]])
    return jsonify({"status": "ok"})


@app.route("/delete_batch", methods=["POST"])
@login_required
def delete_batch():
    data = request.json or {}
    try:
        batch_id = _require_int(data, "id", "Batch", 1)
    except ValueError as e:
        return jsonify({"status": "error", "message": str(e)}), 400
    old = con.execute("SELECT class_id, batch_name FROM batches WHERE id=?", [batch_id]).fetchone()
    if not old:
        return jsonify({"status": "error", "message": "Batch not found"}), 404
    class_row = con.execute("SELECT name, year, division FROM classes WHERE id=?", [old[0]]).fetchone()
    if class_row:
        class_display = _class_display_name_from_values(class_row[1], class_row[0], class_row[2])
        con.execute("DELETE FROM timetable WHERE class_name=? AND batch_name=?", [class_display, old[1]])
    con.execute("DELETE FROM batches WHERE id=?", [batch_id])
    return jsonify({"status": "ok"})


@app.route("/get_years")
@login_required
def get_years():
    class_years = [r[0] for r in con.execute("SELECT DISTINCT year FROM classes").fetchall()]
    timetable_years = [r[0] for r in con.execute("SELECT DISTINCT year FROM timetable").fetchall()]
    years = sorted({int(y) for y in class_years + timetable_years if y is not None})
    return jsonify(years)


@app.route("/get_departments")
@login_required
def get_departments():
    try:
        departments = [r[0] for r in con.execute("SELECT DISTINCT department FROM classes WHERE department IS NOT NULL AND department != '' ORDER BY department").fetchall()]
        return jsonify(departments)
    except Exception as e:
        return jsonify({"error": str(e), "departments": []}), 500


@app.route("/debug/data")
@login_required
def debug_data():
    """Debug endpoint to see database state"""
    try:
        classes_count = con.execute("SELECT COUNT(*) FROM classes").fetchone()[0]
        classes_total = con.execute("SELECT * FROM classes LIMIT 5").fetchall()
        departments_raw = con.execute("SELECT DISTINCT department FROM classes").fetchall()
        years_raw = con.execute("SELECT DISTINCT year FROM classes").fetchall()
        return jsonify({
            "classes_count": classes_count,
            "sample_classes": [dict(c.__dict__) if hasattr(c, '__dict__') else list(c) for c in classes_total],
            "all_departments": [d[0] for d in departments_raw],
            "all_years": sorted([y[0] for y in years_raw if y[0] is not None])
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/get_timetable_versions")
@login_required
def get_timetable_versions():
    selected_year = request.args.get("year")
    if selected_year and str(selected_year).strip():
        rows = con.execute(
            """
            SELECT id,label,note,scope_year,created_at,entry_count
            FROM timetable_versions
            WHERE scope_year=? OR scope_year IS NULL
            ORDER BY id DESC
            LIMIT 50
            """,
            [int(selected_year)],
        ).fetchall()
    else:
        rows = con.execute(
            "SELECT id,label,note,scope_year,created_at,entry_count FROM timetable_versions ORDER BY id DESC LIMIT 50"
        ).fetchall()
    return jsonify(
        [
            {
                "id": r[0],
                "label": r[1],
                "note": r[2],
                "scope_year": r[3],
                "created_at": r[4],
                "entry_count": r[5],
            }
            for r in rows
        ]
    )


@app.route("/save_timetable_version", methods=["POST"])
@login_required
def save_timetable_version():
    payload = request.json or {}
    year_raw = payload.get("year")
    scope_year = int(year_raw) if year_raw not in {None, "", "all"} else None
    label = _clean_text(payload.get("label")) or (f"Manual snapshot Y{scope_year}" if scope_year else "Manual snapshot")
    note = _clean_text(payload.get("note"))
    version_id = _save_timetable_version(scope_year=scope_year, label=label, note=note)
    if not version_id:
        return jsonify({"status": "error", "message": "Nothing to snapshot for the selected scope"}), 400
    return jsonify({"status": "ok", "version_id": version_id})


@app.route("/rollback_timetable_version", methods=["POST"])
@login_required
def rollback_timetable_version():
    payload = request.json or {}
    try:
        version_id = _require_int(payload, "version_id", "Version", 1)
    except ValueError as e:
        return jsonify({"status": "error", "message": str(e)}), 400
    current_scope = payload.get("current_year")
    current_scope = int(current_scope) if current_scope not in {None, "", "all"} else None
    _save_timetable_version(scope_year=current_scope, label=f"Backup before rollback {version_id}", note="Automatic rollback backup")
    try:
        version = _restore_timetable_version(version_id)
    except ValueError as e:
        return jsonify({"status": "error", "message": str(e)}), 404
    return jsonify({"status": "ok", "version": version})


@app.route("/generate")
@login_required
def generate():
    scope_year_raw = request.args.get("year")
    try:
        scope_year = int(scope_year_raw) if scope_year_raw not in {None, "", "all"} else None
    except (TypeError, ValueError):
        return jsonify({"status": "error", "message": "Invalid year value", "unplaced": [], "placed_count": 0}), 200

    readiness = _generation_readiness(scope_year)
    if readiness["missing"] or readiness["invalid_subjects"]:
        message_bits = []
        if readiness["missing"]:
            message_bits.append("Missing setup: " + ", ".join(readiness["missing"]))
        if readiness["invalid_subjects"]:
            message_bits.append("Invalid subject mappings found")
        return jsonify({
            "status": "incomplete",
            "message": "; ".join(message_bits) or "Generation prerequisites are incomplete",
            "unplaced": [],
            "placed_count": 0,
            "details": {
                "classes": len(readiness["classes"]),
                "rooms": len(readiness["rooms"]),
                "subjects": readiness["subjects_count"],
                "faculty": len(readiness["faculty_meta"]),
                "invalid_subjects": readiness["invalid_subjects"],
            },
        }), 200

    _save_timetable_version(scope_year=scope_year, label=f"Backup before generate {'all' if scope_year is None else 'Y' + str(scope_year)}", note="Automatic backup")
    if scope_year is None:
        con.execute("DELETE FROM timetable WHERE locked=FALSE")
    else:
        con.execute("DELETE FROM timetable WHERE locked=FALSE AND year=?", [scope_year])

    classes, faculty_meta, rooms, demands, _ = (
        readiness["classes"],
        readiness["faculty_meta"],
        readiness["rooms"],
        readiness["demands"],
        readiness["requirement_counter"],
    )

    scheduler_rules = _load_scheduler_rules(scope_year)
    placed, missing = _solve_with_backtracking(demands, faculty_meta, rooms, scheduler_rules=scheduler_rules)
    next_id = con.execute("SELECT COALESCE(MAX(id),0)+1 FROM timetable").fetchone()[0]
    for d, p in placed:
        for slot_index in range(p["start"], p["start"] + d["duration"]):
            con.execute(
                """
                INSERT INTO timetable (
                    id,class,faculty,subject,room,day,time_slot,locked,year,class_name,batch_name,slot_index,slot_label,is_lab,is_manual
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                [
                    next_id,
                    d["class_name"],
                    p.get("faculty", d["faculty"]),
                    d["subject"],
                    p["room"],
                    DAYS[p["day_idx"]],
                    SLOTS[slot_index],
                    False,
                    d["year"],
                    d["class_name"],
                    d["batch_name"],
                    slot_index,
                    SLOTS[slot_index],
                    d["is_lab"],
                    False,
                ],
            )
            next_id += 1

    for i, m in enumerate(missing):
        missing[i]["reason"] = _diagnose_unplaced_demand(m, faculty_meta, rooms, existing_placements=placed, scheduler_rules=scheduler_rules)
    return jsonify({"message": "Generated timetable", "unplaced": missing, "placed_count": len(placed)})


@app.route("/regenerate_unlocked", methods=["POST"])
@login_required
def regenerate_unlocked():
    payload = request.json or {}
    year = payload.get("year")
    try:
        scope_year = int(year) if year not in {None, "", "all"} else None
    except (TypeError, ValueError):
        return jsonify({"status": "error", "message": "Invalid year value", "unplaced": [], "placed_count": 0}), 200

    readiness = _generation_readiness(scope_year)
    if readiness["missing"] or readiness["invalid_subjects"]:
        message_bits = []
        if readiness["missing"]:
            message_bits.append("Missing setup: " + ", ".join(readiness["missing"]))
        if readiness["invalid_subjects"]:
            message_bits.append("Invalid subject mappings found")
        return jsonify({
            "status": "incomplete",
            "message": "; ".join(message_bits) or "Generation prerequisites are incomplete",
            "unplaced": [],
            "placed_count": 0,
            "details": {
                "classes": len(readiness["classes"]),
                "rooms": len(readiness["rooms"]),
                "subjects": readiness["subjects_count"],
                "faculty": len(readiness["faculty_meta"]),
                "invalid_subjects": readiness["invalid_subjects"],
            },
        }), 200

    _save_timetable_version(scope_year=scope_year, label=f"Backup before regenerate {'all' if scope_year is None else 'Y' + str(scope_year)}", note="Automatic backup")
    if scope_year is None:
        con.execute("DELETE FROM timetable WHERE locked=FALSE")
    else:
        con.execute("DELETE FROM timetable WHERE locked=FALSE AND year=?", [scope_year])

    classes, faculty_meta, rooms, demands, _ = (
        readiness["classes"],
        readiness["faculty_meta"],
        readiness["rooms"],
        readiness["demands"],
        readiness["requirement_counter"],
    )

    scheduler_rules = _load_scheduler_rules(scope_year)
    placed, missing = _solve_with_backtracking(demands, faculty_meta, rooms, scheduler_rules=scheduler_rules)
    next_id = con.execute("SELECT COALESCE(MAX(id),0)+1 FROM timetable").fetchone()[0]
    for d, p in placed:
        for slot_index in range(p["start"], p["start"] + d["duration"]):
            con.execute(
                """
                INSERT INTO timetable (
                    id,class,faculty,subject,room,day,time_slot,locked,year,class_name,batch_name,slot_index,slot_label,is_lab,is_manual
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                [
                    next_id,
                    d["class_name"],
                    p.get("faculty", d["faculty"]),
                    d["subject"],
                    p["room"],
                    DAYS[p["day_idx"]],
                    SLOTS[slot_index],
                    False,
                    d["year"],
                    d["class_name"],
                    d["batch_name"],
                    slot_index,
                    SLOTS[slot_index],
                    d["is_lab"],
                    False,
                ],
            )
            next_id += 1

    for i, m in enumerate(missing):
        missing[i]["reason"] = _diagnose_unplaced_demand(m, faculty_meta, rooms, existing_placements=placed, scheduler_rules=scheduler_rules)
    return jsonify({"message": "Generated timetable", "unplaced": missing, "placed_count": len(placed)})


@app.route("/validate")
@login_required
def validate():
    return jsonify(_collect_validation_issues())


@app.route("/get_timetable")
@login_required
def get_timetable():
    selected_year = int(request.args.get("year", 1))
    selected_department = (request.args.get("department") or "").strip()

    allowed_classes = None
    if selected_department:
        class_rows = con.execute(
            "SELECT name, year, department, division FROM classes WHERE year=? AND department=? ORDER BY division, name",
            [selected_year, selected_department],
        ).fetchall()
        allowed_classes = set()
        for name, year, department, division in class_rows:
            allowed_classes.update(_class_name_aliases_from_values(year, name, division, department))

    rows = con.execute(
        """
        SELECT t.class_name,t.batch_name,t.faculty,t.subject,t.room,t.day,t.slot_index,t.slot_label,t.is_lab,t.locked
        FROM timetable t
        WHERE t.year=?
        ORDER BY t.class_name, t.day, t.slot_index
        """,
        [selected_year],
    ).fetchall()
    if allowed_classes is not None:
        filtered_rows = [r for r in rows if (r[0] or '').strip() in allowed_classes]
        if filtered_rows:
            rows = filtered_rows

    entries = [
        {
            "class_name": r[0],
            "batch_name": r[1],
            "faculty": r[2],
            "subject": r[3],
            "room": r[4],
            "day": r[5],
            "slot_index": r[6],
            "time": r[7],
            "slot_label": r[7],
            "is_lab": bool(r[8]),
            "locked": bool(r[9]),
            "is_break": _is_break_slot(int(r[6])) and _normalized_key(r[3]) in {"break", "lunchbreak"},
        }
        for r in rows
    ]
    entries = _with_break_rows(entries)
    for idx, entry in enumerate(entries, start=1):
        if entry.get("is_break"):
            entry["subject"] = BREAK_LABEL
            entry["faculty"] = ""
            entry["room"] = ""
            entry["batch_name"] = ""
            entry["locked"] = True
        entry["id"] = idx
        if not entry.get("time"):
            entry["time"] = SLOTS[int(entry["slot_index"])] if 0 <= int(entry["slot_index"]) < len(SLOTS) else ""
    return jsonify(entries)


@app.route("/export_csv")
@login_required
def export_csv():
    selected_year = request.args.get("year")
    where = ""
    params = []
    file_suffix = "all"
    if selected_year:
        where = "WHERE year=?"
        params = [int(selected_year)]
        file_suffix = f"year-{selected_year}"

    rows = con.execute(
        f"""
        SELECT year,class_name,batch_name,faculty,subject,room,day,slot_label,is_lab
        FROM timetable
        {where}
        ORDER BY year,class_name,day,slot_index
        """,
        params,
    ).fetchall()

    entries = _with_break_rows(
        [
            {
                "year": r[0],
                "class_name": r[1],
                "batch_name": r[2],
                "faculty": r[3],
                "subject": r[4],
                "room": r[5],
                "day": r[6],
                "slot_label": r[7],
                "slot_index": SLOTS.index(r[7]) if r[7] in SLOTS else 0,
                "is_lab": bool(r[8]),
            }
            for r in rows
        ]
    )

    file_path = f"timetable-{file_suffix}.csv"
    with open(file_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["Year", "Class", "Batch", "Faculty", "Subject", "Room", "Day", "Time", "Type"])
        for e in entries:
            is_break = bool(e.get("is_break"))
            writer.writerow([
                e.get("year"),
                e.get("class_name"),
                e.get("batch_name", ""),
                "" if is_break else e.get("faculty", ""),
                BREAK_LABEL if is_break else e.get("subject", ""),
                "" if is_break else e.get("room", ""),
                e.get("day", ""),
                e.get("slot_label") or (SLOTS[int(e.get("slot_index", 0))] if 0 <= int(e.get("slot_index", 0)) < len(SLOTS) else ""),
                "Break" if is_break else ("Lab" if e.get("is_lab") else "Theory"),
            ])
    return send_file(file_path, as_attachment=True)


@app.route("/export_xlsx")
@login_required
def export_xlsx():
    try:
        from openpyxl import Workbook
    except Exception:
        return jsonify({"error": "Install openpyxl to enable XLSX export"}), 500
    rows = con.execute(
        "SELECT year,class_name,batch_name,faculty,subject,room,day,slot_label,is_lab FROM timetable ORDER BY year,class_name,day,slot_index"
    ).fetchall()
    entries = _with_break_rows(
        [
            {
                "year": r[0],
                "class_name": r[1],
                "batch_name": r[2],
                "faculty": r[3],
                "subject": r[4],
                "room": r[5],
                "day": r[6],
                "slot_label": r[7],
                "slot_index": SLOTS.index(r[7]) if r[7] in SLOTS else 0,
                "is_lab": bool(r[8]),
            }
            for r in rows
        ]
    )
    wb = Workbook()
    ws = wb.active
    ws.title = "Timetable"
    ws.append(["Year", "Class", "Batch", "Faculty", "Subject", "Room", "Day", "Time", "Type"])
    for e in entries:
        is_break = bool(e.get("is_break"))
        ws.append([
            e.get("year"),
            e.get("class_name"),
            e.get("batch_name", ""),
            "" if is_break else e.get("faculty", ""),
            BREAK_LABEL if is_break else e.get("subject", ""),
            "" if is_break else e.get("room", ""),
            e.get("day", ""),
            e.get("slot_label") or (SLOTS[int(e.get("slot_index", 0))] if 0 <= int(e.get("slot_index", 0)) < len(SLOTS) else ""),
            "Break" if is_break else ("Lab" if e.get("is_lab") else "Theory"),
        ])
    file_path = "timetable.xlsx"
    wb.save(file_path)
    return send_file(file_path, as_attachment=True)


@app.route("/export_pdf")
@login_required
def export_pdf():
    try:
        from reportlab.lib import colors
        from reportlab.lib.pagesizes import A4, landscape
        from reportlab.lib.styles import getSampleStyleSheet
        from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
    except Exception:
        return jsonify({"error": "Install reportlab to enable PDF export"}), 500
    selected_year = int(request.args.get("year", 1))
    rows = con.execute(
        """
        SELECT class_name,batch_name,faculty,subject,room,day,slot_index,slot_label,is_lab
        FROM timetable
        WHERE year=?
        ORDER BY class_name, day, slot_index
        """,
        [selected_year],
    ).fetchall()
    if not rows:
        return jsonify({"error": f"No timetable found for year {selected_year}"}), 404

    entry_rows = _with_break_rows(
        [
            {
                "year": selected_year,
                "class_name": r[0],
                "batch_name": r[1],
                "faculty": r[2],
                "subject": r[3],
                "room": r[4],
                "day": r[5],
                "slot_index": int(r[6]),
                "slot_label": r[7],
                "is_lab": bool(r[8]),
            }
            for r in rows
        ]
    )

    file_path = f"timetable-year-{selected_year}.pdf"
    doc = SimpleDocTemplate(
        file_path,
        pagesize=landscape(A4),
        leftMargin=24,
        rightMargin=24,
        topMargin=24,
        bottomMargin=24,
    )
    styles = getSampleStyleSheet()
    story = [Paragraph(f"Timetable Report - Year {selected_year}", styles["Title"]), Spacer(1, 12)]

    grouped = {}
    for item in entry_rows:
        class_name = item["class_name"]
        grouped.setdefault(class_name, []).append(
            {
                "batch_name": item.get("batch_name", ""),
                "faculty": item.get("faculty", ""),
                "subject": item.get("subject", ""),
                "room": item.get("room", ""),
                "day": item.get("day", ""),
                "slot_index": int(item.get("slot_index", 0)),
                "slot_label": item.get("slot_label") or (SLOTS[int(item.get("slot_index", 0))] if 0 <= int(item.get("slot_index", 0)) < len(SLOTS) else ""),
                "is_lab": bool(item.get("is_lab")),
                "is_break": bool(item.get("is_break")),
            }
        )

    for class_name in sorted(grouped.keys()):
        story.append(Paragraph(class_name, styles["Heading2"]))
        table_data = [["Time / Day"] + DAYS]
        class_entries = grouped[class_name]
        for slot_idx, slot_label in enumerate(SLOTS):
            row = [slot_label]
            for day in DAYS:
                entries = [e for e in class_entries if e["day"] == day and e["slot_index"] == slot_idx]
                if not entries:
                    row.append("Free")
                else:
                    lines = []
                    for e in entries:
                        if e.get("is_break"):
                            lines.append(BREAK_LABEL)
                            continue
                        label = f"{e['subject']}\n{e['faculty']}\n{e['room']}"
                        if e["batch_name"]:
                            label += f" | Batch {e['batch_name']}"
                        if e["is_lab"]:
                            label += " | Lab"
                        lines.append(label)
                    row.append("\n\n".join(lines))
            table_data.append(row)

        table = Table(table_data, repeatRows=1)
        table.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, 0), colors.whitesmoke),
                    ("TEXTCOLOR", (0, 0), (-1, 0), colors.black),
                    ("GRID", (0, 0), (-1, -1), 0.5, colors.lightgrey),
                    ("ALIGN", (0, 0), (-1, 0), "LEFT"),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                    ("FONTNAME", (0, 1), (0, -1), "Helvetica-Bold"),
                    ("FONTSIZE", (0, 0), (-1, -1), 9),
                    ("LEFTPADDING", (0, 0), (-1, -1), 6),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                    ("TOPPADDING", (0, 0), (-1, -1), 6),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
                ]
            )
        )
        story.append(table)
        story.append(Spacer(1, 16))

    doc.build(story)
    return send_file(file_path, as_attachment=True)


@app.route("/reports")
@login_required
def reports():
    total = con.execute("SELECT COUNT(*) FROM timetable").fetchone()[0]
    locked = con.execute("SELECT COUNT(*) FROM timetable WHERE locked=TRUE").fetchone()[0]
    manual = con.execute("SELECT COUNT(*) FROM timetable WHERE is_manual=TRUE").fetchone()[0]
    by_year = con.execute("SELECT year, COUNT(*) FROM timetable GROUP BY year ORDER BY year").fetchall()
    validation = _collect_validation_issues()
    return jsonify(
        {
            "total_entries": total,
            "locked_entries": locked,
            "manual_entries": manual,
            "by_year": [{"year": r[0], "entries": r[1]} for r in by_year],
            "validation_issues": validation,
        }
    )


@app.route("/ai_suggestions")
@login_required
def ai_suggestions():
    selected_year = request.args.get("year")
    selected_year = int(selected_year) if selected_year else None

    where = "WHERE year=?" if selected_year else ""
    params = [selected_year] if selected_year else []

    summary_rows = con.execute(
        f"""
        SELECT year, class_name, COUNT(*) AS entries
        FROM timetable
        {where}
        GROUP BY year, class_name
        ORDER BY year, class_name
        """,
        params,
    ).fetchall()
    validation = _collect_validation_issues()
    issues_for_scope = [i for i in validation if not selected_year or f"Y{selected_year}" in i]

    summary_text = "\n".join([f"- Y{r[0]} {r[1]}: {r[2]} scheduled slots" for r in summary_rows]) or "- No timetable entries yet."
    issues_text = "\n".join([f"- {i}" for i in issues_for_scope]) or "- No validation issues found."

    prompt = f"""
You are an academic timetable optimization assistant.
Return concise, practical recommendations to improve the schedule quality.

Scope year: {selected_year if selected_year else "All years"}

Current schedule summary:
{summary_text}

Detected issues:
{issues_text}

Give:
1) Top 5 recommendations in priority order.
2) For each recommendation, mention expected impact.
3) If no issues are detected, provide optimization ideas (faculty load balancing, room utilization, and lab continuity).
Use plain text bullets only.
""".strip()

    advice = generate_response(prompt)
    return jsonify(
        {
            "scope_year": selected_year,
            "issues_count": len(issues_for_scope),
            "advice": advice,
        }
    )


@app.route("/ai_assistant", methods=["POST"])
@login_required
def ai_assistant():
    payload = request.json or {}
    selected_year = payload.get("year")
    selected_year = int(selected_year) if selected_year else None
    user_question = (payload.get("question") or "").strip()
    if not user_question:
        return jsonify({"error": "question is required"}), 400

    direct = _answer_natural_language_query(user_question, selected_year)
    if direct.get("mode") != "fallback":
        return jsonify({"scope_year": direct.get("scope_year"), "question": user_question, "answer": direct.get("answer"), "mode": direct.get("mode")})

    summary = _build_timetable_summary(selected_year)
    conflicts = _build_conflict_explanation(selected_year)
    rules = _load_scheduler_rules(selected_year)
    rules_text = "\n".join([f"- {rule['text']}" for rule in rules[:10]]) or "- No natural-language scheduler rules saved."

    if _is_llm_configured():
        prompt = f"""
You are an AI assistant for a college timetable system.
Answer the user's question using the live timetable summary, the conflict explanations, and the saved natural-language scheduling rules.
Be concrete and practical. If suggesting a change, mention why it would help.

Scope year: {selected_year if selected_year else 'All years'}

Timetable summary:
{summary['text']}

Conflict explanation:
{conflicts['text']}

Saved scheduler rules:
{rules_text}

User question:
{user_question}

Response format:
- Use short bullet points.
- End with a brief 'Recommended next action'.
""".strip()
        answer = generate_response(prompt)
        return jsonify({"scope_year": selected_year, "question": user_question, "answer": answer, "mode": "llm"})

    fallback_answer = "\n\n".join([
        "LLM is not configured, so here is a grounded timetable-based answer.",
        summary["text"],
        conflicts["text"],
        "Saved rules:\n" + rules_text,
    ])
    return jsonify({"scope_year": selected_year, "question": user_question, "answer": fallback_answer, "mode": "fallback"})


@app.route("/nlp_query", methods=["POST"])
@login_required
def nlp_query():
    payload = request.json or {}
    selected_year = payload.get("year")
    selected_year = int(selected_year) if selected_year else None
    question = (payload.get("question") or "").strip()
    if not question:
        return jsonify({"error": "question is required"}), 400
    try:
        result = _answer_natural_language_query(question, selected_year)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify(result)


@app.route("/nlp_summary")
@login_required
def nlp_summary():
    selected_year = request.args.get("year")
    selected_year = int(selected_year) if selected_year else None
    return jsonify(_build_timetable_summary(selected_year))


@app.route("/nlp_conflicts")
@login_required
def nlp_conflicts():
    selected_year = request.args.get("year")
    selected_year = int(selected_year) if selected_year else None
    return jsonify(_build_conflict_explanation(selected_year))


@app.route("/get_scheduler_rules")
@login_required
def get_scheduler_rules():
    selected_year = request.args.get("year")
    selected_year = int(selected_year) if selected_year else None
    return jsonify(_load_scheduler_rules(selected_year))


@app.route("/add_scheduler_rule", methods=["POST"])
@login_required
def add_scheduler_rule():
    payload = request.json or {}
    selected_year = payload.get("year")
    selected_year = int(selected_year) if selected_year else None
    rule_text = (payload.get("text") or "").strip()
    try:
        parsed = _parse_natural_language_rule(rule_text, selected_year)
    except ValueError as e:
        return jsonify({"status": "error", "message": str(e)}), 400
    new_id = con.execute("SELECT COALESCE(MAX(id),0)+1 FROM scheduler_rules").fetchone()[0]
    config = dict(parsed.get("config") or {})
    config["summary"] = parsed.get("summary")
    created_at = datetime.now().isoformat(timespec="seconds")
    con.execute(
        "INSERT INTO scheduler_rules (id,text,rule_type,scope_year,rule_json,created_at,active) VALUES (?,?,?,?,?,?,?)",
        [new_id, parsed["text"], parsed["rule_type"], selected_year, json.dumps(config), created_at, True],
    )
    return jsonify({"status": "ok", "message": "Rule saved", "rule": {"id": new_id, "text": parsed["text"], "scope_year": selected_year, "summary": parsed.get("summary"), "rule_type": parsed["rule_type"], "created_at": created_at}})


@app.route("/delete_scheduler_rule", methods=["POST"])
@login_required
def delete_scheduler_rule():
    payload = request.json or {}
    try:
        rule_id = _require_int(payload, "id", "Rule", 1)
    except ValueError as e:
        return jsonify({"status": "error", "message": str(e)}), 400
    con.execute("DELETE FROM scheduler_rules WHERE id=?", [rule_id])
    return jsonify({"status": "ok", "message": "Rule deleted"})


@app.route("/preview_timetable_import", methods=["POST"])
@login_required
def preview_timetable_import():
    if "file" not in request.files:
        return jsonify({"error": "file is required"}), 400
    f = request.files["file"]
    if not f or not f.filename:
        return jsonify({"error": "empty file"}), 400

    year_raw = request.form.get("year")
    try:
        year = int(year_raw) if year_raw is not None and str(year_raw).strip() != "" else None
    except (TypeError, ValueError):
        year = None
    if year is None:
        return jsonify({"error": "year is required"}), 400

    default_class_name = (request.form.get("default_class_name") or "").strip()
    raw = f.read()
    if not raw:
        return jsonify({"error": "empty file"}), 400

    parsed, err = extract_timetable_from_upload(raw, f.filename, f.mimetype)
    if err:
        return jsonify({"error": err}), 400

    entries = parsed.get("entries")
    if not isinstance(entries, list) or not entries:
        return jsonify({"error": "Model returned no usable entries", "hint": str(parsed)[:500]}), 400

    preview_rows, warnings, skipped_parse = _preview_import_entries(year, entries, default_class_name)
    extracted_summary = {
        "classes": sorted({_clean_text(r.get("class_name")) for r in preview_rows if _clean_text(r.get("class_name"))}),
        "batches": sorted({_clean_text(r.get("batch_name")) for r in preview_rows if _clean_text(r.get("batch_name"))}),
        "faculties": sorted({_clean_text(r.get("faculty")) for r in preview_rows if _clean_text(r.get("faculty"))}),
        "subjects": sorted({_clean_text(r.get("subject")) for r in preview_rows if _clean_text(r.get("subject"))}),
        "rooms": sorted({_clean_text(r.get("room")) for r in preview_rows if _clean_text(r.get("room"))}),
        "labs_detected": sum(1 for r in preview_rows if _parse_bool(r.get("is_lab"))),
    }
    return jsonify(
        {
            "preview_entries": preview_rows,
            "warnings": warnings,
            "skipped_parse": skipped_parse,
            "extracted_count": len(entries),
            "extracted_summary": extracted_summary,
        }
    )


@app.route("/apply_timetable_import", methods=["POST"])
@login_required
def apply_timetable_import():
    payload = request.json or {}
    try:
        year = _require_int(payload, "year", "Year", 1, 4)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    entries = payload.get("entries") or []
    if not isinstance(entries, list) or not entries:
        return jsonify({"error": "No preview entries supplied"}), 400

    try:
        con.execute("BEGIN TRANSACTION")
        resolved_rows, summary, warnings = _resolve_import_rows_with_resources(year, entries)

        class_catalog = _get_class_catalog()
        room_catalog = _get_room_catalog()
        faculty_catalog = _get_faculty_catalog()
        valid_rows = []
        for idx, row in enumerate(resolved_rows, start=1):
            issues = _validate_single_timetable_entry(year, row, class_catalog, room_catalog, faculty_catalog)
            if issues:
                summary["timetable_entries"]["skipped_invalid"] += 1
                warnings.append(f"Skipped row {idx}: {'; '.join(issues)}")
                continue
            valid_rows.append(row)

        if not valid_rows:
            con.execute("ROLLBACK")
            return jsonify({"error": "No valid rows to import", "warnings": warnings, "summary": summary}), 400

        by_key = {}
        for row in valid_rows:
            if _is_parallel_batch_lab(row.get("batch_name"), row.get("is_lab")):
                key = (row["class_name"], row["day"], row["slot_index"], row.get("batch_name", ""))
            else:
                key = (row["class_name"], row["day"], row["slot_index"])
            by_key[key] = row
        rows = list(by_key.values())

        final_rows = []
        for row in rows:
            if _is_parallel_batch_lab(row.get("batch_name"), row.get("is_lab")):
                locked = con.execute(
                    """
                    SELECT COUNT(*)
                    FROM timetable
                    WHERE year=? AND class_name=? AND day=? AND slot_index=? AND locked=TRUE
                      AND (COALESCE(batch_name,'')=? OR COALESCE(batch_name,'')='' OR UPPER(COALESCE(batch_name,''))='ALL' OR is_lab=FALSE)
                    """,
                    [year, row["class_name"], row["day"], row["slot_index"], row["batch_name"]],
                ).fetchone()[0]
            else:
                locked = con.execute(
                    "SELECT COUNT(*) FROM timetable WHERE year=? AND class_name=? AND day=? AND slot_index=? AND locked=TRUE",
                    [year, row["class_name"], row["day"], row["slot_index"]],
                ).fetchone()[0]
            if locked:
                summary["timetable_entries"]["skipped_locked"] += 1
                continue
            final_rows.append(row)

        if summary["timetable_entries"]["skipped_locked"]:
            warnings.append(f"Skipped {summary['timetable_entries']['skipped_locked']} row(s) because the target slot is locked.")
        if not final_rows:
            con.execute("ROLLBACK")
            return jsonify({"error": "All edited rows were skipped due to validation/locks", "warnings": warnings, "summary": summary}), 400

        _save_timetable_version(scope_year=year, label=f"Backup before AI import Y{year}", note="Automatic backup")
        con.execute("DELETE FROM timetable WHERE locked=FALSE AND year=?", [year])
        inserted = _insert_timetable_rows(year, final_rows, is_manual=True)
        summary["timetable_entries"]["inserted"] = int(inserted)
        summary["timetable_entries"]["unique_slots"] = len(final_rows)
        con.execute("COMMIT")
    except Exception as exc:
        try:
            con.execute("ROLLBACK")
        except Exception:
            pass
        return jsonify({"error": f"Import apply failed: {exc}"}), 500

    return jsonify(
        {
            "status": "ok",
            "inserted": inserted,
            "unique_slots": len(final_rows),
            "warnings": warnings,
            "summary": summary,
        }
    )


@app.route("/import_timetable_file", methods=["POST"])
@login_required
def import_timetable_file():
    if "file" not in request.files:
        return jsonify({"error": "file is required"}), 400
    f = request.files["file"]
    if not f or not f.filename:
        return jsonify({"error": "empty file"}), 400

    year_raw = request.form.get("year")
    try:
        year = int(year_raw) if year_raw is not None and str(year_raw).strip() != "" else None
    except (TypeError, ValueError):
        year = None
    if year is None:
        return jsonify({"error": "year is required"}), 400

    default_class_name = (request.form.get("default_class_name") or "").strip()
    raw = f.read()
    if not raw:
        return jsonify({"error": "empty file"}), 400
    parsed, err = extract_timetable_from_upload(raw, f.filename, f.mimetype)
    if err:
        return jsonify({"error": err}), 400
    entries = parsed.get("entries")
    if not isinstance(entries, list) or not entries:
        return jsonify({"error": "Model returned no usable entries", "hint": str(parsed)[:500]}), 400
    inserted, unique_slots, skipped_parse, warnings = _apply_extracted_timetable(year, entries, default_class_name)
    return jsonify({"inserted": inserted, "unique_slots": unique_slots, "skipped_parse": skipped_parse, "warnings": warnings, "extracted_count": len(entries)})


@app.route("/toggle_lock", methods=["POST"])
@login_required
def toggle_lock():
    d = request.json or {}
    year = int(d.get("year", 1))
    _save_timetable_version(scope_year=year, label=f"Backup before lock toggle Y{year}", note="Automatic backup")
    con.execute(
        """
        UPDATE timetable
        SET locked = NOT locked
        WHERE year=? AND class_name=? AND day=? AND slot_index=?
        """,
        [year, d.get("class_name"), d.get("day"), int(d.get("slot_index", 0))],
    )
    return jsonify({"status": "ok"})


@app.route("/manual_edit_slot", methods=["POST"])
@login_required
def manual_edit_slot():
    d = request.json or {}
    try:
        year = _require_int(d, "year", "Year", 1, 4)
        class_name = _require_text(d, "class_name", "Class")
        day = _require_text(d, "day", "Day")
        if day not in DAYS:
            raise ValueError("Invalid day selected")
        slot_index = _require_int(d, "slot_index", "Slot", 0, len(SLOTS) - 1)
    except ValueError as e:
        return jsonify({"status": "error", "message": str(e)}), 400
    if _is_break_slot(slot_index):
        return jsonify({"status": "error", "message": f"Slot {slot_index + 1} is reserved for {BREAK_LABEL}"}), 400
    _save_timetable_version(scope_year=year, label=f"Backup before manual edit Y{year}", note=f"{class_name} {day} slot {slot_index + 1}")
    if not _clean_text(d.get("subject")):
        if _is_parallel_batch_lab(d.get("batch_name"), d.get("is_lab")):
            con.execute(
                "DELETE FROM timetable WHERE year=? AND class_name=? AND day=? AND slot_index=? AND COALESCE(batch_name,'')=? AND locked=FALSE",
                [year, class_name, day, slot_index, _clean_text(d.get("batch_name"))],
            )
            return jsonify({"status": "ok", "message": "Cleared batch lab slot"})
        con.execute(
            "DELETE FROM timetable WHERE year=? AND class_name=? AND day=? AND slot_index=? AND locked=FALSE",
            [year, class_name, day, slot_index],
        )
        return jsonify({"status": "ok", "message": "Cleared slot"})
    try:
        subject = _require_text(d, "subject", "Subject")
        faculty = _require_text(d, "faculty", "Faculty")
        room = _require_text(d, "room", "Room")
        is_lab = _parse_bool(d.get("is_lab", False))
        batch_name = _clean_text(d.get("batch_name"))
        if is_lab and not batch_name:
            raise ValueError("Batch name is required for lab slots")
        parallel_lab = _is_parallel_batch_lab(batch_name, is_lab)
        if _subject_scheduled_same_day(class_name, subject, day, year, batch_name, slot_index):
            raise ValueError("This subject is already scheduled for the same class on this day")
        issues = _validate_single_timetable_entry(
            year,
            {
                "class_name": class_name,
                "faculty": faculty,
                "subject": subject,
                "room": room,
                "day": day,
                "slot_index": slot_index,
                "batch_name": batch_name,
                "is_lab": is_lab,
            },
        )
        if issues:
            raise ValueError("; ".join(issues))
        if parallel_lab:
            existing_whole_class = con.execute(
                """
                SELECT COUNT(*)
                FROM timetable
                WHERE year=? AND class_name=? AND day=? AND slot_index=?
                  AND (is_lab=FALSE OR COALESCE(batch_name,'')='' OR UPPER(COALESCE(batch_name,''))='ALL')
                """,
                [year, class_name, day, slot_index],
            ).fetchone()[0]
            if existing_whole_class:
                raise ValueError("This slot already has a whole-class session, so the batch lab cannot be added here")
        else:
            existing_parallel_labs = con.execute(
                """
                SELECT COUNT(*)
                FROM timetable
                WHERE year=? AND class_name=? AND day=? AND slot_index=?
                  AND is_lab=TRUE AND COALESCE(batch_name,'') <> '' AND UPPER(COALESCE(batch_name,'')) <> 'ALL'
                """,
                [year, class_name, day, slot_index],
            ).fetchone()[0]
            if existing_parallel_labs:
                raise ValueError("This slot already contains batch labs for the class")
        existing_faculty = con.execute(
            """
            SELECT COUNT(*)
            FROM timetable
            WHERE year=? AND faculty=? AND day=? AND slot_index=?
              AND NOT (class_name=? AND COALESCE(batch_name,'')=?)
            """,
            [year, faculty, day, slot_index, class_name, batch_name or ""],
        ).fetchone()[0]
        if existing_faculty:
            raise ValueError("Faculty already has another class in this slot")
        existing_room = con.execute(
            """
            SELECT COUNT(*)
            FROM timetable
            WHERE year=? AND room=? AND day=? AND slot_index=?
              AND NOT (class_name=? AND COALESCE(batch_name,'')=?)
            """,
            [year, room, day, slot_index, class_name, batch_name or ""],
        ).fetchone()[0]
        if existing_room:
            raise ValueError("Room is already occupied in this slot")
    except ValueError as e:
        return jsonify({"status": "error", "message": str(e)}), 400
    if _is_parallel_batch_lab(batch_name, is_lab):
        con.execute(
            "DELETE FROM timetable WHERE year=? AND class_name=? AND day=? AND slot_index=? AND COALESCE(batch_name,'')=? AND locked=FALSE",
            [year, class_name, day, slot_index, batch_name],
        )
    else:
        con.execute(
            "DELETE FROM timetable WHERE year=? AND class_name=? AND day=? AND slot_index=? AND locked=FALSE",
            [year, class_name, day, slot_index],
        )
    inserted = _insert_timetable_rows(year, [{
        "class_name": class_name,
        "faculty": faculty,
        "subject": subject,
        "room": room,
        "day": day,
        "slot_index": slot_index,
        "batch_name": batch_name,
        "is_lab": is_lab,
    }], is_manual=True)
    return jsonify({"status": "ok", "message": "Manual slot updated", "inserted": inserted})


@app.errorhandler(413)
def request_entity_too_large(_e):
    return jsonify({"error": "File too large (max 10 MB)"}), 413


init_db()


if __name__ == "__main__":
    app.run(debug=True, use_reloader=False)
