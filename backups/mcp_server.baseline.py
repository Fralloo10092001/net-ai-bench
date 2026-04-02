import argparse
import json
import subprocess
import sys
from pathlib import Path

from mcp.server.fastmcp import FastMCP

_lab_name: str = ""
_task_file: Path = Path()
_log_file: Path = Path()

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

BLOCKED_SUBSTRINGS = [
    "rm ",
    "reboot",
    "shutdown",
    "poweroff",
    "mkfs",
    "dd ",
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

mcp = FastMCP("netconfbench-ani")


def _clab(node: str) -> str:
    return f"clab-{_lab_name}-{node}"


def _container_exists(container_name: str) -> bool:
    result = subprocess.run(
        ["docker", "ps", "--format", "{{.Names}}"],
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    )
    names = result.stdout.splitlines()
    return container_name in names


def _exec(node: str, command: str, check: bool = True) -> str:
    container_name = _clab(node)

    if not _container_exists(container_name):
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


def _load_task() -> dict:
    with open(_task_file) as f:
        return json.load(f)


FORBIDDEN_TOKENS = ["&&", "||", ";"]

def _is_allowed_command(cmd: str) -> bool:
    cmd = cmd.strip()

    if not cmd:
        return False

    if any(tok in cmd for tok in FORBIDDEN_TOKENS):
        return False

    if any(bad in cmd for bad in BLOCKED_SUBSTRINGS):
        return False

    return any(cmd.startswith(prefix) for prefix in ALLOWED_PREFIXES)

def _list_running_containers() -> list[str]:
    result = subprocess.run(
        ["docker", "ps", "--format", "{{.Names}}"],
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    )
    return result.stdout.splitlines()


def _warn_if_lab_not_running() -> None:
    try:
        task = _load_task()
        nodes = task.get("topology", {}).get("nodes", [])
        running = set(_list_running_containers())
        expected = {_clab(node) for node in nodes}
        missing = sorted(expected - running)

        if missing:
            print(
                "WARNING: some lab containers are not running: "
                + ", ".join(missing),
                file=sys.stderr,
            )
    except Exception as e:
        print(f"WARNING: could not verify lab status: {e}", file=sys.stderr)

@mcp.tool()
def get_topology(devices: list[str] | None = None) -> dict:
    """
    Return the lab topology (nodes and links).
    If devices is None or empty, return the full topology.
    """
    task = _load_task()
    topology = task["topology"]

    if devices:
        filtered = {
            "nodes": [n for n in topology["nodes"] if n in devices],
            "links": [l for l in topology["links"] if any(d in l for d in devices)],
        }
        return {"topology": filtered}

    return {"topology": topology}


@mcp.tool()
def get_running_config(device: str) -> dict:
    """
    Return the active configuration of a device:
    IP addresses, routes, and ip_forward flag.
    """
    ip_out = _exec(device, "ip addr show")
    route_out = _exec(device, "ip route show")
    fwd_out = _exec(device, "cat /proc/sys/net/ipv4/ip_forward")

    config = (
        f"=== ip addr ===\n{ip_out}\n"
        f"=== routes ===\n{route_out}\n"
        f"=== ip_forward ===\n{fwd_out}"
    )
    return {"running_config": config}


@mcp.tool()
def update_config(device: str, commands: list[str]) -> dict:
    """
    Apply a list of shell commands to a device via docker exec.
    Returns per-command status (success / error / blocked).
    """
    results = []

    for cmd in commands:
        cmd = cmd.strip()

        if not _is_allowed_command(cmd):
            results.append({
                "command": cmd,
                "status": "blocked",
                "output": "command not allowed",
            })
            continue

        with open(_log_file, "a") as f:
            f.write(json.dumps({"device": device, "command": cmd}) + "\n")

        try:
            out = _exec(device, cmd)
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


@mcp.tool()
def execute_validation(device: str, command: str) -> dict:
    """
    Execute a read-only command on a device and return its output.
    """
    try:
        out = _exec(device, command, check=False)
        return {"output": out}
    except subprocess.TimeoutExpired:
        return {"output": "validation command timed out"}
    except Exception as e:
        return {"output": str(e)}


def main() -> None:
    global _lab_name, _task_file, _log_file

    parser = argparse.ArgumentParser(description="NetConfBench MCP Server (ANI)")
    parser.add_argument("--lab", required=True, help="Containerlab lab name")
    parser.add_argument("--task", required=True, help="Path to task JSON file")
    parser.add_argument("--log", default="../logs/agent_commands.jsonl", help="Path for command log output")
    args = parser.parse_args()

    _lab_name = args.lab
    _task_file = Path(args.task).resolve()
    _log_file = Path(args.log).resolve()

    _log_file.parent.mkdir(parents=True, exist_ok=True)

    if not _task_file.exists():
        print(f"ERROR: task file not found: {_task_file}", file=sys.stderr)
        sys.exit(1)

    print(f"ANI server ready | lab={_lab_name} | task={_task_file}", file=sys.stderr)
    _warn_if_lab_not_running()
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()