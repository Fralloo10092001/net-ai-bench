import json
import subprocess
from pathlib import Path

from torch import cat

from .base import BaseDriver

#Lista di prefissi ammessi
ALLOWED_PREFIXES = [
    "ip addr add",
    "ip addr del",
    "ip addr show",
    "ip link set",
    "ip link show",
    "ip route add",
    "ip route del",
    "ip route show",
    "ip rule add",
    "ip rule del",
    "ip rule show",
    "sysctl -w net.ipv4.ip_forward=",
    "cat /proc/sys/net/ipv4/ip_forward",
    "echo 1 > /proc/sys/net/ipv4/ip_forward",
    "echo 0 > /proc/sys/net/ipv4/ip_forward",
    "ping ",
    "ping6 ",
    "traceroute ",
    "tracepath ",
    "ss ",
    "netstat ",
    "arp ",
    "ip neigh",
    "ip -br addr",
    "ip -br link",
    "hostname",
    "uname ",
]

#Lista di prefissi non ammessi
BLOCKED_SUBSTRINGS = [
    "rm ",
    "reboot",
    "shutdown",
    "poweroff",
    "mkfs",
    "iptables -F",
    "nft flush",
    "> /etc/",
    ">> /etc/",
    "chmod ",
    "chown ",
    "apt ",
    "yum ",
    "dnf ",
    "apk ",
    "curl ",
    "wget ",
    "nc -e",
    "bash -c",
]

FORBIDDEN_TOKENS = ["&&", "||", ";"]


class LinuxDriver(BaseDriver):

    #Salva nome lab e file di log
    def __init__(self, lab_name: str, log_file: Path):
        self.lab_name = lab_name
        self.log_file = log_file

    #Costruisce nome container
    def _clab(self, node: str) -> str:
        return f"clab-{self.lab_name}-{node}"

    #Verifica con docker ps se esiste il container
    def _container_exists(self, container_name: str) -> bool:
        result = subprocess.run(
            ["docker", "ps", "--format", "{{.Names}}"],
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
        return container_name in result.stdout.splitlines()

    #Gestisce timeout, stdout/stderr, output combinato
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
    
    #Verifica:comando non vuoto, no token vietati, no sottostringhe bloccate,
    #se è ping, deve essere bounded con -c ,deve iniziare con uno dei prefissi ammessi
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

    #Esegue:
    #ip addr show
    #ip route show
    #cat /proc/sys/net/ipv4/ip_forward
    #e li concatena in un unico blob di stato
    def get_running_config(self, device: str) -> dict:
        ip_out = self._exec(device, "ip addr show")
        route_out = self._exec(device, "ip route show")
        fwd_out = self._exec(device, "cat /proc/sys/net/ipv4/ip_forward")

        config = (
            f"=== ip addr ===\n{ip_out}\n"
            f"=== routes ===\n{route_out}\n"
            f"=== ip_forward ===\n{fwd_out}"
        )
        return {"running_config": config}

    #Per ogni comando:
    #strip
    #verifica allowlist
    #logga in agent_commands.jsonl
    #prova esecuzione
    #registra status
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

    #Esegue il comando in modalità check=False, cioè non esplode se il comando fallisce
    def execute_validation(self, device: str, command: str) -> dict:
        try:
            out = self._exec(device, command, check=False)
            return {"output": out}
        except subprocess.TimeoutExpired:
            return {"output": "validation command timed out"}
        except Exception as e:
            return {"output": str(e)}