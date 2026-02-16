# backend/main.py (PUBLIC-SAFE)
# ✅ Render backend should NOT use local Windows paths, psutil, win32*, or touch user's PC.
# ✅ This file keeps: Auth (Supabase), AWS VM lifecycle, and VM orchestration calls.
# ✅ Adds: pre-signed S3 URLs so Local Agent never needs AWS creds.

from fastapi import FastAPI, HTTPException, Depends
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from aws_manager import AWSManager

import uvicorn
import os
import requests
import boto3
from typing import Optional, Dict, Any


app = FastAPI()
aws_manager = AWSManager()

# -------------------------
# CORS (configurable)
# -------------------------
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
VM_API_KEY = os.getenv("VM_API_KEY", "")

# -------------------------
# S3 Presign config
# -------------------------
S3_PRESIGN_EXPIRES_SECONDS = int(os.getenv("S3_PRESIGN_EXPIRES_SECONDS", "300"))

# Comma list allowlist. Example:
# ALLOWED_S3_BUCKETS="notepadfiles,cloudram-vscode"
ALLOWED_S3_BUCKETS = [
    b.strip()
    for b in os.getenv("ALLOWED_S3_BUCKETS", "notepadfiles,cloudram-vscode").split(",")
    if b.strip()
]

# ✅ Content-Type allowlist for presigned PUT (security)
# You hit: "application/json not allowed" for deps_hint.json
ALLOWED_PRESIGN_CONTENT_TYPES = [
    ct.strip()
    for ct in os.getenv(
        "ALLOWED_PRESIGN_CONTENT_TYPES",
        "application/octet-stream,application/zip,text/plain,application/json",
    ).split(",")
    if ct.strip()
]

s3_client = boto3.client("s3")


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


class S3SignPutRequest(BaseModel):
    user_id: str
    bucket: str
    key: str
    content_type: str = "application/octet-stream"


class S3SignGetRequest(BaseModel):
    user_id: str
    bucket: str
    key: str


# -------------------------
# Helpers: talk to VM
# -------------------------
def _vm_headers() -> Dict[str, str]:
    headers: Dict[str, str] = {}
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
# Helpers: S3 presign safety
# -------------------------
def _require_user_scoped_key(user_id: str, key: str):
    expected_prefix = f"users/{user_id}/"
    if not key or not key.startswith(expected_prefix):
        raise HTTPException(
            status_code=403,
            detail=f"Invalid key scope. Key must start with '{expected_prefix}'",
        )


def _require_allowed_bucket(bucket: str):
    if bucket not in ALLOWED_S3_BUCKETS:
        raise HTTPException(
            status_code=403,
            detail=f"Bucket not allowed. Allowed: {', '.join(ALLOWED_S3_BUCKETS)}",
        )


def _require_allowed_content_type(content_type: str):
    ct = (content_type or "").strip()
    if ct not in ALLOWED_PRESIGN_CONTENT_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"content_type not allowed. Allowed: {', '.join(ALLOWED_PRESIGN_CONTENT_TYPES)}",
        )


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
# ✅ S3 Presigned URLs (for Local Agent)
#   Local Agent sends SB token to backend, backend signs per-user object URL.
# =========================
@app.post("/s3/sign_put")
async def s3_sign_put(req: S3SignPutRequest, user: dict = Depends(verify_token)):
    token_user_id = user.get("id")
    if not token_user_id:
        raise HTTPException(status_code=401, detail="Invalid user payload (missing id)")

    if req.user_id != token_user_id:
        raise HTTPException(status_code=403, detail="user_id mismatch")

    _require_allowed_bucket(req.bucket)
    _require_user_scoped_key(req.user_id, req.key)
    _require_allowed_content_type(req.content_type)

    try:
        url = s3_client.generate_presigned_url(
            ClientMethod="put_object",
            Params={
                "Bucket": req.bucket,
                "Key": req.key,
                "ContentType": req.content_type,
            },
            ExpiresIn=S3_PRESIGN_EXPIRES_SECONDS,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to presign PUT URL: {str(e)}")

    return {
        "url": url,
        "bucket": req.bucket,
        "key": req.key,
        "expires_in": S3_PRESIGN_EXPIRES_SECONDS,
    }


@app.post("/s3/sign_get")
async def s3_sign_get(req: S3SignGetRequest, user: dict = Depends(verify_token)):
    token_user_id = user.get("id")
    if not token_user_id:
        raise HTTPException(status_code=401, detail="Invalid user payload (missing id)")

    if req.user_id != token_user_id:
        raise HTTPException(status_code=403, detail="user_id mismatch")

    _require_allowed_bucket(req.bucket)
    _require_user_scoped_key(req.user_id, req.key)

    try:
        url = s3_client.generate_presigned_url(
            ClientMethod="get_object",
            Params={"Bucket": req.bucket, "Key": req.key},
            ExpiresIn=S3_PRESIGN_EXPIRES_SECONDS,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to presign GET URL: {str(e)}")

    return {"url": url, "expires_in": S3_PRESIGN_EXPIRES_SECONDS}


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


if __name__ == "__main__":
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
