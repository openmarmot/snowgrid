import os
import json
import time
import threading
import tempfile
import subprocess
from datetime import datetime
from flask import Flask, render_template, request, redirect, url_for, jsonify
import requests
from openai import OpenAI

app = Flask(__name__)
CONFIG_FILE = 'config.json'
WORKER_STATUS = {"task_id": None, "status": "Idle", "log": []}
HEALTH_STATUS = {
    "task_api": "Pending",
    "llm": "Pending",
    "opencode": "Pending",
    "overall": "Pending",
    "last_check": None,
    "errors": {}
}

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

# ====================== PROMPTS (unchanged - already excellent) ======================
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

    "judge": """You are a strict, accurate, and impartial task completion judge.

TASK DESCRIPTION:
{description}

AGENT STDOUT (this is the main output / result the agent produced):
{stdout}

AGENT STDERR (runtime logs from the tool — usually just normal model loading messages like "build · ggml-..." — these are NOT errors and can be ignored unless they show a real crash):
{stderr}

Evaluate whether the agent SUCCESSFULLY completed the task EXACTLY as described.

- For creative tasks (haiku, story, poem, etc.): Does STDOUT contain exactly what was asked?
- For coding/analysis tasks: Did it perform the required steps and produce the expected result?
- Ignore normal STDERR model-loading lines completely.

Be fair but strict. Only mark as DONE if the STDOUT clearly proves the task is 100% complete.

Reply with ONLY this exact JSON (no markdown, no extra text, no explanations):
{{
  "decision": "DONE" or "NEED_MORE",
  "reason": "one short sentence explaining your decision"
}}
"""
}

# ====================== HELPER FUNCTIONS ======================
def run_opencode_task(description: str, previous_output: str, model: str, cwd: str, timeout: int = 720):
    """Run opencode. Returns (full_output_for_logs, stdout, stderr, success)."""
    prompt = PROMPTS["opencode"].format(
        description=description,
        previous_output=previous_output or "None yet"
    )

    try:
        result = subprocess.run(
            ["opencode", "run", prompt, "--model", model],
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=cwd
        )
        full_output = f"STDOUT:\n{result.stdout}\n\nSTDERR:\n{result.stderr}"
        return full_output, result.stdout, result.stderr, result.returncode == 0
    except subprocess.TimeoutExpired:
        return "TIMEOUT after 12 minutes", "TIMEOUT", "TIMEOUT", False
    except Exception as e:
        err = f"ERROR running opencode: {e}"
        return err, err, "", False


def judge_task_completion(description: str, stdout: str, stderr: str, base_url: str, model: str):
    """Single attempt at judging (core logic). Raises on any error so retries can happen higher up."""
    prompt = PROMPTS["judge"].format(
        description=description,
        stdout=stdout[:7000],
        stderr=stderr[:1000]
    )

    raw = ""
    try:
        client = OpenAI(base_url=base_url, api_key="dummy")
        resp = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            timeout=90
        )
        raw = resp.choices[0].message.content.strip()

        if raw.startswith("```json"):
            raw = raw.split("```json", 1)[1].split("```", 1)[0].strip()
        elif raw.startswith("```"):
            raw = raw.split("```", 1)[1].strip()

        decision = json.loads(raw)
        if "decision" not in decision or "reason" not in decision:
            raise ValueError("Missing keys in JSON")
        return {
            "decision": decision["decision"],
            "reason": decision.get("reason", "No reason provided")
        }
    except Exception as e:
        log(f"Judge attempt failed: {e} | Raw: {raw[:300] if raw else 'empty'}")
        raise  # let the retry wrapper catch it


def judge_with_retries(description: str, stdout: str, stderr: str, base_url: str, model: str):
    """Retry the judge up to 3 times on any error/timeout before giving up.
    This prevents re-running the expensive opencode task just because the judge flaked."""
    max_retries = 3
    for attempt in range(1, max_retries + 1):
        try:
            decision = judge_task_completion(description, stdout, stderr, base_url, model)
            if attempt > 1:
                log(f"✅ Judge succeeded on retry {attempt}/{max_retries}")
            return decision
        except Exception:
            if attempt < max_retries:
                wait = attempt * 2
                log(f"Judge retry {attempt}/{max_retries} failed — waiting {wait}s before next try...")
                time.sleep(wait)
            else:
                log(f"❌ Judge failed after {max_retries} retries")
                return {"decision": "NEED_MORE", "reason": "Judge failed after 3 retries (timeout/parse error)"}


# ====================== HEALTH CHECK (unchanged) ======================
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
        if config.get("LLM_BASE_URL"):
            client = OpenAI(base_url=config["LLM_BASE_URL"], api_key="dummy")
            client.models.list(timeout=8)
            HEALTH_STATUS["llm"] = "Healthy"
            healthy_count += 1
        else:
            HEALTH_STATUS["llm"] = "Unhealthy"
            errors["llm"] = "Not configured"
    except Exception as e:
        HEALTH_STATUS["llm"] = "Unhealthy"
        errors["llm"] = str(e)[:150]

    try:
        model = config.get("OPEN_CODE_MODEL", "").strip()
        if not model:
            raise ValueError("OPEN_CODE_MODEL not set")
        result = subprocess.run(
            ["opencode", "run", "Print exactly: HEALTH_CHECK_OK", "--model", model],
            capture_output=True,
            text=True,
            timeout=30,
            cwd="/"
        )
        stdout = result.stdout.lower()
        if result.returncode == 0 and "health_check_ok" in stdout:
            HEALTH_STATUS["opencode"] = "Healthy"
            healthy_count += 1
        else:
            HEALTH_STATUS["opencode"] = "Unhealthy"
            error_msg = (result.stderr or result.stdout or "Unknown error")[:200]
            if "model not found" in error_msg.lower():
                error_msg = f"Model '{model}' not found. Check OPEN_CODE_MODEL."
            errors["opencode"] = error_msg
    except Exception as e:
        HEALTH_STATUS["opencode"] = "Unhealthy"
        errors["opencode"] = str(e)[:200]

    HEALTH_STATUS["overall"] = "Healthy" if healthy_count == 3 else "Unhealthy"
    HEALTH_STATUS["last_check"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    HEALTH_STATUS["errors"] = errors
    return HEALTH_STATUS["overall"] == "Healthy"


# ====================== WORKER LOOP ======================
def worker_loop():
    global config
    while True:
        if not perform_health_check():
            log(f"Health check FAILED — waiting 45s")
            time.sleep(45)
            continue

        if not all(k in config for k in ["TASK_API_URL", "LLM_BASE_URL", "LLM_MODEL", "OPEN_CODE_MODEL"]):
            time.sleep(10)
            continue

        try:
            tasks = call_task_api("GET", "/api/tasks?status=new")["tasks"]
            if not tasks:
                log("No new tasks — sleeping 30s")
                time.sleep(30)
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
                    log(f"Working in temp dir: {tmpdir}")
                    full_output, stdout, stderr, _ = run_opencode_task(
                        description=description,
                        previous_output=previous_output,
                        model=config["OPEN_CODE_MODEL"],
                        cwd=tmpdir
                    )

                print("\n" + "="*100)
                print(f"FULL OPCODE OUTPUT — ATTEMPT {attempt} — TASK {task_id}")
                print("="*100)
                print(full_output)
                print("="*100 + "\n")

                log("Full opencode output printed above")
                update_task(task_id, work=full_output, log_note=f"Attempt {attempt} completed")

                previous_output = full_output[:3500]

                # === Judge with built-in retries (new) ===
                decision = judge_with_retries(
                    description=description,
                    stdout=stdout,
                    stderr=stderr,
                    base_url=config["LLM_BASE_URL"],
                    model=config["LLM_MODEL"]
                )

                log(f"LLM decision: {decision.get('decision')} — {decision.get('reason')}")

                if decision.get("decision") == "DONE":
                    update_task(task_id, status="complete", log_note=f"Completed on attempt {attempt}")
                    log(f"✅ Task {task_id} marked COMPLETE on attempt {attempt}")
                    log("✅ Task marked COMPLETE")
                    break
                elif attempt == max_attempts:
                    update_task(task_id, status="failed", log_note=f"Failed after {max_attempts} attempts — {decision.get('reason')}")
                    log(f"❌ Task {task_id} marked FAILED after {max_attempts} attempts — {decision.get('reason')}")
                    log("❌ Task marked FAILED")
                else:
                    log("Retrying full task...")

        except Exception as e:
            log(f"Worker error: {e}")
            if WORKER_STATUS.get("task_id"):
                update_task(WORKER_STATUS["task_id"], status="failed", log_note=f"Agent crashed: {str(e)[:200]}")
        finally:
            WORKER_STATUS["task_id"] = None
            WORKER_STATUS["status"] = "Idle"


# ====================== FLASK UI (unchanged) ======================
@app.route('/', methods=['GET', 'POST'])
def index():
    global config
    if request.method == 'POST':
        config = {
            "TASK_API_URL": request.form["TASK_API_URL"],
            "LLM_BASE_URL": request.form["LLM_BASE_URL"],
            "LLM_MODEL": request.form["LLM_MODEL"],
            "OPEN_CODE_MODEL": request.form["OPEN_CODE_MODEL"]
        }
        save_config(config)
        log("Configuration updated via web UI")
        perform_health_check()
        log("Health check triggered after config save")
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
    log("AI Worker Agent started — judge now retries 3x on timeout/error before retrying full task")
    app.run(host='0.0.0.0', port=5001, debug=False)