import json
import subprocess
from pathlib import Path
from typing import Any

from .base import BaseDriver


# Prefissi ammessi per comandi read-only su SR Linux
SHOW_PREFIXES = [
    "show ",
    "info",
    "info ",
    "ping ",
    "ping6 ",
    "hostname",
    "uname ",
]

# Sottostringhe vietate nei comandi di configurazione o shell
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

# Token shell vietati
FORBIDDEN_TOKENS = ["&&", "||", ";"]


# Driver per nodi Nokia SR Linux
class SRLinuxDriver(BaseDriver):
    # Inizializza il driver salvando il nome del lab e il file di log
    def __init__(self, lab_name: str, log_file: Path):
        self.lab_name = lab_name
        self.log_file = log_file

    # Costruisce il nome del container Containerlab per un nodo SR Linux
    def _clab(self, node: str) -> str:
        return f"clab-{self.lab_name}-{node}"

    # Verifica che il container del nodo esista ed sia attivo
    def _container_exists(self, container_name: str) -> bool:
        result = subprocess.run(
            ["docker", "ps", "--format", "{{.Names}}"],
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
        return container_name in result.stdout.splitlines()

    # Esegue un comando shell nel container SR Linux
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

    # Esegue uno script via stdin dentro sr_cli per applicare configurazione candidate/commit
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

    # Ripulisce l'output dei comandi show/info rimuovendo rumore noto
    def _sanitize_show_output(self, text: str) -> str:
        lines = text.splitlines()
        filtered = [
            line for line in lines
            if "Parsing error: Unknown token 'commit'" not in line
        ]
        return "\n".join(filtered).strip()

    # Verifica che un comando di validazione/show sia consentito e bounded
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

    # Verifica che una riga di configurazione candidate non contenga contenuti pericolosi
    def _is_allowed_config_line(self, line: str) -> bool:
        line = line.strip()

        if not line:
            return False

        if any(tok in line for tok in FORBIDDEN_TOKENS):
            return False

        if any(bad in line for bad in CONFIG_BLOCKED_SUBSTRINGS):
            return False

        return True

    # Esegue un comando SR Linux read-only e ne sanifica l'output
    def _exec_readonly_srl(self, device: str, command: str) -> str:
        if not self._is_allowed_show_command(command):
            raise RuntimeError(f"Read-only command not allowed: {command}")

        if command.startswith(("show ", "info")):
            out = self._exec_shell(device, f'sr_cli -c "{command}"', check=False)
            return self._sanitize_show_output(out)

        out = self._exec_shell(device, command, check=False)
        return self._sanitize_show_output(out)

    # Ricava uno snapshot read-only dello stato corrente di un nodo SR Linux
    def get_running_config(self, device: str) -> dict:
        sections = [
            ("show version", 'sr_cli -c "show version"'),
            ("show interface brief", 'sr_cli -c "show interface brief"'),
            ("info from running", 'sr_cli -c "info from running"'),
        ]

        outputs = []
        for title, shell_command in sections:
            try:
                out = self._exec_shell(device, shell_command, check=False)
                out = self._sanitize_show_output(out)
            except Exception as e:
                out = f"ERROR: {e}"
            outputs.append(f"=== {title} ===\n{out}")

        return {"running_config": "\n".join(outputs)}

    # Cattura uno snapshot minimale per SR Linux, utile per diagnostica ma non ancora per restore
    def capture_rollback_state(self, device: str) -> dict[str, Any]:
        return {
            "kind": "nokia_srlinux",
            "running_config": self.get_running_config(device).get("running_config", ""),
        }

    # Per SR Linux il rollback non è ancora supportato in questa v1
    def restore_rollback_state(self, device: str, snapshot: dict[str, Any]) -> dict:
        return {
            "status": "unsupported",
            "device": device,
            "message": "rollback is not yet supported for nokia_srlinux in rollback v1",
        }

    # Applica una lista di comandi di configurazione dentro candidate e commit now
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

    # Esegue un comando di validazione SR Linux, purché read-only
    def execute_validation(self, device: str, command: str) -> dict:
        command = command.strip()

        try:
            out = self._exec_readonly_srl(device, command)
            return {"output": out}
        except subprocess.TimeoutExpired:
            return {"output": "validation command timed out"}
        except Exception as e:
            return {"output": str(e)}