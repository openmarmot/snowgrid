import os
import json
import time
import threading
import tempfile
import subprocess
from datetime import datetime
from flask import Flask, render_template, request, redirect, url_for, jsonify
import requests

app = Flask(__name__)
CONFIG_FILE = 'config.json'
WORKER_STATUS = {"task_id": None, "status": "Idle", "log": []}
HEALTH_STATUS = {
    "task_api": "Pending",
    "opencode": "Pending",
    "overall": "Pending",
    "last_check": None,
    "errors": {}
}

def quick_health_check():
    try:
        call_task_api("GET", "/api/tasks")
        HEALTH_STATUS["task_api"] = "Healthy"
        HEALTH_STATUS["last_check"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        return True
    except Exception as e:
        HEALTH_STATUS["task_api"] = "Unhealthy"
        HEALTH_STATUS["overall"] = "Unhealthy"
        HEALTH_STATUS["errors"]["task_api"] = str(e)[:150]
        HEALTH_STATUS["last_check"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        return False

def load_config():
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE) as f:
            return json.load(f)
    return {}

def save_config(config):
    with open(CONFIG_FILE, 'w') as f:
        json.dump(config, f, indent=2)

config = load_config()

def log(msg):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    entry = f"[{timestamp}] {msg}"
    print(entry)
    WORKER_STATUS["log"].append(entry)
    if len(WORKER_STATUS["log"]) > 100:
        WORKER_STATUS["log"].pop(0)

def call_task_api(method, endpoint, json_data=None):
    url = config.get("TASK_API_URL", "").rstrip("/") + endpoint
    r = requests.request(method, url, json=json_data, timeout=15)
    r.raise_for_status()
    return r.json() if r.text else None

def update_task(task_id, status=None, work=None, log_note=None):
    data = {}
    if status: data["status"] = status
    if work is not None: data["work"] = work
    if log_note: data["log_note"] = log_note
    try:
        call_task_api("PATCH", f"/api/tasks/{task_id}", data)
    except Exception as e:
        log(f"Update failed: {e}")

PROMPTS = {
    "opencode": """You are an expert AI coding agent. Follow the task description EXACTLY as written:

{description}

Rules:
- If the task contains "git clone", run that command first.
- After cloning, cd into the cloned folder and do the rest of the task.
- Use any tools or git commands needed.
- At the end, print a clear summary of what you did and your findings.

Previous attempt output:
{previous_output}

Start now.""",

    "judge": """You are a strict task completion judge.

TASK DESCRIPTION:
{description}

AGENT OUTPUT:
{agent_output}

Did the agent complete the task EXACTLY as described?

Start your reply with exactly one of these two lines and nothing else before it:

DONE: one short sentence why it is complete
NEED_MORE: one short sentence why more work is needed"""
}

def run_opencode_task(description: str, previous_output: str, model: str, cwd: str, timeout: int = 720):
    prompt = PROMPTS["opencode"].format(
        description=description,
        previous_output=previous_output or "None yet"
    )
    try:
        result = subprocess.run(
            ["opencode", "run", prompt, "--model", model],
            capture_output=True, text=True, timeout=timeout, cwd=cwd
        )
        full_output = f"STDOUT:\n{result.stdout}\n\nSTDERR:\n{result.stderr}"
        agent_output = f"Execution trace:\n{result.stderr}\n\nFinal summary:\n{result.stdout}"
        return full_output, agent_output
    except subprocess.TimeoutExpired:
        return "TIMEOUT after 12 minutes", "TIMEOUT"
    except Exception as e:
        err = f"ERROR running opencode: {e}"
        return err, err

def run_opencode_judge(description: str, agent_output: str, model: str):
    prompt = PROMPTS["judge"].format(
        description=description,
        agent_output=agent_output[:7500]
    )
    try:
        result = subprocess.run(
            ["opencode", "run", prompt, "--model", model],
            capture_output=True, text=True, timeout=90, cwd="/"
        )
        raw = result.stdout.strip()
        log(f"RAW JUDGE OUTPUT:\n{raw}")

        # Super simple parsing — looks for the magic line at the start
        for line in raw.splitlines()[:8]:
            line = line.strip()
            if line.upper().startswith("DONE:"):
                return {"decision": "DONE", "reason": line[5:].strip()}
            if line.upper().startswith("NEED_MORE:"):
                return {"decision": "NEED_MORE", "reason": line[10:].strip()}

        return {"decision": "NEED_MORE", "reason": "Judge did not start with DONE: or NEED_MORE:"}
    except Exception as e:
        log(f"Judge crashed: {e}")
        return {"decision": "NEED_MORE", "reason": "Judge execution error"}

def perform_health_check():
    global HEALTH_STATUS
    errors = {}
    healthy_count = 0

    try:
        call_task_api("GET", "/api/tasks")
        HEALTH_STATUS["task_api"] = "Healthy"
        healthy_count += 1
    except Exception as e:
        HEALTH_STATUS["task_api"] = "Unhealthy"
        errors["task_api"] = str(e)[:150]

    try:
        model = config.get("OPEN_CODE_MODEL", "").strip()
        if not model:
            raise ValueError("OPEN_CODE_MODEL not set")

        result = subprocess.run(
            ["opencode", "run", "Print exactly: HEALTH_CHECK_OK", "--model", model],
            capture_output=True, text=True, timeout=90, cwd="/"   # ← was 30s
        )

        output = (result.stdout + result.stderr).lower()
        if result.returncode == 0 and "health_check_ok" in output:
            HEALTH_STATUS["opencode"] = "Healthy"
            healthy_count += 1
        else:
            HEALTH_STATUS["opencode"] = "Unhealthy"
            errors["opencode"] = f"Bad output (code {result.returncode}): {output[:250]}"
    except subprocess.TimeoutExpired:
        HEALTH_STATUS["opencode"] = "Unhealthy"
        errors["opencode"] = "opencode run timed out after 90s (normal cold-start on 35B model)"
    except Exception as e:
        HEALTH_STATUS["opencode"] = "Unhealthy"
        errors["opencode"] = str(e)[:250]

    HEALTH_STATUS["overall"] = "Healthy" if healthy_count == 2 else "Unhealthy"
    HEALTH_STATUS["last_check"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    HEALTH_STATUS["errors"] = errors
    return HEALTH_STATUS["overall"] == "Healthy"

def worker_loop():
    global config
    log("AI Worker started — simple plain-text judge (no JSON)")
    perform_health_check()

    while True:
        quick_health_check()

        if not all(k in config for k in ["TASK_API_URL", "OPEN_CODE_MODEL"]):
            time.sleep(10)
            continue

        try:
            tasks = call_task_api("GET", "/api/tasks?status=new")["tasks"]
            if not tasks:
                time.sleep(30)
                continue

            log("New task — running full health check...")
            if not perform_health_check():
                time.sleep(45)
                continue

            task = tasks[0]
            task_id = task["id"]
            WORKER_STATUS["task_id"] = task_id
            WORKER_STATUS["status"] = f"Working on {task_id}"

            log(f"Starting task {task_id}")
            update_task(task_id, status="wip", log_note="Agent picked up task")

            description = task["description"]
            max_attempts = 3
            previous_output = ""

            for attempt in range(1, max_attempts + 1):
                log(f"--- Attempt {attempt}/{max_attempts} for {task_id} ---")

                with tempfile.TemporaryDirectory() as tmpdir:
                    full_output, agent_output = run_opencode_task(
                        description, previous_output, config["OPEN_CODE_MODEL"], tmpdir
                    )

                print("\n" + "="*100)
                print(f"FULL OPCODE OUTPUT — ATTEMPT {attempt} — TASK {task_id}")
                print("="*100)
                print(agent_output)
                print("="*100 + "\n")

                update_task(task_id, work=full_output, log_note=f"Attempt {attempt} completed")
                previous_output = full_output[:3500]

                decision = run_opencode_judge(description, agent_output, config["OPEN_CODE_MODEL"])

                log(f"Judge: {decision.get('decision')} — {decision.get('reason')}")

                if decision.get("decision") == "DONE":
                    update_task(task_id, status="review", log_note=f"Completed on attempt {attempt}")
                    log(f"✅ Task {task_id} marked REVIEW")
                    break
                elif attempt == max_attempts:
                    update_task(task_id, status="failed", log_note=f"Failed after {max_attempts} attempts")
                    log(f"❌ Task {task_id} marked FAILED")
                else:
                    log("Retrying...")

        except Exception as e:
            log(f"Worker error: {e}")
            if WORKER_STATUS.get("task_id"):
                update_task(WORKER_STATUS["task_id"], status="failed", log_note=f"Crash: {str(e)[:150]}")
        finally:
            WORKER_STATUS["task_id"] = None
            WORKER_STATUS["status"] = "Idle"

@app.route('/', methods=['GET', 'POST'])
def index():
    global config
    if request.method == 'POST':
        config = {
            "TASK_API_URL": request.form["TASK_API_URL"],
            "OPEN_CODE_MODEL": request.form["OPEN_CODE_MODEL"]
        }
        save_config(config)
        log("Configuration updated")
        perform_health_check()
        return redirect(url_for('index'))

    return render_template('agent.html',
        status=WORKER_STATUS["status"],
        task_id=WORKER_STATUS["task_id"],
        logs="\n".join(reversed(WORKER_STATUS["log"][-40:])),
        health=HEALTH_STATUS,
        last_check=HEALTH_STATUS["last_check"],
        config=config
    )

@app.route('/health')
def health_json():
    return jsonify(HEALTH_STATUS)

if __name__ == '__main__':
    threading.Thread(target=worker_loop, daemon=True).start()
    log("Snowgrid Shard Agent started — now using simple plain-text judge (no JSON)")
    app.run(host='0.0.0.0', port=5001, debug=False)