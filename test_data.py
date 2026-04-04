import duckdb
import sys

try:
    con = duckdb.connect('database/db.duckdb')
    
    # Test years
    classes_years = con.execute('SELECT DISTINCT year FROM classes ORDER BY year').fetchall()
    timetable_years = con.execute('SELECT DISTINCT year FROM timetable').fetchall()
    print(f"Classes years: {classes_years}")
    print(f"Timetable years: {timetable_years}")
    
    # Merge years
    years = sorted({int(y[0]) for y in classes_years + timetable_years if y[0] is not None})
    print(f"Final years: {years}")
    
    # Test departments
    depts = con.execute("SELECT DISTINCT department FROM classes WHERE department IS NOT NULL AND department != '' ORDER BY department").fetchall()
    print(f"Departments: {depts}")
    
    # Check class count
    class_count = con.execute('SELECT COUNT(*) FROM classes').fetchone()
    print(f"Total classes: {class_count}")
    
except Exception as e:
    print(f"Error: {e}")
    sys.exit(1)
