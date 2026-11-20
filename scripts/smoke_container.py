"""Exercise the real image with synthetic credentials and disposable tmpfs only."""

import subprocess
import sys
import uuid

image = sys.argv[1] if len(sys.argv) > 1 else "xunji-mcp:test"
name = "xunji-smoke-" + uuid.uuid4().hex[:8]
env = {
    "DATA_DIR": "/data",
    "BASE_URL": "https://xunji.example.com",
    "ALLOWED_GITHUB_LOGIN": "test-owner",
    "ALLOWED_GITHUB_ID": "1",
    "GITHUB_CLIENT_ID": "test-client",
    "GITHUB_CLIENT_SECRET": "synthetic-only",
    "JWT_SIGNING_KEY": "synthetic-signing-only-" * 3,
    "STORAGE_ENCRYPTION_SECRET": "synthetic-storage-only-" * 3,
}
env.update(
    {f"XUNJI_{key}_KEY": "synthetic-only" for key in ("TRAIN", "FOOD", "FOOD_SEARCH", "BODY", "TEMPLATE")}
)
command = [
    "docker",
    "run",
    "--detach",
    "--name",
    name,
    "--memory",
    "256m",
    "--cpus",
    "1",
    "--read-only",
    "--cap-drop",
    "ALL",
    "--security-opt",
    "no-new-privileges:true",
    "--tmpfs",
    "/tmp:rw,nosuid,nodev,size=64m",
    "--tmpfs",
    "/data:rw,nosuid,nodev,size=16m,uid=10001,gid=10001",
]
for key, value in env.items():
    command += ["--env", f"{key}={value}"]
command.append(image)
probe = """
import time, urllib.request, urllib.error
for i in range(30):
    try:
        assert urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=1).read() == b'ok'
        break
    except (OSError, AssertionError):
        time.sleep(.5)
else:
    raise RuntimeError('Health check failed')
try:
    urllib.request.urlopen(urllib.request.Request('http://127.0.0.1:8000/mcp', data=b'{}', headers={'Content-Type': 'application/json'}))
    raise RuntimeError('Anonymous MCP access was accepted')
except urllib.error.HTTPError as e:
    assert e.code == 401, e.code
    assert 'resource_metadata' in e.headers['WWW-Authenticate']
assert urllib.request.urlopen('http://127.0.0.1:8000/.well-known/oauth-authorization-server').status == 200
print('health=200 anonymous_mcp=401 oauth_metadata=200')
"""
try:
    subprocess.run(command, check=True, capture_output=True)
    subprocess.run(["docker", "exec", name, "python", "-c", probe], check=True)
    subprocess.run(
        ["docker", "stats", "--no-stream", "--format", "{{.Name}} {{.MemUsage}}", name], check=True
    )
finally:
    subprocess.run(["docker", "logs", name], check=False)
    subprocess.run(["docker", "rm", "--force", name], check=False, capture_output=True)
