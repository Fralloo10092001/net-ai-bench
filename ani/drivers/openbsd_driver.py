from __future__ import annotations

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

READ_ONLY_PREFIXES = [
    "ifconfig",
    "route -n show",
    "netstat -rn",
    "sysctl net.inet.ip.forwarding",
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
    # Inizializza il driver salvando il nome del lab e il file di log
    def __init__(self, lab_name: str, log_file: Path):
        self.lab_name = lab_name
        self.log_file = log_file

    # Costruisce il nome reale del container Containerlab per un dato nodo
    def _clab(self, node: str) -> str:
        return f"clab-{self.lab_name}-{node}"

    # Verifica se il container del nodo esiste ed è visibile via docker ps
    def _container_exists(self, container_name: str) -> bool:
        result = subprocess.run(
            ["docker", "ps", "--format", "{{.Names}}"],
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
        return container_name in result.stdout.splitlines()

    # Esegue un comando shell nel container e restituisce stdout/stderr combinati
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

    # Controlla se un comando generico rientra nella allowlist di sicurezza
    def _is_allowed_command(self, cmd: str) -> bool:
        cmd = cmd.strip()

        if not cmd:
            return False

        if any(tok in cmd for tok in FORBIDDEN_TOKENS):
            return False

        if any(bad in cmd for bad in BLOCKED_SUBSTRINGS):
            return False

        if cmd.startswith("ping ") or cmd.startswith("ping6 "):
            return "-c" in cmd

        return any(cmd.startswith(prefix) for prefix in ALLOWED_PREFIXES)

    # Controlla se un comando è read-only e quindi sicuro per inspection/validation
    def _is_readonly_command(self, cmd: str) -> bool:
        cmd = cmd.strip()

        if not cmd:
            return False

        if any(tok in cmd for tok in FORBIDDEN_TOKENS):
            return False

        if any(bad in cmd for bad in BLOCKED_SUBSTRINGS):
            return False

        if cmd.startswith("ping ") or cmd.startswith("ping6 "):
            return "-c" in cmd

        return any(cmd.startswith(prefix) for prefix in READ_ONLY_PREFIXES)

    # Esegue un comando solo se è esplicitamente classificato come read-only
    def _exec_readonly(self, node: str, command: str) -> str:
        if not self._is_readonly_command(command):
            raise RuntimeError(f"Read-only command not allowed: {command}")

        return self._exec(node, command, check=False)

    # Ricava uno snapshot read-only dello stato corrente di un nodo OpenBSD
    def get_running_config(self, device: str) -> dict:
        sections = [
            ("ifconfig", "ifconfig"),
            ("routes", "netstat -rn"),
            ("ip_forward", "sysctl net.inet.ip.forwarding"),
        ]

        outputs = []
        for title, command in sections:
            try:
                out = self._exec_readonly(device, command)
            except Exception as e:
                out = f"ERROR: {e}"
            outputs.append(f"=== {title} ===\n{out}")

        return {"running_config": "\n".join(outputs)}

    # Cattura uno snapshot minimale dello stato OpenBSD per rollback diagnostico
    def capture_rollback_state(self, device: str) -> dict:
        return {
            "kind": "openbsd",
            "running_config": self.get_running_config(device).get("running_config", ""),
        }

    # Per OpenBSD il rollback non è ancora supportato in questa v1
    def restore_rollback_state(self, device: str, snapshot: dict) -> dict:
        return {
            "status": "unsupported",
            "device": device,
            "message": "rollback is not yet supported for openbsd in rollback v1",
        }

    # Applica una lista di comandi di configurazione e li registra nel log dei comandi eseguiti
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

    # Esegue un comando di validazione, purché read-only, senza far esplodere il driver se fallisce
    def execute_validation(self, device: str, command: str) -> dict:
        try:
            out = self._exec_readonly(device, command)
            return {"output": out}
        except subprocess.TimeoutExpired:
            return {"output": "validation command timed out"}
        except Exception as e:
            return {"output": str(e)}
