import duckdb
import json
from datetime import datetime
from pathlib import Path
from app import init_db

BASE_DIR = Path(__file__).resolve().parent
init_db()

# Connect to database
con = duckdb.connect(str(BASE_DIR / 'database' / 'db.duckdb'))

print("🔄 Populating database with sample data...\n")

# ============ ADD DEPARTMENTS & CLASSES ============
print("📚 Adding sample classes...")

classes_data = [
    # Computer Science Department
    (1, "CSE-A", 1, "Computer Science", "A", 60),
    (2, "CSE-B", 1, "Computer Science", "B", 60),
    (3, "CSE-A", 2, "Computer Science", "A", 60),
    (4, "CSE-B", 2, "Computer Science", "B", 60),
    (5, "CSE-A", 3, "Computer Science", "A", 55),
    (6, "CSE-B", 3, "Computer Science", "B", 55),
    
    # Electronics Department
    (7, "ECE-A", 1, "Electronics", "A", 60),
    (8, "ECE-B", 1, "Electronics", "B", 60),
    (9, "ECE-A", 2, "Electronics", "A", 60),
    (10, "ECE-B", 2, "Electronics", "B", 60),
    
    # Mechanical Department
    (11, "ME-A", 1, "Mechanical", "A", 60),
    (12, "ME-B", 1, "Mechanical", "B", 60),
    (13, "ME-A", 2, "Mechanical", "A", 60),
    (14, "ME-B", 2, "Mechanical", "B", 60),
]

for class_id, name, year, dept, division, strength in classes_data:
    con.execute(
        "INSERT INTO classes (id, name, year, department, division, strength) VALUES (?, ?, ?, ?, ?, ?)",
        [class_id, name, year, dept, division, strength]
    )

print(f"✅ Added {len(classes_data)} classes across 3 departments\n")

# ============ ADD FACULTY ============
print("👨‍🏫 Adding sample faculty...")

faculty_data = [
    # Computer Science Faculty
    (1, "Dr. Rajesh Kumar", "Computer Science", json.dumps(["Data Structures", "Algorithms", "DBMS"]), 5, 20, json.dumps([]), json.dumps([1,2,3,4])),
    (2, "Prof. Anita Singh", "Computer Science", json.dumps(["Web Development", "Python"]), 4, 16, json.dumps([]), json.dumps([1,2,3])),
    (3, "Dr. Priya Sharma", "Computer Science", json.dumps(["Machine Learning", "AI"]), 5, 20, json.dumps([]), json.dumps([2,3,4])),
    (4, "Mr. Vikram Patel", "Computer Science", json.dumps(["Database Lab", "System Design"]), 4, 18, json.dumps([]), json.dumps([1,2,3])),
    
    # Electronics Faculty
    (5, "Dr. Suresh Gupta", "Electronics", json.dumps(["Circuits", "Signals"]), 5, 20, json.dumps([]), json.dumps([1,2,3,4])),
    (6, "Prof. Neha Verma", "Electronics", json.dumps(["Digital Electronics", "Microprocessors"]), 4, 16, json.dumps([]), json.dumps([1,2,3])),
    
    # Mechanical Faculty
    (7, "Dr. Harish Rao", "Mechanical", json.dumps(["Thermodynamics", "Mechanics"]), 5, 20, json.dumps([]), json.dumps([1,2,3,4])),
    (8, "Prof. Meera Nair", "Mechanical", json.dumps(["Design", "CAD"]), 4, 16, json.dumps([]), json.dumps([1,2,3,4])),
]

for fac_id, name, dept, subjects, max_day, max_week, unavailable, allowed_years in faculty_data:
    con.execute(
        "INSERT INTO faculty (id, name, department, subjects, max_day, max_week, unavailable, allowed_years) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [fac_id, name, dept, subjects, max_day, max_week, unavailable, allowed_years]
    )

print(f"✅ Added {len(faculty_data)} faculty members\n")

# ============ ADD SUBJECTS ============
print("📖 Adding sample subjects...")

subjects_data = [
    # Year 1 CSE
    (1, "Data Structures", "CS101", "Theory", 3, 0, "50 min", "High", "Dr. Rajesh Kumar", 1),
    (2, "Algorithms", "CS102", "Theory", 3, 0, "50 min", "High", "Dr. Rajesh Kumar", 1),
    (3, "Web Development", "CS103", "Theory", 2, 2, "50 min", "Medium", "Prof. Anita Singh", 1),
    (4, "Python Programming", "CS104", "Lab", 0, 3, "50 min", "High", "Prof. Anita Singh", 1),
    
    # Year 2 CSE
    (5, "Database Management", "CS201", "Theory", 3, 0, "50 min", "High", "Dr. Rajesh Kumar", 2),
    (6, "DBMS Lab", "CS202", "Lab", 0, 3, "50 min", "High", "Mr. Vikram Patel", 2),
    (7, "Machine Learning", "CS203", "Theory", 3, 0, "50 min", "High", "Dr. Priya Sharma", 2),
    (8, "AI Workshop", "CS204", "Lab", 0, 2, "50 min", "Medium", "Dr. Priya Sharma", 2),
    
    # Year 1 ECE
    (9, "Circuit Analysis", "EC101", "Theory", 3, 0, "50 min", "High", "Dr. Suresh Gupta", 1),
    (10, "Circuit Lab", "EC102", "Lab", 0, 3, "50 min", "High", "Dr. Suresh Gupta", 1),
    (11, "Digital Electronics", "EC103", "Theory", 3, 0, "50 min", "High", "Prof. Neha Verma", 1),
    
    # Year 1 Mechanical
    (12, "Thermodynamics", "ME101", "Theory", 3, 0, "50 min", "High", "Dr. Harish Rao", 1),
    (13, "Engineering Mechanics", "ME102", "Theory", 3, 0, "50 min", "High", "Dr. Harish Rao", 1),
    (14, "CAD Basics", "ME103", "Lab", 0, 3, "50 min", "High", "Prof. Meera Nair", 1),
]

for subj_id, name, code, subj_type, lectures, labs, duration, priority, faculty, year in subjects_data:
    weekly_sessions = labs if subj_type == "Lab" else lectures
    continuous_slots = 2 if subj_type == "Lab" else 1
    con.execute(
        """
        INSERT INTO subjects (
            id, name, code, type, weekly_lectures, lab_hours, duration, priority, faculty, year, weekly_sessions, continuous_slots
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [subj_id, name, code, subj_type, lectures, labs, duration, priority, faculty, year, weekly_sessions, continuous_slots]
    )

print(f"✅ Added {len(subjects_data)} subjects\n")

# ============ ADD ROOMS ============
print("🏛️ Adding sample rooms...")

rooms_data = [
    (1, "A-101", "Classroom", 60, "Computer Science"),
    (2, "A-102", "Classroom", 60, "Computer Science"),
    (3, "A-103", "Classroom", 50, "Computer Science"),
    (4, "A-LAB-1", "Lab", 30, "Computer Science"),
    (5, "A-LAB-2", "Lab", 30, "Computer Science"),
    (6, "B-201", "Classroom", 60, "Electronics"),
    (7, "B-202", "Classroom", 60, "Electronics"),
    (8, "B-LAB-1", "Lab", 30, "Electronics"),
    (9, "C-301", "Classroom", 60, "Mechanical"),
    (10, "C-302", "Classroom", 60, "Mechanical"),
    (11, "C-LAB-1", "Lab", 30, "Mechanical"),
    (12, "Main-Hall", "Auditorium", 200, "General"),
]

for room_id, name, room_type, capacity, dept in rooms_data:
    con.execute(
        "INSERT INTO rooms (id, name, type, capacity, department) VALUES (?, ?, ?, ?, ?)",
        [room_id, name, room_type, capacity, dept]
    )

print(f"✅ Added {len(rooms_data)} rooms\n")

# ============ ADD BATCHES FOR LAB SCHEDULING ============
print("🧪 Adding sample batches...")

batch_id = 1
for class_id, name, year, dept, division, strength in classes_data:
    for batch_name in ("B1", "B2"):
        con.execute(
            "INSERT INTO batches (id, class, batch_name, size, class_id) VALUES (?, ?, ?, ?, ?)",
            [batch_id, name, batch_name, strength // 2, class_id]
        )
        batch_id += 1

print(f"✅ Added {batch_id - 1} batches\n")

# ============ ADD SAMPLE TIMETABLE ENTRIES ============
print("📅 Adding sample timetable entries...")

days = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]
slots = ["09:00-10:00", "10:00-11:00", "11:00-12:00", "13:00-14:00", "14:00-15:00", "15:00-16:00"]

timetable_entries = [
    # Y1-CSE-A entries
    ("Y1-A", "", "Dr. Rajesh Kumar", "Data Structures", "A-101", "Monday", 0, "09:00-10:00", 0, 0),
    ("Y1-A", "", "Prof. Anita Singh", "Web Development", "A-102", "Monday", 1, "10:00-11:00", 0, 0),
    ("Y1-A", "", "Prof. Anita Singh", "Python Programming", "A-LAB-1", "Tuesday", 2, "11:00-12:00", 1, 0),
    ("Y1-A", "", "Dr. Rajesh Kumar", "Algorithms", "A-101", "Wednesday", 0, "09:00-10:00", 0, 0),
    ("Y1-A", "", "Prof. Anita Singh", "Web Development", "A-102", "Wednesday", 3, "13:00-14:00", 0, 0),
    ("Y1-A", "", "Prof. Anita Singh", "Python Programming", "A-LAB-2", "Thursday", 2, "11:00-12:00", 1, 0),
    ("Y1-A", "", "Dr. Rajesh Kumar", "Data Structures", "A-101", "Friday", 1, "10:00-11:00", 0, 0),
    
    # Y1-ECE-A entries
    ("Y1-A", "", "Dr. Suresh Gupta", "Circuit Analysis", "B-201", "Monday", 0, "09:00-10:00", 0, 0),
    ("Y1-A", "", "Prof. Neha Verma", "Digital Electronics", "B-202", "Monday", 1, "10:00-11:00", 0, 0),
    ("Y1-A", "", "Dr. Suresh Gupta", "Circuit Lab", "B-LAB-1", "Tuesday", 2, "11:00-12:00", 1, 0),
    ("Y1-A", "", "Dr. Suresh Gupta", "Circuit Analysis", "B-201", "Wednesday", 0, "09:00-10:00", 0, 0),
    
    # Y2-CSE-A entries
    ("Y2-A", "", "Dr. Rajesh Kumar", "Database Management", "A-101", "Monday", 0, "09:00-10:00", 0, 0),
    ("Y2-A", "", "Dr. Priya Sharma", "Machine Learning", "A-102", "Monday", 1, "10:00-11:00", 0, 0),
    ("Y2-A", "", "Mr. Vikram Patel", "DBMS Lab", "A-LAB-1", "Tuesday", 2, "11:00-12:00", 1, 0),
    ("Y2-A", "", "Dr. Priya Sharma", "Machine Learning", "A-102", "Wednesday", 0, "09:00-10:00", 0, 0),
    ("Y2-A", "", "Dr. Rajesh Kumar", "Database Management", "A-101", "Wednesday", 3, "13:00-14:00", 0, 0),
    ("Y2-A", "", "Dr. Priya Sharma", "AI Workshop", "A-LAB-2", "Thursday", 2, "11:00-12:00", 1, 0),
]

year_mapping = {
    "Y1-A": 1,
    "Y1-B": 1,
    "Y2-A": 2,
    "Y2-B": 2,
    "Y3-A": 3,
    "Y3-B": 3,
}

for class_name, batch_name, faculty, subject, room, day, slot_index, slot_label, is_lab, locked in timetable_entries:
    year = year_mapping.get(class_name, 1)
    con.execute(
        "INSERT INTO timetable (class_name, batch_name, faculty, subject, room, day, slot_index, slot_label, is_lab, locked, year) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [class_name, batch_name, faculty, subject, room, day, slot_index, slot_label, is_lab, locked, year]
    )

print(f"✅ Added {len(timetable_entries)} timetable entries\n")

con.commit()

# ============ VERIFY DATA ============
print("✅ VERIFICATION:\n")

class_count = con.execute("SELECT COUNT(*) FROM classes").fetchone()[0]
print(f"   Total Classes: {class_count}")

dept_result = con.execute("SELECT DISTINCT department FROM classes ORDER BY department").fetchall()
departments = [d[0] for d in dept_result]
print(f"   Departments: {', '.join(departments)}")

year_result = con.execute("SELECT DISTINCT year FROM classes ORDER BY year").fetchall()
years = [y[0] for y in year_result]
print(f"   Years: {', '.join(map(str, years))}")

faculty_count = con.execute("SELECT COUNT(*) FROM faculty").fetchone()[0]
print(f"   Total Faculty: {faculty_count}")

subject_count = con.execute("SELECT COUNT(*) FROM subjects").fetchone()[0]
print(f"   Total Subjects: {subject_count}")

room_count = con.execute("SELECT COUNT(*) FROM rooms").fetchone()[0]
print(f"   Total Rooms: {room_count}")

timetable_count = con.execute("SELECT COUNT(*) FROM timetable").fetchone()[0]
print(f"   Total Timetable Entries: {timetable_count}")

print("\n" + "="*50)
print("✨ Sample data population complete!")
print("="*50)
print("\n📝 You can now:")
print("   1. Go to Schedule page")
print("   2. Select a department from the dropdown")
print("   3. Select a year from the dropdown")
print("   4. View the timetable")
print("   5. Test the Save Snapshot button (visible in dark mode)")
print("\n")

con.close()
