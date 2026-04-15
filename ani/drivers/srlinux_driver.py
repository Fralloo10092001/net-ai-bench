import json
import subprocess
from pathlib import Path

from .base import BaseDriver

#Comandi read-only ammessi
SHOW_PREFIXES = [
    "show ",
    "info",
    "info ",
    "ping ",
    "ping6 ",
    "hostname",
    "uname ",
]

#Divieti focalizzati sul non permettere shell dannose
CONFIG_BLOCKED_SUBSTRINGS = [
    "rm ",
    "reboot",
    "shutdown",
    "poweroff",
    "mkfs",
    "> /etc/",
    ">> /etc/",
    "chmod ",
    "chown ",
    "curl ",
    "wget ",
    "bash -c",
]

FORBIDDEN_TOKENS = ["&&", "||", ";"]


class SRLinuxDriver(BaseDriver):
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

    #Esegue shell nel container con docker exec ... bash -lc ...
    def _exec_shell(self, node: str, command: str, check: bool = True) -> str:
        container_name = self._clab(node)

        if not self._container_exists(container_name):
            raise RuntimeError(f"Container not found: {container_name}")

        result = subprocess.run(
            ["docker", "exec", container_name, "bash", "-lc", command],
            capture_output=True,
            text=True,
            check=check,
            timeout=20,
        )

        stdout = result.stdout.strip()
        stderr = result.stderr.strip()

        if stderr and not stdout:
            return stderr
        if stdout and stderr:
            return f"{stdout}\n{stderr}"
        return stdout

    #Invia uno script via stdin a sr_cli, questa è la logica di configurazione SR Linux.
    def _exec_sr_cli_script(self, node: str, lines: list[str]) -> str:
        container_name = self._clab(node)

        if not self._container_exists(container_name):
            raise RuntimeError(f"Container not found: {container_name}")

        script = "\n".join(lines) + "\n"

        result = subprocess.run(
            ["docker", "exec", "-i", container_name, "sr_cli"],
            input=script,
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        )

        stdout = result.stdout.strip()
        stderr = result.stderr.strip()

        if stderr and not stdout:
            return stderr
        if stdout and stderr:
            return f"{stdout}\n{stderr}"
        return stdout

    #Rimuove righe spazzatura, in particolare l’errore 
    # Parsing error: Unknown token 'commit'
    def _sanitize_show_output(self, text: str) -> str:
        lines = text.splitlines()
        filtered = [
            line for line in lines
            if "Parsing error: Unknown token 'commit'" not in line
        ]
        return "\n".join(filtered).strip()

    #Controlla che i comandi di validazione siano sicuri
    def _is_allowed_show_command(self, cmd: str) -> bool:
        cmd = cmd.strip()

        if not cmd:
            return False

        if any(tok in cmd for tok in FORBIDDEN_TOKENS):
            return False

        if any(bad in cmd for bad in CONFIG_BLOCKED_SUBSTRINGS):
            return False

        if cmd.startswith("ping ") or cmd.startswith("ping6 "):
            if "-c" not in cmd:
                return False

        return any(cmd.startswith(prefix) for prefix in SHOW_PREFIXES)

    #Controlla che le linee di configurazione non contengano token/sottostringhe vietati
    def _is_allowed_config_line(self, line: str) -> bool:
        line = line.strip()

        if not line:
            return False

        if any(tok in line for tok in FORBIDDEN_TOKENS):
            return False

        if any(bad in line for bad in CONFIG_BLOCKED_SUBSTRINGS):
            return False

        return True

    def get_running_config(self, device: str) -> dict:
        info_out = self._sanitize_show_output(
            self._exec_shell(device, 'sr_cli -c "info from running"', check=False)
        )
        intf_out = self._sanitize_show_output(
            self._exec_shell(device, 'sr_cli -c "show interface brief"', check=False)
        )
        version_out = self._sanitize_show_output(
            self._exec_shell(device, 'sr_cli -c "show version"', check=False)
        )

        config = (
            f"=== show version ===\n{version_out}\n"
            f"=== show interface brief ===\n{intf_out}\n"
            f"=== info from running ===\n{info_out}"
        )
        return {"running_config": config}

    def update_config(self, device: str, commands: list[str]) -> dict:
        cleaned = []
        blocked = []

        for cmd in commands:
            cmd = cmd.strip()
            if not self._is_allowed_config_line(cmd):
                blocked.append({
                    "command": cmd,
                    "status": "blocked",
                    "output": "command not allowed",
                })
            else:
                cleaned.append(cmd)

        if not cleaned:
            return {"results": blocked}

        for cmd in cleaned:
            with open(self.log_file, "a") as f:
                f.write(json.dumps({"device": device, "command": cmd}) + "\n")

        script_lines = ["enter candidate"] + cleaned + ["commit now", "quit"]

        try:
            out = self._exec_sr_cli_script(device, script_lines)
            results = blocked + [{
                "command": "\n".join(cleaned),
                "status": "success",
                "output": out,
            }]
            return {"results": results}
        except subprocess.TimeoutExpired:
            results = blocked + [{
                "command": "\n".join(cleaned),
                "status": "error",
                "output": "command timed out",
            }]
            return {"results": results}
        except subprocess.CalledProcessError as e:
            stderr = (e.stderr or "").strip()
            stdout = (e.stdout or "").strip()
            output = stderr or stdout or str(e)
            results = blocked + [{
                "command": "\n".join(cleaned),
                "status": "error",
                "output": output,
            }]
            return {"results": results}
        except Exception as e:
            results = blocked + [{
                "command": "\n".join(cleaned),
                "status": "error",
                "output": str(e),
            }]
            return {"results": results}

    def execute_validation(self, device: str, command: str) -> dict:
        command = command.strip()

        if not self._is_allowed_show_command(command):
            return {"output": "validation command not allowed"}

        try:
            if command.startswith(("show ", "info")):
                out = self._exec_shell(device, f'sr_cli -c "{command}"', check=False)
                return {"output": self._sanitize_show_output(out)}

            out = self._exec_shell(device, command, check=False)
            return {"output": out}
        except subprocess.TimeoutExpired:
            return {"output": "validation command timed out"}
        except Exception as e:
            return {"output": str(e)}