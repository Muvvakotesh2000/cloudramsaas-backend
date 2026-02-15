# backend/main.py (PUBLIC-SAFE)
# ✅ Render backend should NOT use local Windows paths, psutil, win32*, or touch user's PC.
# ✅ This file keeps: Auth (Supabase), AWS VM lifecycle, and VM orchestration calls.

from fastapi import FastAPI, HTTPException, Depends
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from aws_manager import AWSManager

import uvicorn
import os
import requests
import time
from typing import Optional, List, Dict, Any


app = FastAPI()
aws_manager = AWSManager()

# -------------------------
# CORS (configurable)
# -------------------------
# ✅ Fix: getenv only takes 2 args. Put all defaults in one string.
CORS_ORIGINS = os.getenv(
    "CORS_ORIGINS",
    "http://localhost:5000,http://127.0.0.1:5000,https://cloudramsaas-frontend.onrender.com",
).split(",")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in CORS_ORIGINS if o.strip()],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

security = HTTPBearer()

# -------------------------
# Supabase (configurable)
# -------------------------
SUPABASE_URL = os.getenv("SUPABASE_URL", "https://fyxxsoepzwhgfmndscxt.supabase.co")
SUPABASE_ANON_KEY = os.getenv("SUPABASE_ANON_KEY", "")

if not SUPABASE_ANON_KEY:
    raise RuntimeError("SUPABASE_ANON_KEY is missing. Set it in environment variables.")

# -------------------------
# VM API Key (optional but recommended)
# -------------------------
# If your VM server has VM_API_KEY set, your backend should send it.
VM_API_KEY = os.getenv("VM_API_KEY", "")


def verify_token_raw(token: str) -> dict:
    if not token:
        raise HTTPException(status_code=401, detail="Missing access token")

    try:
        resp = requests.get(
            f"{SUPABASE_URL}/auth/v1/user",
            headers={
                "Authorization": f"Bearer {token}",
                "apikey": SUPABASE_ANON_KEY,
            },
            timeout=8,
        )
    except requests.RequestException as e:
        raise HTTPException(status_code=503, detail=f"Auth service unreachable: {str(e)}")

    if resp.status_code != 200:
        try:
            detail = resp.json()
        except Exception:
            detail = resp.text
        raise HTTPException(status_code=401, detail=f"Invalid or expired token: {detail}")

    return resp.json()


async def verify_token(credentials: HTTPAuthorizationCredentials = Depends(security)):
    return verify_token_raw(credentials.credentials)


# -------------------------
# Models
# -------------------------
class RamRequest(BaseModel):
    ram_size: int = Field(..., ge=1, le=64)


class VmActionRequest(BaseModel):
    vm_id: Optional[str] = None


class BeaconStopRequest(BaseModel):
    vm_id: str
    access_token: str


class RamUsageRequest(BaseModel):
    vm_ip: str


class VmRunTaskRequest(BaseModel):
    vm_ip: str
    task: str


class VmMigrateTasksRequest(BaseModel):
    vm_ip: str
    task_names: List[str]


class SetupVSCodeOnVmRequest(BaseModel):
    vm_ip: str

    # S3 pointers created by LOCAL AGENT
    user_id: str
    project_name: str

    project_s3_bucket: str
    project_s3_key: str

    config_s3_bucket: str
    config_s3_key: str

    opened_path_kind: str = "folder"  # "folder" or "workspace"

    # Optional deps pointers created by LOCAL AGENT
    deps_s3_bucket: Optional[str] = None
    deps_s3_key: Optional[str] = None
    deps_meta_s3_key: Optional[str] = None


# -------------------------
# Helpers: talk to VM
# -------------------------
def _vm_headers() -> Dict[str, str]:
    headers = {}
    if VM_API_KEY:
        headers["X-VM-API-KEY"] = VM_API_KEY
    return headers


def _vm_post(vm_ip: str, path: str, payload: Dict[str, Any], timeout: int = 30) -> requests.Response:
    url = f"http://{vm_ip}:5000{path}"
    try:
        return requests.post(url, json=payload, headers=_vm_headers(), timeout=timeout)
    except requests.RequestException as e:
        raise HTTPException(status_code=503, detail=f"VM unreachable: {str(e)}")


def _vm_get(vm_ip: str, path: str, timeout: int = 15) -> requests.Response:
    url = f"http://{vm_ip}:5000{path}"
    try:
        return requests.get(url, headers=_vm_headers(), timeout=timeout)
    except requests.RequestException as e:
        raise HTTPException(status_code=503, detail=f"VM unreachable: {str(e)}")


# -------------------------
# Basic endpoints
# -------------------------
@app.get("/health")
async def health_check():
    return {"status": "healthy"}


@app.get("/my_vm")
async def my_vm(user: dict = Depends(verify_token)):
    user_id = user.get("id")
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid user payload (missing id)")

    inst = aws_manager.find_user_instance(user_id)
    if not inst:
        return {"exists": False}

    return {
        "exists": True,
        "vm_id": inst["InstanceId"],
        "state": inst["State"]["Name"],
        "ip": inst.get("PublicIpAddress"),
    }


# =========================
# ✅ STOP VM (button)
# =========================
@app.post("/stop_vm")
async def stop_vm(req: VmActionRequest, user: dict = Depends(verify_token)):
    user_id = user.get("id")
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid user payload (missing id)")

    inst = aws_manager.find_user_instance(user_id)
    if not inst:
        raise HTTPException(status_code=404, detail="No VM found for this user.")

    vm_id = req.vm_id or inst["InstanceId"]
    ok = aws_manager.stop_vm(vm_id)
    if not ok:
        raise HTTPException(status_code=500, detail="Failed to stop VM.")
    return {"message": f"Stopping VM {vm_id}."}


# =========================
# ✅ STOP VM (beacon - best effort)
# =========================
@app.post("/stop_vm_beacon")
async def stop_vm_beacon(req: BeaconStopRequest):
    user = verify_token_raw(req.access_token)
    user_id = user.get("id")
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid user payload (missing id)")

    inst = aws_manager.find_user_instance(user_id)
    if not inst:
        return {"message": "No VM found for this user."}

    vm_id = req.vm_id or inst["InstanceId"]
    ok = aws_manager.stop_vm(vm_id)
    if not ok:
        raise HTTPException(status_code=500, detail="Failed to stop VM.")
    return {"message": f"Stopping VM {vm_id}."}


@app.post("/start_vm")
async def start_vm(req: VmActionRequest, user: dict = Depends(verify_token)):
    user_id = user.get("id")
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid user payload (missing id)")

    inst = aws_manager.find_user_instance(user_id)
    if not inst:
        raise HTTPException(status_code=404, detail="No VM found for this user.")

    vm_id = req.vm_id or inst["InstanceId"]
    ok = aws_manager.start_vm(vm_id)
    if not ok:
        raise HTTPException(status_code=500, detail="Failed to start VM.")

    try:
        ip = aws_manager.wait_for_running_and_ip(vm_id)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"VM start timed out: {str(e)}")

    services_ok = aws_manager.wait_for_vm_services(ip_address=ip, max_attempts=60)
    if not services_ok:
        raise HTTPException(status_code=500, detail="VM started but services not ready in time.")

    return {"message": f"VM {vm_id} started.", "vm_id": vm_id, "ip": ip}


@app.post("/terminate_vm")
async def terminate_vm(req: VmActionRequest, user: dict = Depends(verify_token)):
    user_id = user.get("id")
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid user payload (missing id)")

    inst = aws_manager.find_user_instance(user_id)
    if not inst:
        raise HTTPException(status_code=404, detail="No VM found for this user.")

    vm_id = req.vm_id or inst["InstanceId"]
    ok = aws_manager.terminate_vm(vm_id)
    if not ok:
        raise HTTPException(status_code=500, detail="Failed to terminate VM.")

    return {"message": f"Terminated VM {vm_id}. Data is permanently lost."}


# =========================
# ✅ Allocate / Resume logic
# =========================
@app.post("/allocate")
@app.post("/allocate/")
async def allocate_ram(request: RamRequest, user: dict = Depends(verify_token)):
    user_id = user.get("id")
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid user payload (missing id)")

    existing = aws_manager.find_user_instance(user_id)
    if existing:
        vm_id = existing["InstanceId"]
        state = existing["State"]["Name"]
        ip = existing.get("PublicIpAddress")

        if state in ("running", "pending") and ip:
            return {"vm_id": vm_id, "ip": ip, "state": state}

        if state in ("stopped", "stopping"):
            return {
                "vm_id": vm_id,
                "ip": ip,
                "state": state,
                "action_required": True,
                "message": "Existing VM is stopped. Choose Resume or Create New.",
            }

    try:
        vm_id, ip_address = aws_manager.create_vm(request.ram_size, user_id=user_id)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to allocate RAM: {str(e)}")

    if not vm_id or not ip_address:
        raise HTTPException(status_code=500, detail="Failed to allocate RAM (no vm_id/ip).")

    return {"vm_id": vm_id, "ip": ip_address, "state": "running"}


# =========================
# ✅ VM RAM usage (from VM)
# =========================
@app.get("/ram_usage")
@app.get("/ram_usage/")
async def ram_usage(vm_ip: str, user: dict = Depends(verify_token)):
    if not vm_ip:
        raise HTTPException(status_code=400, detail="VM IP is required")

    # VM exposes /ram_usage (Flask)
    resp = _vm_get(vm_ip, "/ram_usage", timeout=12)
    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail=f"VM ram_usage failed: {resp.status_code} {resp.text}")

    data = resp.json()
    return {
        "total_ram": data.get("total_ram", 0),
        "used_ram": data.get("used_ram", 0),
        "available_ram": data.get("available_ram", 0),
        "percent_used": data.get("percent_used", 0),
    }


# =========================
# ✅ VM Task Runner (PUBLIC SAFE)
# =========================
@app.post("/vm/run_task")
async def vm_run_task(req: VmRunTaskRequest, user: dict = Depends(verify_token)):
    if not req.vm_ip:
        raise HTTPException(status_code=400, detail="vm_ip is required")
    if not req.task:
        raise HTTPException(status_code=400, detail="task is required")

    # NOTE: Your VM currently supports /run_task only for notepad++.exe.
    resp = _vm_post(req.vm_ip, "/run_task", {"task": req.task}, timeout=45)
    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail=f"VM run_task failed: {resp.status_code} {resp.text}")
    return resp.json()


@app.post("/vm/sync_notepad")
async def vm_sync_notepad(req: VmRunTaskRequest, user: dict = Depends(verify_token)):
    if not req.vm_ip:
        raise HTTPException(status_code=400, detail="vm_ip is required")

    resp = _vm_post(req.vm_ip, "/sync_notepad_files", {}, timeout=60)
    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail=f"VM sync_notepad_files failed: {resp.status_code} {resp.text}")
    return resp.json()


@app.post("/vm/migrate_tasks")
async def vm_migrate_tasks(req: VmMigrateTasksRequest, user: dict = Depends(verify_token)):
    """
    Public-safe replacement for old /migrate_tasks/.
    This does NOT touch the user's local PC.
    It only asks the VM to start tasks (if VM supports them).
    """
    if not req.vm_ip:
        raise HTTPException(status_code=400, detail="vm_ip is required")
    if not req.task_names:
        raise HTTPException(status_code=400, detail="task_names is required")

    results = []
    for task in req.task_names:
        try:
            r = _vm_post(req.vm_ip, "/run_task", {"task": task}, timeout=45)
            ok = (r.status_code == 200)
            results.append({"task": task, "success": ok, "detail": None if ok else r.text})
        except HTTPException as e:
            results.append({"task": task, "success": False, "detail": str(e.detail)})

    return {"results": results}


# =========================
# ✅ VSCode setup on VM (PUBLIC SAFE)
# =========================
@app.post("/vscode/setup_on_vm")
async def vscode_setup_on_vm(req: SetupVSCodeOnVmRequest, user: dict = Depends(verify_token)):
    """
    The LOCAL AGENT should create the zips and upload to S3.
    This backend endpoint ONLY tells the VM to pull from S3 and open VSCode.
    """
    if not req.vm_ip:
        raise HTTPException(status_code=400, detail="vm_ip is required")

    payload = {
        "user_id": req.user_id,
        "project_name": req.project_name,
        "project_s3_bucket": req.project_s3_bucket,
        "project_s3_key": req.project_s3_key,
        "config_s3_bucket": req.config_s3_bucket,
        "config_s3_key": req.config_s3_key,
        "opened_path_kind": req.opened_path_kind,
        "deps_s3_bucket": req.deps_s3_bucket,
        "deps_s3_key": req.deps_s3_key,
        "deps_meta_s3_key": req.deps_meta_s3_key,
    }

    start = _vm_post(req.vm_ip, "/setup_vscode", payload, timeout=30)
    if start.status_code != 200:
        raise HTTPException(status_code=502, detail=f"VM setup_vscode failed: {start.status_code} {start.text}")

    job_id = start.json().get("job_id")
    if not job_id:
        raise HTTPException(status_code=502, detail="VM did not return job_id")

    # Poll status (best effort)
    for _ in range(60):  # ~5 minutes
        st = _vm_get(req.vm_ip, f"/vscode_setup_status/{job_id}", timeout=10)
        if st.status_code == 200:
            j = st.json()
            if j.get("status") == "done":
                return {"ok": True, "job_id": job_id, "status": j}
            if j.get("status") == "error":
                raise HTTPException(status_code=500, detail=f"VM VSCode setup error: {j.get('message')}")
        time.sleep(5)

    return {"ok": False, "job_id": job_id, "message": "Timed out waiting for VM to finish VSCode setup."}


if __name__ == "__main__":
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
