# vm_server.py (FULL FILE — updated with VSCode migration support)
# - Adds /setup_vscode endpoint to download zipped project + vscode config from S3
# - Extracts project to C:\CloudRAM\VSCode\project
# - Applies VSCode user config into %APPDATA%\Code\User
# - (Best effort) installs extensions from extensions.txt
# - Launches VSCode opening the migrated folder/workspace
#
# NOTE:
# - This assumes the VM has AWS_* env creds already (same as your existing script).
# - Make sure the VM IAM/creds allow s3:GetObject for the bucket used for VSCode zips.

from flask import Flask, request, jsonify
import os
import psutil
import subprocess
import boto3
import botocore
import time
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
import threading
import sys
import logging
import requests
import zipfile
import shutil
import uuid
from datetime import datetime
import json


# ----------------------------
# Logging
# ----------------------------
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler("C:\\CloudRAM\\vm_server.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

app = Flask(__name__)

VM_API_KEY = os.getenv("VM_API_KEY")  # required in production

# ----------------------------
# Existing Notepad Sync Config
# ----------------------------

BUCKET_NAME = os.getenv("NOTEPAD_BUCKET_NAME", "notepadfiles")
SYNCED_DIR = os.getenv("SYNCED_DIR", fr"C:\Users\vm_user\SyncedNotepadFiles")
VSCODE_BUCKET_NAME = os.getenv("VSCODE_BUCKET_NAME", "cloudram-vscode")
AWS_DEFAULT_REGION = os.getenv("AWS_DEFAULT_REGION", "us-east-1")

# ----------------------------
# AWS Session from env creds
# ----------------------------
session = boto3.Session(region_name=AWS_DEFAULT_REGION)

credentials = session.get_credentials()

if credentials is None:
    logger.error("AWS credentials not found in environment.")
else:
    logger.info("AWS credentials loaded from environment.")

s3 = session.client('s3')

# ----------------------------
# Notepad++ possible paths
# ----------------------------
NOTEPAD_PATHS = [
    r"C:\\Program Files\\Notepad++\\notepad++.exe",
    r"C:\\Program Files (x86)\\Notepad++\\notepad++.exe"
]

# In-memory task tracking
running_tasks = {}
vscode_jobs = {}  # job_id -> {status, message, started_at, finished_at, project_name}
# Track open files in Notepad++
open_notepad_files = set()

# ----------------------------
# VSCode migration constants
# ----------------------------
VSCODE_BASE_DIR = r"C:\CloudRAM\VSCode"
VSCODE_DOWNLOADS_DIR = os.path.join(VSCODE_BASE_DIR, "downloads")
VSCODE_PROJECTS_DIR = os.path.join(VSCODE_BASE_DIR, "projects")
VSCODE_CFG_DIR = os.path.join(VSCODE_BASE_DIR, "cfg")



def ensure_dir(path: str):
    os.makedirs(path, exist_ok=True)

def unzip(zip_path: str, dest_dir: str):
    ensure_dir(dest_dir)
    with zipfile.ZipFile(zip_path, "r") as zf:
        zf.extractall(dest_dir)

def find_vscode_exe():
    # Common install paths
    candidates = [
        r"C:\Program Files\Microsoft VS Code\Code.exe",
        r"C:\Program Files (x86)\Microsoft VS Code\Code.exe",
        r"C:\Users\Administrator\AppData\Local\Programs\Microsoft VS Code\Code.exe",
        r"C:\Users\vm_user\AppData\Local\Programs\Microsoft VS Code\Code.exe",
    ]
    for p in candidates:
        if os.path.exists(p):
            return p
    return "code"  # fallback if code is on PATH

def apply_vscode_user_config(cfg_dir: str):
    r"""
    cfg_dir contains:
      - settings.json
      - keybindings.json
      - snippets\...
      - extensions.txt
    Copy settings/keybindings/snippets into %APPDATA%\Code\User\
    """
    appdata = os.environ.get("APPDATA")
    if not appdata:
        # fallback typical Administrator roaming path
        appdata = r"C:\Users\Administrator\AppData\Roaming"

    user_dir = os.path.join(appdata, "Code", "User")
    ensure_dir(user_dir)

    # Copy settings.json, keybindings.json if present
    for name in ["settings.json", "keybindings.json"]:
        src = os.path.join(cfg_dir, name)
        if os.path.exists(src):
            dst = os.path.join(user_dir, name)
            shutil.copy2(src, dst)
            logger.info(f"Applied VSCode config: {name} -> {dst}")

    # Copy snippets directory
    snips_src = os.path.join(cfg_dir, "snippets")
    if os.path.isdir(snips_src):
        snips_dst = os.path.join(user_dir, "snippets")
        shutil.copytree(snips_src, snips_dst, dirs_exist_ok=True)
        logger.info(f"Applied VSCode snippets -> {snips_dst}")

    return user_dir

def install_vscode_extensions_from_file(ext_file: str):
    """
    Best effort. Requires `code` CLI available (or Code.exe supports args via cmd /c).
    """
    if not os.path.exists(ext_file):
        return

    try:
        with open(ext_file, "r", encoding="utf-8") as f:
            exts = [line.strip() for line in f if line.strip()]
    except Exception as e:
        logger.warning(f"Could not read extensions file: {e}")
        return

    if not exts:
        return

    code_exe = find_vscode_exe()
    logger.info(f"Installing {len(exts)} VSCode extensions (best effort) using: {code_exe}")

    for ext in exts:
        try:
            # Prefer code CLI if available
            subprocess.run(
                ["cmd.exe", "/c", "code", "--install-extension", ext, "--force"],
                capture_output=True, text=True, timeout=45
            )
        except Exception:
            # Fall back to running Code.exe directly (may or may not work)
            try:
                subprocess.run(
                    ["cmd.exe", "/c", code_exe, "--install-extension", ext, "--force"],
                    capture_output=True, text=True, timeout=45
                )
            except Exception as e:
                logger.warning(f"Extension install failed for {ext}: {e}")

def pick_open_target(project_dir: str, kind: str):
    """
    If workspace zipped, find *.code-workspace inside extracted project.
    Else open the project folder.
    """
    if kind == "workspace":
        for root, _, files in os.walk(project_dir):
            for fn in files:
                if fn.lower().endswith(".code-workspace"):
                    return os.path.join(root, fn)
    return project_dir

def _get_active_session_id() -> str | None:
    """
    Returns active session id (RDP/console) or None.
    """
    try:
        out = subprocess.check_output(["qwinsta"], text=True, stderr=subprocess.STDOUT)
        for line in out.splitlines():
            parts = line.split()
            # Typical:  SESSIONNAME USERNAME ID STATE ...
            if len(parts) >= 4 and parts[3].lower() == "active":
                # ID is usually the 3rd column
                return parts[2]
    except Exception as e:
        logger.warning(f"qwinsta failed: {e}")
    return None


def launch_vscode(open_target: str, cwd: str):
    code_exe = find_vscode_exe()
    logger.info(f"Launching VSCode (interactive): {code_exe} target={open_target} cwd={cwd}")

    schtasks = shutil.which("schtasks") or r"C:\Windows\System32\schtasks.exe"
    task_name = "CloudRAM-LaunchVSCode"

    # Use cmd start to detach and allow spaces safely
    # IMPORTANT: schtasks /TR needs a single command string.
    cmd = f'cmd.exe /c start "" "{code_exe}" "{open_target}"'
    # If you want it to open in the right folder context, you can cd first:
    # cmd = f'cmd.exe /c cd /d "{cwd}" && start "" "{code_exe}" "{open_target}"'

    try:
        # Delete old task if exists (ignore errors)
        subprocess.run([schtasks, "/delete", "/tn", task_name, "/f"], capture_output=True, text=True)

        # Create task to run as Administrator, interactive
        # Use a time 1 minute in the future so Windows accepts it reliably.
        future = time.localtime(time.time() + 60)
        st = time.strftime("%H:%M", future)

        create_cmd = [
            schtasks, "/create",
            "/tn", task_name,
            "/tr", cmd,
            "/sc", "once",
            "/st", st,
            "/ru", "Administrator",
            "/it",
            "/f",
        ]
        cr = subprocess.run(create_cmd, capture_output=True, text=True)
        logger.info(f"schtasks create: {cr.stdout.strip()} {cr.stderr.strip()}")

        run_cmd = [schtasks, "/run", "/tn", task_name]
        rr = subprocess.run(run_cmd, capture_output=True, text=True)
        logger.info(f"schtasks run: {rr.stdout.strip()} {rr.stderr.strip()}")

        # optional: don't delete immediately; keep for debugging
        # subprocess.run([schtasks, "/delete", "/tn", task_name, "/f"], capture_output=True, text=True)

    except Exception as e:
        logger.error(f"Failed to launch VSCode via schtasks: {e}")
        raise

def get_notepad_exe():
    for path in NOTEPAD_PATHS:
        if os.path.exists(path):
            return path
    return "notepad++.exe"

# ----------------------------
# Basic endpoints
# ----------------------------
@app.route("/")
def home():
    auth = require_api_key()
    if auth:
        return auth
    logger.info("Accessed home endpoint")
    return jsonify({"message": "Cloud RAM VM API is running!"})

@app.route("/list_tasks", methods=["GET"])
def list_tasks():
    auth = require_api_key()
    if auth:
        return auth

    target_tasks = ['notepad++.exe', 'chrome.exe', 'Code.exe']
    task_list = []
    for proc in psutil.process_iter(attrs=['pid', 'name']):
        if proc.info['name'] in target_tasks:
            task_list.append({"pid": proc.info['pid'], "name": proc.info['name']})
    return jsonify({"tasks": task_list})

@app.route("/terminate_task", methods=["POST"])
def terminate_task():
    auth = require_api_key()
    if auth:
        return auth
    data = request.get_json()
    pid = data.get("pid")
    if not pid:
        logger.error("PID required but not provided")
        return jsonify({"error": "PID required"}), 400
    try:
        process = psutil.Process(pid)
        process.terminate()
        running_tasks.pop(pid, None)
        logger.info(f"Task with PID {pid} terminated successfully")
        return jsonify({"message": f"Task with PID {pid} terminated successfully"})
    except psutil.NoSuchProcess:
        logger.error(f"Process with PID {pid} not found")
        return jsonify({"error": "Process not found"}), 404

@app.route("/ram_usage", methods=["GET"])
def ram_usage():
    auth = require_api_key()
    if auth:
        return auth
    ram_info = psutil.virtual_memory()
    return jsonify({
        "total_ram": ram_info.total,
        "used_ram": ram_info.used,
        "available_ram": ram_info.available,
        "percent_used": ram_info.percent
    })

# ----------------------------
# VSCode migration endpoint (NEW)
# ----------------------------
@app.route("/setup_vscode", methods=["POST"])
def setup_vscode():
    auth = require_api_key()
    if auth:
        return auth

    data = request.get_json(force=True) or {}

    pb = data.get("project_s3_bucket")
    pk = data.get("project_s3_key")
    cb = data.get("config_s3_bucket")
    ck = data.get("config_s3_key")
    kind = data.get("opened_path_kind", "folder")

    user_id = data.get("user_id", "unknown")
    project_name = data.get("project_name", "project")

    deps_bucket = data.get("deps_s3_bucket")
    deps_key = data.get("deps_s3_freeze_key") or data.get("deps_s3_key")  # accept both
    deps_meta_key = data.get("deps_s3_meta_key") or data.get("deps_meta_s3_key")  # accept both


    if not all([pb, pk, cb, ck]):
        return jsonify({"error": "Missing S3 bucket/key fields"}), 400

    job_id = str(uuid.uuid4())
    vscode_jobs[job_id] = {
        "status": "running",
        "message": "Starting VSCode setup...",
        "user_id": user_id,
        "project_name": project_name,
        "started_at": datetime.utcnow().isoformat() + "Z",
        "finished_at": None,
    }

    def worker():
        try:
            vscode_jobs[job_id]["message"] = "Preparing folders..."
            ensure_dir(VSCODE_BASE_DIR)
            ensure_dir(VSCODE_DOWNLOADS_DIR)
            ensure_dir(VSCODE_PROJECTS_DIR)  # make sure you have this

            project_zip = os.path.join(VSCODE_DOWNLOADS_DIR, f"{project_name}.zip")
            config_zip  = os.path.join(VSCODE_DOWNLOADS_DIR, "vscode_config.zip")
            deps_freeze = os.path.join(VSCODE_DOWNLOADS_DIR, "deps_freeze.txt")

            logger.info(f"[{job_id}] Download project s3://{pb}/{pk} -> {project_zip}")
            s3.download_file(pb, pk, project_zip)

            logger.info(f"[{job_id}] Download config s3://{cb}/{ck} -> {config_zip}")
            s3.download_file(cb, ck, config_zip)

            if deps_bucket and deps_key:
                logger.info(f"[{job_id}] Download deps s3://{deps_bucket}/{deps_key} -> {deps_freeze}")
                s3.download_file(deps_bucket, deps_key, deps_freeze)

            # ✅ project path becomes: C:\CloudRAM\VSCode\projects\<project_name>
            dest_project_dir = os.path.join(VSCODE_PROJECTS_DIR, project_name)
            if os.path.exists(dest_project_dir):
                shutil.rmtree(dest_project_dir, ignore_errors=True)

            vscode_jobs[job_id]["message"] = "Extracting project..."
            unzip(project_zip, dest_project_dir)
            project_root = normalize_extracted_project_root(dest_project_dir)
            logger.info(f"[{job_id}] project_root resolved to: {project_root}")


            vscode_jobs[job_id]["message"] = "Applying VSCode config..."
            if os.path.exists(VSCODE_CFG_DIR):
                shutil.rmtree(VSCODE_CFG_DIR, ignore_errors=True)
            unzip(config_zip, VSCODE_CFG_DIR)
            apply_vscode_user_config(VSCODE_CFG_DIR)

            vscode_jobs[job_id]["message"] = "Installing extensions..."
            ext_file = os.path.join(VSCODE_CFG_DIR, "extensions.txt")
            install_vscode_extensions_from_file(ext_file)

            # ✅ deps install (best effort)
            if os.path.exists(deps_freeze):
                vscode_jobs[job_id]["message"] = "Installing Python packages..."
                dep_result = install_deps_from_freeze(project_root, deps_freeze)
                write_vscode_python_interpreter(project_root)
                logger.info(f"[{job_id}] dep_result: {dep_result}")
            else:
                logger.info(f"[{job_id}] deps_freeze not found, skipping deps install")

            open_target = pick_open_target(project_root, kind)

            vscode_jobs[job_id]["message"] = "Launching VSCode..."
            launch_vscode(open_target=open_target, cwd=project_root)

            vscode_jobs[job_id]["status"] = "done"
            vscode_jobs[job_id]["message"] = f"VSCode opened: {open_target}"
            vscode_jobs[job_id]["finished_at"] = datetime.utcnow().isoformat() + "Z"

        except Exception as e:
            logger.error(f"[{job_id}] setup_vscode failed: {e}", exc_info=True)
            vscode_jobs[job_id]["status"] = "error"
            vscode_jobs[job_id]["message"] = str(e)
            vscode_jobs[job_id]["finished_at"] = datetime.utcnow().isoformat() + "Z"

    threading.Thread(target=worker, daemon=True).start()

    # ✅ Return immediately so local side doesn't timeout
    return jsonify({"ok": True, "job_id": job_id})
    
def install_deps_from_freeze(project_dir: str, freeze_file: str):
    """
    Creates/uses project_dir\\.venv and installs dependencies from a pip-freeze style file.
    Best effort:
      - bulk install: pip install -r deps_freeze.txt
      - fallback: install line-by-line (skips editable/path-based)
    Returns a dict for logs/UI.
    """
    project_dir = os.path.abspath(project_dir)
    freeze_file = os.path.abspath(freeze_file)

    if not os.path.isdir(project_dir):
        raise RuntimeError(f"Project dir not found: {project_dir}")
    if not os.path.isfile(freeze_file):
        raise RuntimeError(f"Freeze file not found: {freeze_file}")

    venv_dir = os.path.join(project_dir, ".venv")
    venv_py = os.path.join(venv_dir, "Scripts", "python.exe")

    base_py = "python"  # system python

    logger.info(f"Installing deps from freeze: {freeze_file}")
    logger.info(f"Project dir: {project_dir}")

    # 1) Create venv if missing
    if not os.path.exists(venv_py):
        logger.info(f"Creating venv at: {venv_dir}")
        subprocess.check_call([base_py, "-m", "venv", venv_dir])

    # 2) If venv dir exists but python.exe missing/broken, recreate
    if os.path.exists(venv_dir) and not os.path.exists(venv_py):
        logger.warning("Found .venv dir but missing python.exe; recreating venv...")
        shutil.rmtree(venv_dir, ignore_errors=True)
        subprocess.check_call([base_py, "-m", "venv", venv_dir])

    if not os.path.exists(venv_py):
        raise RuntimeError("venv python was not created properly (missing .venv\\Scripts\\python.exe)")

    # 3) Upgrade pip
    logger.info("Upgrading pip inside venv...")
    subprocess.check_call([venv_py, "-m", "pip", "install", "--upgrade", "pip"])

    # 4) Bulk install
    logger.info("Installing deps from freeze file (bulk)...")
    try:
        subprocess.check_call([venv_py, "-m", "pip", "install", "-r", freeze_file], cwd=project_dir)
        return {
            "ok": True,
            "mode": "bulk",
            "venv": venv_dir
        }
    except Exception as bulk_err:
        logger.warning(f"Bulk install failed, falling back to line-by-line. Error: {bulk_err}")

    # 5) Line-by-line fallback
    failed = []
    installed = 0

    with open(freeze_file, "r", encoding="utf-8", errors="ignore") as f:
        lines = [ln.strip() for ln in f.readlines() if ln.strip() and not ln.strip().startswith("#")]

    for ln in lines:
        # Skip machine-specific / path based installs
        if ln.startswith("-e ") or " @" in ln:
            failed.append({"pkg": ln, "error": "Skipped editable/path-based requirement"})
            continue

        try:
            subprocess.check_call([venv_py, "-m", "pip", "install", ln], cwd=project_dir)
            installed += 1
        except Exception as e:
            failed.append({"pkg": ln, "error": str(e)})

    return {
        "ok": installed > 0,
        "mode": "line-by-line",
        "venv": venv_dir,
        "installed_count": installed,
        "failed_count": len(failed),
        "failed": failed[:25]
    }

# ----------------------------
# Notepad migration endpoint (existing)
# ----------------------------
@app.route("/run_task", methods=["POST"])
def run_task():
    auth = require_api_key()
    if auth:
        return auth
    try:
        logger.info("/run_task endpoint called")
        data = request.get_json()
        task = data.get("task")

        if not task:
            return jsonify({"error": "Task name required"}), 400

        if task != "notepad++.exe":
            return jsonify({"error": f"Unsupported task: {task}"}), 400

        # Ensure directories exist
        os.makedirs(SYNCED_DIR, exist_ok=True)
        os.makedirs("C:\\CloudRAM", exist_ok=True)

        # Sync files from S3
        try:
            sync_notepad_files()
        except Exception as sync_error:
            logger.error(f"Sync error: {sync_error}")

        # Gather file paths
        file_paths = [
            os.path.join(SYNCED_DIR, f)
            for f in os.listdir(SYNCED_DIR)
            if os.path.isfile(os.path.join(SYNCED_DIR, f)) and f.endswith(('.txt', '.cpp', '.py', '.html'))
        ]

        if not file_paths:
            logger.info("No files found to open")
            return jsonify({"message": "No files found to open", "file_count": 0})

        # Get Notepad++ executable path
        notepad_exe = get_notepad_exe()
        if not os.path.exists(notepad_exe):
            logger.error(f"Notepad++ executable not found at {notepad_exe}")
            return jsonify({"error": "Notepad++ executable not found"}), 500

        # Log current session info for debugging
        try:
            session_info = subprocess.run(
                ["wmic", "process", "where", f"ProcessID={os.getpid()}", "get", "SessionId"],
                capture_output=True, text=True
            )
            logger.info(f"Flask app running in session: {session_info.stdout}")
        except Exception as e:
            logger.error(f"Failed to get session info: {e}")

        # Get the active session ID (VNC session)
        try:
            session_check = subprocess.run(
                ["qwinsta"], capture_output=True, text=True
            )
            logger.info(f"Active sessions: {session_check.stdout}")
            active_session_id = None
            for line in session_check.stdout.splitlines():
                parts = line.split()
                if len(parts) >= 4 and parts[3] == "Active" and ("console" in line.lower() or "rdp-tcp" in line.lower()):
                    active_session_id = parts[2]  # Session ID is the third column
                    break
            logger.info(f"Detected active session ID: {active_session_id}")
        except Exception as e:
            logger.error(f"Failed to get active session: {e}")
            active_session_id = None

        # Use schtasks to launch Notepad++ in the active session
        task_name = "LaunchNotepad"
        # OLD (remove)
        # cmd = f'"{notepad_exe}" {" ".join([f"\\"{path}\\"" for path in file_paths])}'

        # NEW (Windows-safe quoting)
        quoted_files = " ".join([f'"{p}"' for p in file_paths])
        cmd = f'"{notepad_exe}" {quoted_files}'

        logger.info(f"Preparing to launch Notepad++ with schtasks command: {cmd}")

        try:
            # Delete existing task if it exists
            delete_result = subprocess.run(
                ["schtasks", "/delete", "/tn", task_name, "/f"],
                capture_output=True, text=True
            )
            logger.info(f"schtasks delete output: {delete_result.stdout}")
            if delete_result.stderr:
                logger.error(f"schtasks delete error: {delete_result.stderr}")

            # Create a scheduled task to run immediately under the Administrator user
            create_task_cmd = [
                "schtasks", "/create", "/tn", task_name, "/tr", cmd,
                "/sc", "once", "/st", "00:00", "/ru", "Administrator", "/it"
            ]
            create_result = subprocess.run(
                create_task_cmd, capture_output=True, text=True
            )
            logger.info(f"schtasks create output: {create_result.stdout}")
            if create_result.stderr:
                logger.error(f"schtasks create error: {create_result.stderr}")

            # Check if task was created
            query_task = subprocess.run(
                ["schtasks", "/query", "/tn", task_name],
                capture_output=True, text=True
            )
            logger.info(f"schtasks query output: {query_task.stdout}")
            if query_task.stderr:
                logger.error(f"schtasks query error: {query_task.stderr}")

            # Run the task immediately
            run_task_cmd = ["schtasks", "/run", "/tn", task_name]
            run_result = subprocess.run(
                run_task_cmd, capture_output=True, text=True
            )
            logger.info(f"schtasks run output: {run_result.stdout}")
            if run_result.stderr:
                logger.error(f"schtasks run error: {run_result.stderr}")

            # Check if Notepad++ is running
            time.sleep(2)  # Give it a moment to start
            task_check = subprocess.run(
                ["tasklist", "/FI", "IMAGENAME eq notepad++.exe", "/V"],
                capture_output=True, text=True
            )
            logger.info(f"Tasklist output for Notepad++: {task_check.stdout}")

            # Extract PID if Notepad++ is running
            pid = None
            for line in task_check.stdout.splitlines():
                if "notepad++.exe" in line.lower():
                    parts = line.split()
                    pid = parts[1]  # PID is the second column
                    break

            if not pid:
                logger.warning("Notepad++ not found in tasklist, trying notepad.exe as fallback")
                # OLD (remove)
                # cmd_fallback = f'"notepad.exe" {" ".join([f"\\"{path}\\"" for path in file_paths])}'

                # NEW
                quoted_files = " ".join([f'"{p}"' for p in file_paths])
                cmd_fallback = f'"notepad.exe" {quoted_files}'

                create_task_cmd = [
                    "schtasks", "/create", "/tn", task_name, "/tr", cmd_fallback,
                    "/sc", "once", "/st", "00:00", "/ru", "Administrator", "/it"
                ]
                create_result = subprocess.run(
                    create_task_cmd, capture_output=True, text=True
                )
                logger.info(f"schtasks create (fallback) output: {create_result.stdout}")
                if create_result.stderr:
                    logger.error(f"schtasks create (fallback) error: {create_result.stderr}")

                run_result = subprocess.run(
                    ["schtasks", "/run", "/tn", task_name],
                    capture_output=True, text=True
                )
                logger.info(f"schtasks run (fallback) output: {run_result.stdout}")
                if run_result.stderr:
                    logger.error(f"schtasks run (fallback) error: {run_result.stderr}")

                time.sleep(2)
                task_check = subprocess.run(
                    ["tasklist", "/FI", "IMAGENAME eq notepad.exe", "/V"],
                    capture_output=True, text=True
                )
                logger.info(f"Tasklist output for notepad.exe: {task_check.stdout}")

                for line in task_check.stdout.splitlines():
                    if "notepad.exe" in line.lower():
                        parts = line.split()
                        pid = parts[1]
                        break

            opened_files = file_paths
            if pid:
                running_tasks[pid] = {"name": task, "files": file_paths}
                return jsonify({
                    "message": "Launched with files",
                    "file_count": len(opened_files),
                    "files": opened_files,
                    "pid": pid
                })
            else:
                return jsonify({"error": "Failed to launch application"}), 500

        except subprocess.SubprocessError as e:
            logger.error(f"Failed to launch: {e}")
            return jsonify({"error": f"Failed to launch: {str(e)}"}), 500

    except Exception as e:
        error_msg = f"Error in /run_task: {str(e)}"
        logger.error(error_msg, exc_info=True)
        return jsonify({"error": error_msg}), 500

# ----------------------------
# Notepad sync endpoints (existing)
# ----------------------------
@app.route("/sync_notepad_files", methods=["POST"])
def sync_notepad_files_endpoint():
    auth = require_api_key()
    if auth:
        return auth

    data = request.get_json()
    specific_file = data.get("file")

    if specific_file:
        logger.info(f"Syncing specific file: {specific_file}")
        sync_specific_file(specific_file)
        # Refresh open files if needed
        refresh_open_files_in_notepad()
    else:
        logger.info("Syncing all Notepad++ files")
        sync_notepad_files()

    return jsonify({"message": "Notepad++ files synced with S3"})

def sync_specific_file(filename):
    """Sync a specific file from S3"""
    os.makedirs(SYNCED_DIR, exist_ok=True)
    local_path = os.path.join(SYNCED_DIR, filename)

    try:
        logger.info(f"Downloading {filename} to {local_path}")
        s3.download_file(BUCKET_NAME, filename, local_path)
        logger.info(f"Downloaded {filename}")

        # If this file is open in Notepad++, refresh it
        if local_path in open_notepad_files:
            logger.info(f"File {filename} is open in Notepad++")
    except Exception as e:
        logger.error(f"Error downloading {filename}: {e}")

def sync_notepad_files():
    os.makedirs(SYNCED_DIR, exist_ok=True)
    logger.info(f"Syncing from S3 bucket: {BUCKET_NAME}")

    try:
        response = s3.list_objects_v2(Bucket=BUCKET_NAME)
        objects = response.get('Contents', [])

        if not objects:
            logger.info("No files found in S3 bucket")
            return

        for obj in objects:
            s3_key = obj['Key']
            filename = os.path.basename(s3_key)
            local_path = os.path.join(SYNCED_DIR, filename)

            try:
                logger.info(f"Downloading {s3_key} to {local_path}")
                s3.download_file(BUCKET_NAME, s3_key, local_path)
                logger.info(f"Downloaded {filename}")
            except Exception as e:
                logger.error(f"Error downloading {s3_key}: {e}")

    except botocore.exceptions.ClientError as ce:
        logger.error(f"S3 ClientError: {ce}")
    except Exception as e:
        logger.error(f"General sync error: {e}")

def upload_to_s3(file_path):
    """Upload modified file to S3 bucket"""
    if not os.path.isfile(file_path):
        logger.error(f"Cannot upload non-existent file: {file_path}")
        return

    try:
        filename = os.path.basename(file_path)
        logger.info(f"Uploading {filename} to S3")
        s3.upload_file(file_path, BUCKET_NAME, filename)
        logger.info(f"Uploaded {filename} to S3")

    except Exception as e:
        logger.error(f"Error uploading {file_path} to S3: {e}")

@app.route("/upload_modified_file", methods=["POST"])
def upload_modified_file():
    auth = require_api_key()
    if auth:
        return auth
    data = request.get_json()
    file_path = data.get("file_path")

    if not file_path:
        logger.error("No file path provided")
        return jsonify({"error": "File path required"}), 400

    if not os.path.isfile(file_path):
        logger.error(f"File not found: {file_path}")
        return jsonify({"error": "File not found"}), 404

    try:
        upload_to_s3(file_path)
        return jsonify({"message": f"File {file_path} uploaded to S3"})
    except Exception as e:
        logger.error(f"Error uploading file: {e}")
        return jsonify({"error": str(e)}), 500

def check_for_open_notepad_files():
    """Check if any notepad processes have the synced files open"""
    if not os.path.exists(SYNCED_DIR):
        return []

    synced_files = [
        os.path.join(SYNCED_DIR, f)
        for f in os.listdir(SYNCED_DIR)
        if f.endswith(('.txt', '.cpp', '.py', '.html'))
    ]

    if not synced_files:
        return []

    open_files = []
    for proc in psutil.process_iter(['pid', 'name']):
        if proc.info['name'] == 'notepad++.exe':
            try:
                p = psutil.Process(proc.info['pid'])
                for file in p.open_files():
                    if file.path in synced_files:
                        open_files.append(file.path)
            except (psutil.AccessDenied, psutil.NoSuchProcess):
                pass

    return open_files

def refresh_open_files_in_notepad():
    """Alert Notepad++ to reload files that may have changed"""
    open_files = check_for_open_notepad_files()
    if not open_files:
        logger.info("No open files to refresh")
        return

    logger.info(f"Files open in Notepad++: {open_files}")
    # Notepad++ auto-detects file changes; ensure files are synced.

class NotepadSyncHandler(FileSystemEventHandler):
    def on_modified(self, event):
        if event.is_directory:
            return
        if event.src_path.endswith(('.txt', '.cpp', '.py', '.html')):
            logger.info(f"Detected change on VM: {event.src_path}")
            upload_to_s3(event.src_path)

    def on_created(self, event):
        if event.is_directory:
            return
        if event.src_path.endswith(('.txt', '.cpp', '.py', '.html')):
            logger.info(f"New file created on VM: {event.src_path}")
            upload_to_s3(event.src_path)

def start_vm_file_watcher():
    event_handler = NotepadSyncHandler()
    observer = Observer()
    observer.schedule(event_handler, SYNCED_DIR, recursive=True)
    observer.start()
    logger.info(f"Watching for changes in VM files at: {SYNCED_DIR}")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        observer.stop()
    observer.join()


@app.route("/export_project", methods=["POST"])
def export_project():
    auth = require_api_key()
    if auth:
        return auth

    data = request.get_json(force=True) or {}
    user_id = (data.get("user_id") or "").strip()
    project_name = (data.get("project_name") or "").strip()

    if not user_id or not project_name:
        return jsonify({"error": "user_id and project_name required"}), 400

    # ✅ sanitize project_name (avoid path traversal / slashes)
    project_name = os.path.basename(project_name.rstrip("\\/"))
    if not project_name:
        return jsonify({"error": "Invalid project_name"}), 400

    # ✅ User-based VM layout
    base_dir = r"C:\CloudRAM\VSCode\projects"
    project_dir = os.path.join(base_dir, user_id, project_name)

    if not os.path.isdir(project_dir):
        return jsonify({"error": f"Project not found on VM: {project_dir}"}), 404

    stamp = str(int(time.time()))
    export_key = f"users/{user_id}/exports/{project_name}/{stamp}/project.zip"

    exports_dir = r"C:\CloudRAM\VSCode\exports"
    os.makedirs(exports_dir, exist_ok=True)
    zip_path = os.path.join(exports_dir, f"{project_name}_{stamp}.zip")

    # ✅ Zip INCLUDING top folder name exactly as project_name/
    try:
        with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for root, dirs, files in os.walk(project_dir):
                for f in files:
                    full = os.path.join(root, f)
                    rel_inside_project = os.path.relpath(full, project_dir)
                    arcname = os.path.join(project_name, rel_inside_project)
                    zf.write(full, arcname)
    except Exception as e:
        return jsonify({"error": f"Zip failed: {e}"}), 500

    try:
        s3.upload_file(zip_path, VSCODE_BUCKET_NAME, export_key)
    except Exception as e:
        return jsonify({"error": f"S3 upload failed: {e}"}), 500

    return jsonify({
        "bucket": VSCODE_BUCKET_NAME,
        "export_key": export_key,
        "project_name": project_name,
        "vm_project_dir": project_dir
    })

@app.route("/vscode_setup_status/<job_id>", methods=["GET"])
def vscode_setup_status(job_id):
    job = vscode_jobs.get(job_id)
    if not job:
        return jsonify({"error": "job not found"}), 404
    return jsonify(job)

def normalize_extracted_project_root(dest_project_dir: str) -> str:
    """
    If extraction produced a single top-level folder inside dest_project_dir,
    return that inner folder as the actual project root.
    """
    try:
        items = [x for x in os.listdir(dest_project_dir) if x not in (".", "..")]
        full_items = [os.path.join(dest_project_dir, x) for x in items]

        dirs = [p for p in full_items if os.path.isdir(p)]
        files = [p for p in full_items if os.path.isfile(p)]

        # If there's exactly one directory and no files at root, treat it as project root.
        if len(dirs) == 1 and len(files) == 0:
            return dirs[0]
    except Exception:
        pass
    return dest_project_dir


def write_vscode_python_interpreter(project_root: str):
    """
    Forces VSCode to use the project's .venv automatically AND forces terminal PATH to venv.
    This fixes: `python -m uvicorn ...` using system python.
    """
    project_root = os.path.abspath(project_root)

    venv_py = os.path.join(project_root, ".venv", "Scripts", "python.exe")
    venv_scripts = os.path.join(project_root, ".venv", "Scripts")
    if not os.path.exists(venv_py):
        logger.warning(f"No venv python found at {venv_py} (skipping VSCode interpreter settings)")
        return

    vscode_dir = os.path.join(project_root, ".vscode")
    os.makedirs(vscode_dir, exist_ok=True)

    settings_path = os.path.join(vscode_dir, "settings.json")

    # Load existing settings.json if present (don’t overwrite user stuff)
    settings = {}
    try:
        if os.path.exists(settings_path):
            with open(settings_path, "r", encoding="utf-8") as f:
                settings = json.load(f) or {}
    except Exception as e:
        logger.warning(f"Could not read existing VSCode settings.json, overwriting. Reason: {e}")
        settings = {}

    # 1) Tell Python extension what interpreter to use
    settings["python.defaultInterpreterPath"] = venv_py
    settings["python.terminal.activateEnvironment"] = True

    # 2) HARD FORCE integrated terminal to use venv (this fixes your exact issue)
    term_env = settings.get("terminal.integrated.env.windows", {}) or {}
    term_env["VIRTUAL_ENV"] = os.path.join(project_root, ".venv")
    # Prepend venv Scripts to PATH (keep existing PATH by using ${env:Path})
    term_env["Path"] = f"{venv_scripts};${{env:Path}}"
    settings["terminal.integrated.env.windows"] = term_env

    with open(settings_path, "w", encoding="utf-8") as f:
        json.dump(settings, f, indent=2)

    logger.info(f"✅ VSCode venv forced in settings: {settings_path}")


def require_api_key():
    if not VM_API_KEY:
        return  # allow dev if not set
    supplied = request.headers.get("X-VM-API-KEY")
    if supplied != VM_API_KEY:
        return jsonify({"error": "Unauthorized"}), 401

if __name__ == "__main__":
    logger.info("Starting VM server...")
    ensure_dir("C:\\CloudRAM")
    watcher_thread = threading.Thread(target=start_vm_file_watcher, daemon=True)
    watcher_thread.start()
    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)
