from flask import Flask, render_template, request, jsonify, send_file, redirect, url_for, session
import duckdb
import os
import json
import csv
from functools import wraps
from dotenv import load_dotenv
from llm import generate_response

load_dotenv()
app = Flask(__name__)
app.secret_key = os.getenv("APP_SECRET", "smart-timetable-secret")
os.makedirs("database", exist_ok=True)
con = duckdb.connect("database/db.duckdb")

DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]
SLOTS = [
    "09:00-10:00",
    "10:00-11:00",
    "11:00-12:00",
    "13:00-14:00",
    "14:00-15:00",
    "15:00-16:00",
]


def _table_columns(table_name):
    return {row[1] for row in con.execute(f"PRAGMA table_info('{table_name}')").fetchall()}


def _ensure_column(table_name, column_name, column_type):
    if column_name not in _table_columns(table_name):
        con.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_type}")


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
    division = (c["division"] or "").strip()
    return f"Y{c['year']}-{division}" if division else f"Y{c['year']}-{c['name']}"


def _faculty_allowed_for_year(faculty_name, year, faculty_meta):
    f = faculty_meta.get(faculty_name)
    if not f:
        return False
    return year in f["allowed_years"]


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
        SELECT class_name,batch_name,faculty,room,day,slot_index
        FROM timetable
        WHERE locked=TRUE
        """
    ).fetchall()
    for class_name, batch_name, faculty, room, day, slot_idx in rows:
        day_idx = DAYS.index(day) if day in DAYS else -1
        if day_idx < 0:
            continue
        occupied["class"].setdefault(class_name, set()).add((day_idx, slot_idx))
        if batch_name:
            occupied["batch"].setdefault((class_name, batch_name), set()).add((day_idx, slot_idx))
        occupied["faculty"].setdefault(faculty, set()).add((day_idx, slot_idx))
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
        "SELECT name,max_day,max_week,allowed_years FROM faculty ORDER BY name"
    ).fetchall()
    faculty_meta = {
        r[0]: {"max_day": int(r[1] or 4), "max_week": int(r[2] or 20), "allowed_years": _parse_json_array(r[3], [1, 2, 3, 4])}
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
        year_subjects = [s for s in subjects if s["year"] == c["year"]]
        for s in year_subjects:
            if not _faculty_allowed_for_year(s["faculty"], c["year"], faculty_meta):
                continue
            for _ in range(s["weekly_sessions"]):
                if s["type"] == "Lab":
                    class_batches = batches_by_class.get(c["id"], []) or [{"batch_name": "ALL", "size": c["strength"]}]
                    for b in class_batches:
                        d = {
                            "year": c["year"],
                            "class_name": _class_display_name(c),
                            "batch_name": b["batch_name"],
                            "batch_size": b["size"],
                            "subject": s["name"],
                            "faculty": s["faculty"],
                            "is_lab": True,
                            "duration": s["continuous_slots"],
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
                        "faculty": s["faculty"],
                        "is_lab": False,
                        "duration": s["continuous_slots"],
                    }
                    demands.append(d)
                    req_key = (d["year"], d["class_name"], d["batch_name"], d["subject"], d["faculty"], d["is_lab"], d["duration"])
                    requirement_counter[req_key] = requirement_counter.get(req_key, 0) + 1

    demands.sort(key=lambda d: (0 if d["is_lab"] else 1, -d["duration"], d["year"], d["subject"]))
    return classes, faculty_meta, rooms, demands, requirement_counter


def _solve_with_backtracking(demands, faculty_meta, rooms):
    occupied = {"class": {}, "batch": {}, "faculty": {}, "room": {}}
    faculty_day_load = {f: {i: 0 for i in range(len(DAYS))} for f in faculty_meta}
    faculty_week_load = {f: 0 for f in faculty_meta}
    _seed_from_locked(occupied, faculty_day_load, faculty_week_load)
    placements = [None] * len(demands)
    nodes = {"count": 0}
    max_nodes = 60000

    def candidate_slots(d):
        cands = []
        if d["faculty"] not in faculty_meta:
            return cands
        f_lim = faculty_meta[d["faculty"]]
        for day_idx in range(len(DAYS)):
            if faculty_day_load[d["faculty"]][day_idx] >= f_lim["max_day"]:
                continue
            if faculty_week_load[d["faculty"]] >= f_lim["max_week"]:
                continue
            for start in range(0, len(SLOTS) - d["duration"] + 1):
                block = list(range(start, start + d["duration"]))
                if any((day_idx, s) in occupied["class"].get(d["class_name"], set()) for s in block):
                    continue
                if d["batch_name"] and any((day_idx, s) in occupied["batch"].get((d["class_name"], d["batch_name"]), set()) for s in block):
                    continue
                if any((day_idx, s) in occupied["faculty"].get(d["faculty"], set()) for s in block):
                    continue
                room = _pick_room(
                    rooms,
                    "Lab" if d["is_lab"] else "Classroom",
                    d["batch_size"] if d["is_lab"] else 1,
                    occupied["room"],
                    day_idx,
                    block,
                )
                if not room:
                    continue
                cands.append((day_idx, start, room))
        return cands

    def apply_place(idx, cand):
        d = demands[idx]
        day_idx, start, room = cand
        block = list(range(start, start + d["duration"]))
        for s in block:
            occupied["class"].setdefault(d["class_name"], set()).add((day_idx, s))
            if d["batch_name"]:
                occupied["batch"].setdefault((d["class_name"], d["batch_name"]), set()).add((day_idx, s))
            occupied["faculty"].setdefault(d["faculty"], set()).add((day_idx, s))
            occupied["room"].setdefault(room, set()).add((day_idx, s))
            faculty_day_load[d["faculty"]][day_idx] += 1
            faculty_week_load[d["faculty"]] += 1
        placements[idx] = {"day_idx": day_idx, "start": start, "room": room}

    def undo_place(idx):
        p = placements[idx]
        if not p:
            return
        d = demands[idx]
        block = list(range(p["start"], p["start"] + d["duration"]))
        for s in block:
            occupied["class"][d["class_name"]].discard((p["day_idx"], s))
            if d["batch_name"]:
                occupied["batch"][(d["class_name"], d["batch_name"])].discard((p["day_idx"], s))
            occupied["faculty"][d["faculty"]].discard((p["day_idx"], s))
            occupied["room"][p["room"]].discard((p["day_idx"], s))
            faculty_day_load[d["faculty"]][p["day_idx"]] -= 1
            faculty_week_load[d["faculty"]] -= 1
        placements[idx] = None

    def choose_index(unassigned):
        best = None
        best_count = 10**9
        for i in unassigned:
            c = len(candidate_slots(demands[i]))
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
        if cnt == 0:
            return False
        cands = candidate_slots(demands[idx])
        cands.sort(key=lambda x: (x[0], x[1]))
        for cand in cands:
            apply_place(idx, cand)
            next_unassigned = [u for u in unassigned if u != idx]
            if dfs(next_unassigned):
                return True
            undo_place(idx)
        return False

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


def _collect_validation_issues():
    issues = []
    faculty_conflicts = con.execute(
        """
        SELECT faculty, day, slot_index, COUNT(*)
        FROM timetable
        GROUP BY faculty, day, slot_index
        HAVING COUNT(*) > 1
        """
    ).fetchall()
    for r in faculty_conflicts:
        issues.append(f"Faculty conflict: {r[0]} at {r[1]} slot {r[2] + 1}")
    room_conflicts = con.execute(
        """
        SELECT room, day, slot_index, COUNT(*)
        FROM timetable
        GROUP BY room, day, slot_index
        HAVING COUNT(*) > 1
        """
    ).fetchall()
    for r in room_conflicts:
        issues.append(f"Room conflict: {r[0]} at {r[1]} slot {r[2] + 1}")
    class_conflicts = con.execute(
        """
        SELECT class_name, day, slot_index, COUNT(*)
        FROM timetable
        GROUP BY class_name, day, slot_index
        HAVING COUNT(*) > 1
        """
    ).fetchall()
    for r in class_conflicts:
        issues.append(f"Class conflict: {r[0]} at {r[1]} slot {r[2] + 1}")
    batch_conflicts = con.execute(
        """
        SELECT class_name, batch_name, day, slot_index, COUNT(*)
        FROM timetable
        WHERE batch_name <> ''
        GROUP BY class_name, batch_name, day, slot_index
        HAVING COUNT(*) > 1
        """
    ).fetchall()
    for r in batch_conflicts:
        issues.append(f"Batch conflict: {r[0]}-{r[1]} at {r[2]} slot {r[3] + 1}")
    lab_capacity_rows = con.execute(
        """
        SELECT t.year,t.class_name,t.batch_name,t.room,b.size,r.capacity
        FROM timetable t
        LEFT JOIN rooms r ON r.name=t.room
        LEFT JOIN classes c ON ('Y' || CAST(c.year AS VARCHAR) || '-' || COALESCE(NULLIF(c.division,''), c.name))=t.class_name
        LEFT JOIN batches b ON b.class_id=c.id AND b.batch_name=t.batch_name
        WHERE t.is_lab=TRUE
        """
    ).fetchall()
    for y, c_name, b_name, room, b_size, room_cap in lab_capacity_rows:
        if b_name and b_size and room_cap and int(b_size) > int(room_cap):
            issues.append(f"Capacity conflict: Y{y} {c_name} batch {b_name} exceeds room {room} capacity")
    _, _, _, _, req_counter = _build_requirements(None)
    rows = con.execute(
        """
        SELECT year,class_name,batch_name,subject,faculty,is_lab,COUNT(DISTINCT day || '-' || CAST(slot_index AS VARCHAR))
        FROM timetable
        GROUP BY year,class_name,batch_name,subject,faculty,is_lab
        """
    ).fetchall()
    scheduled_counter = {(r[0], r[1], r[2], r[3], r[4], r[5]): r[6] for r in rows}
    for req_key, expected_count in req_counter.items():
        short_key = req_key[:6]
        scheduled = scheduled_counter.get(short_key, 0)
        if scheduled < expected_count * req_key[6]:
            issues.append(
                f"Unassigned requirement: Y{req_key[0]} {req_key[1]} {req_key[3]} {req_key[4]} ({expected_count}x{req_key[6]} slots)"
            )
    return issues


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
    return render_template("timetable.html")


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
    new_id = con.execute("SELECT COALESCE(MAX(id),0)+1 FROM classes").fetchone()[0]
    con.execute(
        "INSERT INTO classes (id,name,year,department,division,strength) VALUES (?,?,?,?,?,?)",
        [
            new_id,
            data.get("name", "").strip() or f"Class-{new_id}",
            int(data.get("year", 1)),
            data.get("department", "General").strip(),
            data.get("division", "").strip(),
            int(data.get("strength", 60)),
        ],
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


@app.route("/add_faculty", methods=["POST"])
@login_required
def add_faculty():
    data = request.json or {}
    new_id = con.execute("SELECT COALESCE(MAX(id),0)+1 FROM faculty").fetchone()[0]
    allowed_years = [int(y) for y in data.get("allowed_years", [1, 2, 3, 4])]
    con.execute(
        """
        INSERT INTO faculty (id,name,department,subjects,max_day,max_week,unavailable,allowed_years)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            new_id,
            data.get("name", "").strip(),
            data.get("department", "General").strip(),
            json.dumps(data.get("subjects", [])),
            int(data.get("max_day", 4)),
            int(data.get("max_week", 20)),
            json.dumps(data.get("unavailable", [])),
            json.dumps(allowed_years),
        ],
    )
    return jsonify({"status": "success"})


@app.route("/get_faculty")
@login_required
def get_faculty():
    rows = con.execute(
        "SELECT id, name, department, max_day, max_week, allowed_years FROM faculty ORDER BY name"
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
            }
            for r in rows
        ]
    )


@app.route("/add_subject", methods=["POST"])
@login_required
def add_subject():
    data = request.json or {}
    new_id = con.execute("SELECT COALESCE(MAX(id),0)+1 FROM subjects").fetchone()[0]
    con.execute(
        """
        INSERT INTO subjects (id,name,code,type,weekly_lectures,lab_hours,duration,priority,faculty,year,weekly_sessions,continuous_slots)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            new_id,
            data.get("name", "").strip(),
            data.get("code", "").strip(),
            data.get("type", "Theory"),
            int(data.get("weekly", 1)),
            0,
            "1hr",
            data.get("priority", "Medium"),
            data.get("faculty", "").strip(),
            int(data.get("year", 1)),
            int(data.get("weekly", 1)),
            int(data.get("continuous_slots", 1)),
        ],
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


@app.route("/add_room", methods=["POST"])
@login_required
def add_room():
    data = request.json or {}
    new_id = con.execute("SELECT COALESCE(MAX(id),0)+1 FROM rooms").fetchone()[0]
    con.execute(
        "INSERT INTO rooms VALUES (?, ?, ?, ?, ?)",
        [
            new_id,
            data.get("name", "").strip(),
            data.get("type", "Classroom"),
            int(data.get("capacity", 60)),
            data.get("department", "General"),
        ],
    )
    return jsonify({"status": "success"})


@app.route("/get_rooms")
@login_required
def get_rooms():
    rows = con.execute("SELECT id, name, type, capacity, department FROM rooms ORDER BY name").fetchall()
    return jsonify(
        [{"id": r[0], "name": r[1], "type": r[2], "capacity": r[3], "department": r[4]} for r in rows]
    )


@app.route("/add_batch", methods=["POST"])
@login_required
def add_batch():
    data = request.json or {}
    class_id = int(data.get("class_id", 0))
    if class_id <= 0:
        return jsonify({"status": "error", "message": "class_id is required"}), 400
    new_id = con.execute("SELECT COALESCE(MAX(id),0)+1 FROM batches").fetchone()[0]
    class_name = con.execute("SELECT name FROM classes WHERE id=?", [class_id]).fetchone()
    con.execute(
        "INSERT INTO batches (id,class,batch_name,size,class_id) VALUES (?,?,?,?,?)",
        [
            new_id,
            class_name[0] if class_name else "",
            data.get("batch_name", "").strip(),
            int(data.get("size", 30)),
            class_id,
        ],
    )
    return jsonify({"status": "ok"})


@app.route("/get_batches")
@login_required
def get_batches():
    rows = con.execute(
        """
        SELECT b.id, b.class_id, c.year, c.division, c.name, b.batch_name, b.size
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
                "batch_name": r[5],
                "size": r[6],
            }
            for r in rows
        ]
    )


@app.route("/get_years")
@login_required
def get_years():
    rows = con.execute("SELECT DISTINCT year FROM classes ORDER BY year").fetchall()
    return jsonify([r[0] for r in rows])


@app.route("/generate")
@login_required
def generate():
    scope_year = request.args.get("year")
    scope_year = int(scope_year) if scope_year else None
    if scope_year is None:
        con.execute("DELETE FROM timetable WHERE locked=FALSE")
    else:
        con.execute("DELETE FROM timetable WHERE locked=FALSE AND year=?", [scope_year])

    classes, faculty_meta, rooms, demands, _ = _build_requirements(scope_year)
    if not classes:
        return jsonify({"message": "No classes configured", "unplaced": []}), 400
    if not rooms:
        return jsonify({"message": "No rooms configured", "unplaced": []}), 400

    placed, missing = _solve_with_backtracking(demands, faculty_meta, rooms)
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
                    d["faculty"],
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
        missing[i]["reason"] = "No feasible slot found"
    return jsonify({"message": "Generated timetable", "unplaced": missing, "placed_count": len(placed)})


@app.route("/regenerate_unlocked", methods=["POST"])
@login_required
def regenerate_unlocked():
    payload = request.json or {}
    year = payload.get("year")
    if year:
        con.execute("DELETE FROM timetable WHERE locked=FALSE AND year=?", [int(year)])
        return generate()
    con.execute("DELETE FROM timetable WHERE locked=FALSE")
    return generate()


@app.route("/validate")
@login_required
def validate():
    return jsonify(_collect_validation_issues())


@app.route("/get_timetable")
@login_required
def get_timetable():
    selected_year = int(request.args.get("year", 1))
    rows = con.execute(
        """
        SELECT class_name,batch_name,faculty,subject,room,day,slot_index,slot_label,is_lab,locked
        FROM timetable
        WHERE year=?
        ORDER BY class_name, day, slot_index
        """,
        [selected_year],
    ).fetchall()

    return jsonify(
        [
            {
                "class_name": r[0],
                "batch_name": r[1],
                "faculty": r[2],
                "subject": r[3],
                "room": r[4],
                "day": r[5],
                "slot_index": r[6],
                "time": r[7],
                "is_lab": r[8],
                "locked": r[9],
                "id": i + 1,
            }
            for i, r in enumerate(rows)
        ]
    )


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

    file_path = f"timetable-{file_suffix}.csv"
    with open(file_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["Year", "Class", "Batch", "Faculty", "Subject", "Room", "Day", "Time", "Type"])
        for r in rows:
            writer.writerow([r[0], r[1], r[2], r[3], r[4], r[5], r[6], r[7], "Lab" if r[8] else "Theory"])
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
    wb = Workbook()
    ws = wb.active
    ws.title = "Timetable"
    ws.append(["Year", "Class", "Batch", "Faculty", "Subject", "Room", "Day", "Time", "Type"])
    for r in rows:
        ws.append([r[0], r[1], r[2], r[3], r[4], r[5], r[6], r[7], "Lab" if r[8] else "Theory"])
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
    for class_name, batch_name, faculty, subject, room, day, slot_index, slot_label, is_lab in rows:
        grouped.setdefault(class_name, []).append(
            {
                "batch_name": batch_name,
                "faculty": faculty,
                "subject": subject,
                "room": room,
                "day": day,
                "slot_index": slot_index,
                "slot_label": slot_label,
                "is_lab": is_lab,
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
You are an AI assistant for timetable quality improvement in a college scheduling system.
Use the provided schedule summary and validation issues to answer the user's question.
Prefer concrete, implementable suggestions.

Scope year: {selected_year if selected_year else "All years"}

Current schedule summary:
{summary_text}

Detected issues:
{issues_text}

User question:
{user_question}

Response format:
- Keep response concise and practical.
- Use bullet points.
- Include a short "Next steps" section at the end.
""".strip()

    answer = generate_response(prompt)
    return jsonify({"scope_year": selected_year, "question": user_question, "answer": answer})


@app.route("/toggle_lock", methods=["POST"])
@login_required
def toggle_lock():
    d = request.json or {}
    con.execute(
        """
        UPDATE timetable
        SET locked = NOT locked
        WHERE year=? AND class_name=? AND day=? AND slot_index=?
        """,
        [int(d.get("year", 1)), d.get("class_name"), d.get("day"), int(d.get("slot_index", 0))],
    )
    return jsonify({"status": "ok"})


@app.route("/manual_edit_slot", methods=["POST"])
@login_required
def manual_edit_slot():
    d = request.json or {}
    year = int(d.get("year", 1))
    class_name = d.get("class_name", "")
    day = d.get("day", "")
    slot_index = int(d.get("slot_index", 0))
    con.execute(
        "DELETE FROM timetable WHERE year=? AND class_name=? AND day=? AND slot_index=? AND locked=FALSE",
        [year, class_name, day, slot_index],
    )
    if not d.get("subject"):
        return jsonify({"status": "ok", "message": "Cleared slot"})
    next_id = con.execute("SELECT COALESCE(MAX(id),0)+1 FROM timetable").fetchone()[0]
    con.execute(
        """
        INSERT INTO timetable (
            id,class,faculty,subject,room,day,time_slot,locked,year,class_name,batch_name,slot_index,slot_label,is_lab,is_manual
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        [
            next_id,
            class_name,
            d.get("faculty", ""),
            d.get("subject", ""),
            d.get("room", ""),
            day,
            SLOTS[slot_index],
            False,
            year,
            class_name,
            d.get("batch_name", ""),
            slot_index,
            SLOTS[slot_index],
            bool(d.get("is_lab", False)),
            True,
        ],
    )
    return jsonify({"status": "ok", "message": "Manual slot updated"})


if __name__ == "__main__":
    init_db()
    app.run(debug=True, use_reloader=False)