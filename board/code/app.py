from flask import Flask, render_template, request, jsonify, redirect, url_for, g
import sqlite3
import uuid
from datetime import datetime
import os

app = Flask(__name__)
DATABASE = 'database.db'

def get_db():
    db = getattr(g, '_database', None)
    if db is None:
        db = g._database = sqlite3.connect(DATABASE)
        db.row_factory = sqlite3.Row
    return db

@app.teardown_appcontext
def close_connection(exception):
    db = getattr(g, '_database', None)
    if db is not None:
        db.close()

# ====================== INTEGRATED DB INIT + MIGRATION ======================
def init_db():
    conn = get_db()
    
    # Check if we have an old table that needs migration
    old_table = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='tasks'").fetchone()
    if old_table:
        # Check if the old CHECK constraint is missing 'failed'
        create_stmt = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='tasks'"
        ).fetchone()[0]
        if 'failed' not in create_stmt:
            print("🔄 Old database detected — performing automatic migration...")
            conn.execute("ALTER TABLE tasks RENAME TO tasks_old")
            
            conn.execute('''
                CREATE TABLE tasks (
                    id TEXT PRIMARY KEY,
                    description TEXT NOT NULL,
                    status TEXT DEFAULT 'new' CHECK(status IN ('new', 'wip', 'review', 'complete', 'failed')),
                    work TEXT DEFAULT '',
                    work_log TEXT DEFAULT '',
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            
            conn.execute('''
                INSERT INTO tasks (id, description, status, work, work_log, created_at, updated_at)
                SELECT id, description, status, work, work_log, created_at, updated_at FROM tasks_old
            ''')
            conn.execute("DROP TABLE tasks_old")
            print("✅ Migration complete — 'failed' status is now supported!")

    # Always ensure the current table exists with full schema
    conn.execute('''
        CREATE TABLE IF NOT EXISTS tasks (
            id TEXT PRIMARY KEY,
            description TEXT NOT NULL,
            status TEXT DEFAULT 'new' CHECK(status IN ('new', 'wip', 'review', 'complete', 'failed')),
            work TEXT DEFAULT '',
            work_log TEXT DEFAULT '',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    conn.commit()
    print("✅ Database ready (integrated init + migration)")

def append_to_log(current_log, note):
    if not note:
        return current_log or ''
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return (current_log or '') + f"[{ts}] {note}\n\n"

# ====================== CORE UPDATE LOGIC ======================
def perform_task_update(task_id, data):
    conn = get_db()
    task = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if not task:
        return False, "Task not found"

    updates = []
    params = []
    log_parts = []

    if 'status' in data and data['status'] != task['status']:
        updates.append("status = ?")
        params.append(data['status'])
        log_parts.append(f"Status changed from {task['status']} to {data['status']}")

    for field in ['description', 'work']:
        if field in data and data[field] is not None:
            updates.append(f"{field} = ?")
            params.append(data[field])

    if 'log_note' in data and data['log_note']:
        log_parts.append(data['log_note'])

    if log_parts:
        new_log = append_to_log(task['work_log'], " | ".join(log_parts))
        updates.append("work_log = ?")
        params.append(new_log)

    if updates:
        updates.append("updated_at = CURRENT_TIMESTAMP")
        conn.execute(f"UPDATE tasks SET {', '.join(updates)} WHERE id = ?", params + [task_id])
        conn.commit()
        return True, "Updated"
    return True, "No changes"

# ====================== API ======================
@app.route('/api/tasks', methods=['POST'])
def api_create():
    data = request.get_json() or {}
    if not data.get('description'):
        return jsonify({"error": "description is required"}), 400
    task_id = str(uuid.uuid4())
    work_log = append_to_log('', f"Task created")
    conn = get_db()
    conn.execute(
        "INSERT INTO tasks (id, description, status, work_log) VALUES (?, ?, ?, ?)",
        (task_id, data['description'], data.get('status', 'new'), work_log)
    )
    conn.commit()
    return jsonify({"success": True, "task_id": task_id}), 201

@app.route('/api/tasks', methods=['GET'])
def api_list():
    status_filter = request.args.get('status')
    conn = get_db()
    query = "SELECT id, description, status, created_at, updated_at FROM tasks"
    params = []
    if status_filter and status_filter in ['new', 'wip', 'review', 'complete', 'failed']:
        query += " WHERE status = ?"
        params = [status_filter]
    query += """ ORDER BY 
        CASE status 
            WHEN 'new' THEN 0 
            WHEN 'wip' THEN 1 
            WHEN 'review' THEN 2 
            WHEN 'complete' THEN 3 
            WHEN 'failed' THEN 4 
        END, updated_at DESC"""
    tasks = [dict(row) for row in conn.execute(query, params).fetchall()]
    return jsonify({"tasks": tasks})

@app.route('/api/tasks/<task_id>', methods=['GET'])
def api_get(task_id):
    task = get_db().execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if not task: return jsonify({"error": "Task not found"}), 404
    return jsonify(dict(task))

@app.route('/api/tasks/<task_id>', methods=['PATCH'])
def api_update(task_id):
    data = request.get_json() or {}
    success, msg = perform_task_update(task_id, data)
    if not success:
        return jsonify({"error": msg}), 404
    return jsonify({"success": True, "task_id": task_id})

# ====================== WEB UI ======================
@app.route('/')
def index():
    conn = get_db()
    tasks = conn.execute("""
        SELECT id, description, status, updated_at 
        FROM tasks 
        ORDER BY CASE status 
            WHEN 'new' THEN 0 WHEN 'wip' THEN 1 WHEN 'review' THEN 2 WHEN 'complete' THEN 3 WHEN 'failed' THEN 4 
        END, updated_at DESC
    """).fetchall()

    grouped = {'new': [], 'wip': [], 'review': [], 'complete': [], 'failed': []}
    for t in tasks:
        grouped[t['status']].append(dict(t))

    return render_template('index.html', grouped=grouped)

@app.route('/create', methods=['POST'])
def web_create():
    desc = request.form.get('description')
    if desc:
        task_id = str(uuid.uuid4())
        work_log = append_to_log('', f"Task created")
        get_db().execute(
            "INSERT INTO tasks (id, description, status, work_log) VALUES (?, ?, ?, ?)",
            (task_id, desc, 'new', work_log)
        )
        get_db().commit()
    return redirect(url_for('index'))

@app.route('/tasks/<task_id>')
def task_detail(task_id):
    task = get_db().execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if not task:
        return "Task not found", 404
    return render_template('task_detail.html', task=dict(task))

@app.route('/tasks/<task_id>/update', methods=['POST'])
def web_update(task_id):
    data = {
        "status": request.form.get('status'),
        "work": request.form.get('work'),
        "log_note": request.form.get('log_note'),
        "description": request.form.get('description')
    }
    success, msg = perform_task_update(task_id, data)
    if not success:
        return msg, 404
    return redirect(url_for('index'))

if __name__ == '__main__':
    with app.app_context():      # ← THIS LINE FIXES THE ERROR
        init_db()
    app.run(debug=True)