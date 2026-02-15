import boto3
import time
import os
import requests
from requests.adapters import HTTPAdapter
import botocore

try:
    from urllib3.util.retry import Retry
except ImportError:
    print("⚠️ urllib3.util.retry not found. Installing urllib3 explicitly might be required.")
    Retry = None


class AWSManager:
    """
    CloudRAMSaaS AWS manager.

    Key rule:
    - One VM per user, persisted via EC2 Tag:
        Key: cloudramsaas_user_id
        Value: <supabase user id>
    """

    USER_TAG_KEY = "cloudramsaas_user_id"
    APP_TAG_KEY = "app"
    APP_TAG_VALUE = "CloudRAMSaaS"

    def __init__(self):
        """Initialize AWS EC2 client and resource manager."""
        self.ec2 = boto3.client("ec2")
        self.ec2_resource = boto3.resource("ec2")
        self.s3 = boto3.client("s3")
        self.bucket_name = os.getenv("CLOUDRAM_SCRIPTS_BUCKET", "cloud-ram-scripts")

    # -------------------------
    # Key Pair / SG / AMI
    # -------------------------
    def create_key_pair(self):
        key_name = "cloud-ram-key"

        try:
            self.ec2.describe_key_pairs(KeyNames=[key_name])
            print(f"✅ Key Pair {key_name} exists in AWS.")
            return key_name, None
        except botocore.exceptions.ClientError as e:
            print("❌ Key pair not found in AWS:", e)
            return None, None

    def create_security_group(self):
        """Dynamically creates a security group for the VM."""
        sg_name = os.getenv("CLOUDRAM_SG_NAME", "cloud-ram-sg")
        try:
            existing_sgs = self.ec2.describe_security_groups()["SecurityGroups"]
            for sg in existing_sgs:
                if sg["GroupName"] == sg_name:
                    print(f"✅ Security Group {sg_name} already exists.")
                    return sg["GroupId"]

            response = self.ec2.create_security_group(
                GroupName=sg_name,
                Description="Security group for Cloud RAM SaaS VMs"
            )
            sg_id = response["GroupId"]

            self.ec2.authorize_security_group_ingress(
                GroupId=sg_id,
                IpPermissions=[
                    {"IpProtocol": "tcp", "FromPort": 3389, "ToPort": 3389, "IpRanges": [{"CidrIp": "0.0.0.0/0"}]},
                    {"IpProtocol": "tcp", "FromPort": 5000, "ToPort": 5000, "IpRanges": [{"CidrIp": "0.0.0.0/0"}]},
                    {"IpProtocol": "tcp", "FromPort": 80, "ToPort": 80, "IpRanges": [{"CidrIp": "0.0.0.0/0"}]},
                    {"IpProtocol": "tcp", "FromPort": 443, "ToPort": 443, "IpRanges": [{"CidrIp": "0.0.0.0/0"}]},
                    {"IpProtocol": "tcp", "FromPort": 8080, "ToPort": 8080, "IpRanges": [{"CidrIp": "0.0.0.0/0"}]},
                    {"IpProtocol": "tcp", "FromPort": 5900, "ToPort": 5900, "IpRanges": [{"CidrIp": "0.0.0.0/0"}]},
                    {"IpProtocol": "tcp", "FromPort": 6080, "ToPort": 6080, "IpRanges": [{"CidrIp": "0.0.0.0/0"}]},
                ]
            )
            print(f"🛡 New Security Group Created: {sg_name} ({sg_id})")
            return sg_id
        except Exception as e:
            print(f"❌ Error creating security group: {str(e)}")
            return None

    def get_latest_windows_ami(self):
        """Finds the latest Windows Server AMI dynamically."""
        try:
            response = self.ec2.describe_images(
                Filters=[
                    {"Name": "platform", "Values": ["windows"]},
                    {"Name": "name", "Values": ["Windows_Server-2022-English-Full-Base*"]}
                ],
                Owners=["amazon"]
            )
            if not response["Images"]:
                print("❌ No Windows Server AMI found.")
                return None
            ami_id = sorted(response["Images"], key=lambda x: x["CreationDate"], reverse=True)[0]["ImageId"]
            print(f"📦 Latest Windows AMI Found: {ami_id}")
            return ami_id
        except Exception as e:
            print(f"❌ Error fetching Windows AMI: {str(e)}")
            return None

    # -------------------------
    # S3 VM script upload
    # -------------------------
    def upload_script_to_s3(self):
        script_path = os.path.join("vm_scripts", "vm_server.py")
        script_key = "vm_server.py"

        if not os.path.exists(script_path):
            print(f"❌ Flask Server script not found at {script_path}")
            return None

        try:
            self.s3.upload_file(script_path, self.bucket_name, script_key)
            print(f"📤 Uploaded {script_path} to s3://{self.bucket_name}/{script_key}")
            return True
        except Exception as e:
            print(f"❌ Error uploading script to S3: {str(e)}")
            return None

    # -------------------------
    # Per-user instance lookup
    # -------------------------
    def find_user_instance(self, user_id: str):
        """
        Find the user's instance by tag.
        Returns the newest instance (LaunchTime) among pending/running/stopping/stopped.
        """
        if not user_id:
            return None

        try:
            resp = self.ec2.describe_instances(
                Filters=[
                    {"Name": f"tag:{self.USER_TAG_KEY}", "Values": [user_id]},
                    {"Name": "instance-state-name", "Values": ["pending", "running", "stopping", "stopped"]},
                ]
            )

            instances = []
            for r in resp.get("Reservations", []):
                instances.extend(r.get("Instances", []))

            if not instances:
                return None

            instances.sort(key=lambda x: x["LaunchTime"], reverse=True)
            return instances[0]
        except Exception as e:
            print(f"❌ Error finding user instance: {str(e)}")
            return None

    def get_instance_state_and_ip(self, vm_id: str):
        try:
            resp = self.ec2.describe_instances(InstanceIds=[vm_id])
            inst = resp["Reservations"][0]["Instances"][0]
            state = inst["State"]["Name"]
            ip = inst.get("PublicIpAddress")
            return state, ip
        except Exception as e:
            print(f"❌ Error describing instance {vm_id}: {e}")
            return None, None

    def wait_for_running_and_ip(self, vm_id: str, timeout=240):
        start = time.time()
        while time.time() - start < timeout:
            state, ip = self.get_instance_state_and_ip(vm_id)
            if state == "running" and ip:
                return ip
            time.sleep(3)
        raise RuntimeError("Timed out waiting for instance to be running with a public IP.")

    def stop_vm(self, vm_id: str):
        try:
            self.ec2.stop_instances(InstanceIds=[vm_id])
            print(f"🟡 Stop requested for VM {vm_id}")
            return True
        except Exception as e:
            print(f"❌ Error stopping VM {vm_id}: {e}")
            return False

    def start_vm(self, vm_id: str):
        try:
            self.ec2.start_instances(InstanceIds=[vm_id])
            print(f"🟢 Start requested for VM {vm_id}")
            return True
        except Exception as e:
            print(f"❌ Error starting VM {vm_id}: {e}")
            return False

    def terminate_vm(self, vm_id):
        """Terminates the EC2 instance."""
        try:
            self.ec2.terminate_instances(InstanceIds=[vm_id])
            print(f"🛑 VM {vm_id} Terminated.")
            return True
        except Exception as e:
            print(f"❌ Error terminating VM: {str(e)}")
            return False

    # -------------------------
    # VM readiness checks
    # -------------------------
    def wait_for_vm_services(self, ip_address: str, max_attempts=180):
        """
        Wait for Flask server on VM (port 5000) to become ready.
        Uses retry strategy; times out after ~30 minutes (180 * 10 sec).
        """
        session = requests.Session()
        if Retry:
            retry_strategy = Retry(total=10, backoff_factor=5, status_forcelist=[500, 502, 503, 504])
            adapter = HTTPAdapter(max_retries=retry_strategy)
            session.mount("http://", adapter)

        print(f"⏳ Waiting for Flask server at {ip_address}:5000...")
        for attempt in range(max_attempts):
            try:
                r = session.get(f"http://{ip_address}:5000/", timeout=20)
                if r.status_code == 200:
                    print(f"✅ Flask server ready at {ip_address}:5000 after {attempt + 1} attempts")
                    return True
            except requests.RequestException as e:
                print(f"⏳ Attempt {attempt + 1}/{max_attempts}: Waiting for Flask... ({str(e)})")
                time.sleep(10)

        print(f"❌ Flask server not ready after waiting at {ip_address}:5000")
        return False

    # -------------------------
    # Create VM (per user)
    # -------------------------
    def create_vm(self, ram_size: int, user_id: str):
        r"""
        Create a Windows VM for user_id.
        Uses ONE shared EC2 keypair name, and downloads the PEM from S3 inside the VM via startup script.

        Env:
        CLOUDRAM_IAM_INSTANCE_PROFILE (default: CloudRAMEC2Role)
        CLOUDRAM_KEY_BUCKET          (default: cloud-ram-secrets)
        CLOUDRAM_KEY_OBJECT          (default: keys/cloud-ram-key.pem)
        """
        # upload your vm_server.py (or other scripts) first
        self.upload_script_to_s3()

        key_name, _ = self.create_key_pair()
        if not key_name:
            print("❌ Failed to create or retrieve key pair.")
            return None, None

        instance_type = {1: "t3.micro", 2: "t3.small", 4: "t3.medium"}.get(ram_size, "t3.medium")

        startup_script_path = os.path.join("vm_scripts", "vm_startup_script.ps1")
        if not os.path.exists(startup_script_path):
            print(f"❌ Startup script not found at {startup_script_path}")
            return None, None

        with open(startup_script_path, "r", encoding="utf-8") as f:
            startup_script = f.read()

        key_bucket = os.getenv("CLOUDRAM_KEY_BUCKET", "cloud-ram-secrets")
        key_object = os.getenv("CLOUDRAM_KEY_OBJECT", "keys/cloud-ram-key.pem")

        # IMPORTANT: vm_startup_script.ps1 should ensure AWS CLI exists before "aws s3 cp" runs.
        # (I showed that snippet earlier—add it near the top of vm_startup_script.ps1)
        user_data = (
            f"{startup_script}\n\n"
            "New-Item -ItemType Directory -Path 'C:\\CloudRAM' -Force\n"
            f"aws s3 cp s3://{key_bucket}/{key_object} C:\\CloudRAM\\cloud-ram-key.pem\n"
            "icacls 'C:\\CloudRAM\\cloud-ram-key.pem' /inheritance:r /grant:r 'Administrators:F'\n"
        )

        try:
            ami = self.get_latest_windows_ami()
            if not ami:
                return None, None

            sg_id = self.create_security_group()
            if not sg_id:
                return None, None

            profile_name = os.getenv("CLOUDRAM_IAM_INSTANCE_PROFILE", "CloudRAMEC2Role")

            print(f"🚀 Creating EC2 instance for user={user_id} with {ram_size}GB RAM ({instance_type})")
            response = self.ec2.run_instances(
                ImageId=ami,
                InstanceType=instance_type,
                MinCount=1,
                MaxCount=1,
                KeyName=key_name,
                SecurityGroupIds=[sg_id],
                UserData=user_data,
                IamInstanceProfile={"Name": profile_name},
                TagSpecifications=[
                    {
                        "ResourceType": "instance",
                        "Tags": [
                            {"Key": self.USER_TAG_KEY, "Value": user_id},
                            {"Key": self.APP_TAG_KEY, "Value": self.APP_TAG_VALUE},
                        ],
                    }
                ],
            )

            instance = response["Instances"][0]
            vm_id = instance["InstanceId"]

            print("⏳ Waiting for instance to start...")
            waiter = self.ec2.get_waiter("instance_running")
            waiter.wait(InstanceIds=[vm_id])

            ip_address = self.wait_for_running_and_ip(vm_id)
            print(f"✅ Instance running at {ip_address}. Waiting for services...")

            ok = self.wait_for_vm_services(ip_address)
            if not ok:
                self.terminate_vm(vm_id)
                return None, None

            print(f"✅ VM Created: ID={vm_id}, IP={ip_address}")
            return vm_id, ip_address

        except Exception as e:
            print(f"❌ Error creating VM: {str(e)}")
            return None, None

    # -------------------------
    # Existing methods (kept)
    # -------------------------
    def get_vm_status(self, vm_ip):
        """Check VM's running processes and resource usage."""
        try:
            url = f"http://{vm_ip}:5000/ram_usage"
            response = requests.get(url, timeout=10)
            if response.status_code != 200:
                return {"error": f"Failed to fetch status, status code: {response.status_code}"}
            return response.json()
        except requests.RequestException as e:
            print(f"❌ Error fetching VM status: {str(e)}")
            return {"error": str(e)}

    def install_application_on_vm(self, vm_ip, app_name):
        """Dynamically install an application on the VM if not present."""
        try:
            response = requests.get(f"http://{vm_ip}:5000/list_tasks", timeout=10)
            if response.status_code == 200:
                tasks = response.json().get("tasks", [])
                if any(task["name"].lower() == app_name.lower() for task in tasks):
                    print(f"✅ {app_name} already running on VM {vm_ip}")
                    return True

            session = requests.Session()
            if Retry:
                retry_strategy = Retry(total=3, backoff_factor=2, status_forcelist=[500, 502, 503, 504])
                adapter = HTTPAdapter(max_retries=retry_strategy)
                session.mount("http://", adapter)

            install_payload = {"app_name": app_name}
            print(f"⏳ Attempting to install {app_name} on VM {vm_ip}")
            response = session.post(f"http://{vm_ip}:5000/install_app", json=install_payload, timeout=120)
            if response.status_code == 200:
                print(f"✅ Successfully installed {app_name} on VM {vm_ip}")
                return True
            else:
                print(f"❌ Failed to install {app_name}: {response.text}")
                return False

        except requests.Timeout as e:
            print(f"❌ Timeout installing {app_name} on VM {vm_ip}: {str(e)}")
            return False
        except requests.RequestException as e:
            print(f"❌ Error installing {app_name} on VM {vm_ip}: {str(e)}")
            return False
        except Exception as e:
            print(f"❌ Unexpected error installing {app_name} on VM {vm_ip}: {str(e)}")
            return False

    def migrate_task_with_ui(self, vm_ip, task_name):
        """Migrate a task and return the VNC URL for UI streaming."""
        try:
            if not self.install_application_on_vm(vm_ip, task_name):
                print(f"❌ Failed to install {task_name} on VM {vm_ip}")
                return None

            print(f"⏳ Migrating {task_name} with UI streaming to VM {vm_ip}")
            response = requests.post(
                f"http://{vm_ip}:5000/migrate_task_with_ui",
                json={"task_name": task_name, "task_data": {"state": "auto_migrated"}},
                timeout=60
            )

            if response.status_code == 200:
                response_data = response.json()
                stream_url = response_data.get("web_vnc_url", f"http://{vm_ip}:8080/vnc.html")
                vnc_direct = response_data.get("vnc_url", f"vnc://{vm_ip}:5900")

                print(f"✅ Task {task_name} migrated with UI streaming")
                print(f"Web VNC: {stream_url}")
                print(f"Direct VNC: {vnc_direct}")

                return stream_url
            else:
                print(f"❌ Failed to migrate {task_name}: {response.text}")
                return None
        except Exception as e:
            print(f"❌ Error migrating {task_name} with UI: {str(e)}")
            return None


if __name__ == "__main__":
    manager = AWSManager()
    # NOTE: for local testing, use a placeholder user_id (real flow passes Supabase user id)
    vm_id, ip = manager.create_vm(2, user_id="local-test-user")
    if vm_id:
        print(f"VM ID: {vm_id}, IP: {ip}")
