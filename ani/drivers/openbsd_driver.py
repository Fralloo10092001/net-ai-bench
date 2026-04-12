import json
import subprocess
from pathlib import Path

from .base import BaseDriver

ALLOWED_PREFIXES = [
    "ifconfig ",
    "route add ",
    "route delete ",
    "route -n show",
    "netstat -rn",
    "sysctl net.inet.ip.forwarding",
    "sysctl -w net.inet.ip.forwarding=",
    "ping ",
    "ping6 ",
    "hostname",
    "uname ",
]

BLOCKED_SUBSTRINGS = [
    "rm ",
    "reboot",
    "shutdown",
    "poweroff",
    "mkfs",
    "> /etc/",
    ">> /etc/",
    "chmod ",
    "chown ",
    "pkg_add",
    "pkg_delete",
    "curl ",
    "wget ",
    "nc -e",
    "bash -c",
]

FORBIDDEN_TOKENS = ["&&", "||", ";"]


class OpenBSDDriver(BaseDriver):
    def __init__(self, lab_name: str, log_file: Path):
        self.lab_name = lab_name
        self.log_file = log_file

    def _clab(self, node: str) -> str:
        return f"clab-{self.lab_name}-{node}"

    def _container_exists(self, container_name: str) -> bool:
        result = subprocess.run(
            ["docker", "ps", "--format", "{{.Names}}"],
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
        return container_name in result.stdout.splitlines()

    def _exec(self, node: str, command: str, check: bool = True) -> str:
        container_name = self._clab(node)

        if not self._container_exists(container_name):
            raise RuntimeError(f"Container not found: {container_name}")

        result = subprocess.run(
            ["docker", "exec", container_name, "sh", "-c", command],
            capture_output=True,
            text=True,
            check=check,
            timeout=10,
        )

        stdout = result.stdout.strip()
        stderr = result.stderr.strip()

        if stderr and not stdout:
            return stderr
        if stdout and stderr:
            return f"{stdout}\n{stderr}"
        return stdout

    def _is_allowed_command(self, cmd: str) -> bool:
        cmd = cmd.strip()

        if not cmd:
            return False

        if any(tok in cmd for tok in FORBIDDEN_TOKENS):
            return False

        if any(bad in cmd for bad in BLOCKED_SUBSTRINGS):
            return False

        if cmd.startswith("ping ") or cmd.startswith("ping6 "):
            if "-c" not in cmd:
                return False

        return any(cmd.startswith(prefix) for prefix in ALLOWED_PREFIXES)

    def get_running_config(self, device: str) -> dict:
        ifconfig_out = self._exec(device, "ifconfig")
        route_out = self._exec(device, "netstat -rn")
        fwd_out = self._exec(device, "sysctl net.inet.ip.forwarding")

        config = (
            f"=== ifconfig ===\n{ifconfig_out}\n"
            f"=== routes ===\n{route_out}\n"
            f"=== ip_forward ===\n{fwd_out}"
        )
        return {"running_config": config}

    def update_config(self, device: str, commands: list[str]) -> dict:
        results = []

        for cmd in commands:
            cmd = cmd.strip()

            if not self._is_allowed_command(cmd):
                results.append({
                    "command": cmd,
                    "status": "blocked",
                    "output": "command not allowed",
                })
                continue

            with open(self.log_file, "a") as f:
                f.write(json.dumps({"device": device, "command": cmd}) + "\n")

            try:
                out = self._exec(device, cmd)
                results.append({
                    "command": cmd,
                    "status": "success",
                    "output": out,
                })
            except subprocess.TimeoutExpired:
                results.append({
                    "command": cmd,
                    "status": "error",
                    "output": "command timed out",
                })
            except subprocess.CalledProcessError as e:
                stderr = (e.stderr or "").strip()
                stdout = (e.stdout or "").strip()
                output = stderr or stdout or str(e)
                results.append({
                    "command": cmd,
                    "status": "error",
                    "output": output,
                })
            except Exception as e:
                results.append({
                    "command": cmd,
                    "status": "error",
                    "output": str(e),
                })

        return {"results": results}

    def execute_validation(self, device: str, command: str) -> dict:
        try:
            out = self._exec(device, command, check=False)
            return {"output": out}
        except subprocess.TimeoutExpired:
            return {"output": "validation command timed out"}
        except Exception as e:
            return {"output": str(e)}