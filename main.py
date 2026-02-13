from fastapi import FastAPI, HTTPException, Depends
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from aws_manager import AWSManager
from process_manager import ProcessManager

import uvicorn
import sys
import os
import requests
import time

sys.path.append(os.path.dirname(os.path.abspath(__file__)))

app = FastAPI()
aws_manager = AWSManager()
process_manager = ProcessManager()

# -------------------------
# CORS (configurable)
# -------------------------
CORS_ORIGINS = os.getenv(
    "CORS_ORIGINS",
    "http://localhost:5000,http://127.0.0.1:5000"
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

class RamRequest(BaseModel):
    ram_size: int

class TaskRequest(BaseModel):
    task_name: str
    vm_ip: str

class MigrateTasksRequest(BaseModel):
    task_names: list[str]
    vm_ip: str

class VmActionRequest(BaseModel):
    vm_id: str | None = None

class BeaconStopRequest(BaseModel):
    vm_id: str
    access_token: str

class MigrateVSCodeRequest(BaseModel):
    vm_ip: str

class SaveProjectRequest(BaseModel):
    vm_ip: str
    project_name: str

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
# ✅ STOP VM (reliable button)
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
    state = inst["State"]["Name"]
    print(f"🟡 STOP REQUEST (button) user={user_id} vm_id={vm_id} state={state}")

    ok = aws_manager.stop_vm(vm_id)
    if not ok:
        raise HTTPException(status_code=500, detail="Failed to stop VM.")
    return {"message": f"Stopping VM {vm_id}."}

# =========================
# ✅ STOP VM (beacon - best effort)
# =========================
@app.post("/stop_vm_beacon")
async def stop_vm_beacon(req: BeaconStopRequest):
    print("📩 stop_vm_beacon HIT (raw)")

    user = verify_token_raw(req.access_token)
    user_id = user.get("id")
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid user payload (missing id)")

    inst = aws_manager.find_user_instance(user_id)
    if not inst:
        return {"message": "No VM found for this user."}

    vm_id = req.vm_id or inst["InstanceId"]
    state = inst["State"]["Name"]
    print(f"🟡 STOP REQUEST (beacon) user={user_id} vm_id={vm_id} state={state}")

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

@app.get("/ram_usage")
@app.get("/ram_usage/")
async def ram_usage(vm_ip: str, user: dict = Depends(verify_token)):
    if not vm_ip:
        raise HTTPException(status_code=400, detail="VM IP is required")

    ram_info = aws_manager.get_vm_status(vm_ip)
    if "error" in ram_info:
        raise HTTPException(status_code=500, detail=ram_info["error"])

    return {
        "total_ram": ram_info.get("total_ram", 0),
        "used_ram": ram_info.get("used_ram", 0),
        "available_ram": ram_info.get("available_ram", 0),
        "percent_used": ram_info.get("percent_used", 0),
    }

@app.get("/running_tasks/")
async def running_tasks():
    return process_manager.get_local_tasks()

@app.post("/migrate_tasks/")
async def migrate_tasks(request: MigrateTasksRequest, user: dict = Depends(verify_token)):
    results = []
    for task_name in request.task_names:
        success = process_manager.move_task_to_cloud(
            task_name,
            request.vm_ip,
            sync_state=(task_name == "notepad++.exe"),
        )
        results.append({"task": task_name, "success": success})
    return {"results": results}

@app.post("/sync_notepad/")
async def sync_notepad(request: TaskRequest, user: dict = Depends(verify_token)):
    process_manager.tracked_files = process_manager.get_current_open_files()
    process_manager.sync_notepad_files(request.vm_ip)
    return {"message": "Synced Notepad++ files"}

@app.post("/migrate_vscode/")
async def migrate_vscode(req: MigrateVSCodeRequest, user: dict = Depends(verify_token)):
    user_id = user.get("id")
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid user payload (missing id)")

    ok, opened_path, err = process_manager.migrate_vscode_project(vm_ip=req.vm_ip, user_id=user_id)
    if not ok:
        raise HTTPException(status_code=500, detail=err or "VSCode migration failed")

    return {"message": "VSCode migrated", "opened_path": opened_path}

@app.post("/save_project_to_local")
async def save_project_to_local(req: SaveProjectRequest, user: dict = Depends(verify_token)):
    user_id = user.get("id")
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid user payload (missing id)")

    local_base = os.getenv("LOCAL_PROJECTS_BASE", r"E:\Kotesh\Projects")

    ok, msg = process_manager.save_project_from_vm_to_local(
        vm_ip=req.vm_ip,
        user_id=user_id,
        project_name=req.project_name,
        local_base=local_base
    )
    if not ok:
        raise HTTPException(status_code=500, detail=msg)

    return {"message": msg}

if __name__ == "__main__":
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
