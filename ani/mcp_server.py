import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import yaml
from drivers import get_driver
from mcp.server.fastmcp import FastMCP

# Questo file espone i tool via FastMCP

_lab_name: str = ""
_task_file: Path = Path()
_log_file: Path = Path()
_project_root: Path = Path()
_node_kinds: dict[str, str] = {}
_rollback_stacks: dict[str, list[dict[str, Any]]] = {}

# Sono globali perché il server MCP viene avviato una volta per run e
# poi i tool devono sapere su quale lab/task operano

# Crea il server MCP
mcp = FastMCP("netconfbench-ani")


# Costruisce il nome del container dato il nome del nodo in containerlab
def _clab(node: str) -> str:
    return f"clab-{_lab_name}-{node}"


# Carica il task JSON
def _load_task() -> dict:
    with open(_task_file) as f:
        return json.load(f)


# Dal task ricava lab_file, lo risolve rispetto alla root progetto e controlla che esista
def _resolve_lab_file() -> Path:
    task = _load_task()
    lab_file = task.get("lab_file")
    if not lab_file:
        raise ValueError("Task JSON must contain 'lab_file'")

    lab_path = (_project_root / lab_file).resolve()
    if not lab_path.exists():
        raise FileNotFoundError(f"Lab file not found: {lab_path}")

    return lab_path


# Legge il YAML Containerlab
def _load_lab_yaml() -> dict:
    lab_path = _resolve_lab_file()
    with open(lab_path) as f:
        data = yaml.safe_load(f)

    if not isinstance(data, dict):
        raise ValueError(f"Invalid lab YAML structure in {lab_path}")

    return data


# Estrae dal YAML lista nodi e lista link in formato leggibile
def _load_topology_from_lab() -> dict:
    lab = _load_lab_yaml()

    topo = lab.get("topology", {})
    nodes_section = topo.get("nodes", {})
    links_section = topo.get("links", [])

    nodes = list(nodes_section.keys())

    links = []
    for link in links_section:
        endpoints = link.get("endpoints", [])
        if len(endpoints) == 2:
            links.append(f"{endpoints[0]} <-> {endpoints[1]}")

    return {
        "nodes": nodes,
        "links": links,
    }


# Costruisce un dict node_name -> kind per selezionare il driver corretto
def _load_node_kinds_from_lab() -> dict[str, str]:
    lab = _load_lab_yaml()
    topo = lab.get("topology", {})
    nodes_section = topo.get("nodes", {})

    result = {}
    for node_name, node_data in nodes_section.items():
        result[node_name] = node_data.get("kind", "linux")

    return result


# Restituisce il driver corretto per il device richiesto
def _driver_for(device: str):
    kind = _node_kinds.get(device, "linux")
    return get_driver(kind=kind, lab_name=_lab_name, log_file=_log_file)


# Usa docker ps per elencare container attivi
def _list_running_containers() -> list[str]:
    result = subprocess.run(
        ["docker", "ps", "--format", "{{.Names}}"],
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    )
    return result.stdout.splitlines()


# Controlla se i container attesi dal lab sono attivi e stampa warning se mancano
def _warn_if_lab_not_running() -> None:
    try:
        topology = _load_topology_from_lab()
        nodes = topology.get("nodes", [])
        running = set(_list_running_containers())
        expected = {_clab(node) for node in nodes}
        missing = sorted(expected - running)

        if missing:
            print(
                "WARNING: some lab containers are not running: " + ", ".join(missing),
                file=sys.stderr,
            )
    except Exception as e:
        print(f"WARNING: could not verify lab status: {e}", file=sys.stderr)


# Inizializza gli stack di rollback per tutti i nodi del lab
def _init_rollback_stacks() -> None:
    global _rollback_stacks
    topology = _load_topology_from_lab()
    _rollback_stacks = {node: [] for node in topology.get("nodes", [])}


# Cattura e salva uno snapshot per un device nello stack di rollback
def _push_snapshot(device: str) -> dict[str, Any]:
    driver = _driver_for(device)
    snapshot = driver.capture_rollback_state(device)
    _rollback_stacks.setdefault(device, []).append(snapshot)
    return snapshot


# Ripristina l'ultimo snapshot disponibile per un device
def _pop_and_restore_snapshot(device: str) -> dict[str, Any]:
    stack = _rollback_stacks.get(device, [])
    if not stack:
        return {
            "status": "error",
            "device": device,
            "message": "no rollback snapshot available",
            "remaining_snapshots": 0,
        }

    snapshot = stack.pop()
    driver = _driver_for(device)
    restore_result = driver.restore_rollback_state(device, snapshot)

    return {
        "device": device,
        "restore_result": restore_result,
        "remaining_snapshots": len(stack),
    }


# Ritorna la topologia intera o filtrata per devices
@mcp.tool()
def get_topology(devices: list[str] | None = None) -> dict:
    """
    Return the lab topology (nodes and links) from the lab YAML.
    If devices is None or empty, return the full topology.
    """
    topology = _load_topology_from_lab()

    if devices:
        filtered = {
            "nodes": [n for n in topology["nodes"] if n in devices],
            "links": [l for l in topology["links"] if any(d in l for d in devices)],
        }
        return {"topology": filtered}

    return {"topology": topology}


# Restituisce la running config corrente del device tramite il driver corretto
@mcp.tool()
def get_running_config(device: str) -> dict:
    """
    Return the active configuration of a device.
    """
    driver = _driver_for(device)
    return driver.get_running_config(device)


# Applica una lista di comandi dopo aver salvato automaticamente uno snapshot rollback del device
@mcp.tool()
def update_config(device: str, commands: list[str]) -> dict:
    """
    Apply a list of commands to a device.
    A rollback snapshot is automatically captured before mutation.
    """
    driver = _driver_for(device)

    snapshot_info: dict[str, Any]
    try:
        snapshot = _push_snapshot(device)
        snapshot_info = {
            "captured": True,
            "device": device,
            "stack_depth": len(_rollback_stacks.get(device, [])),
            "snapshot_kind": snapshot.get("kind", "generic"),
        }
    except Exception as e:
        snapshot_info = {
            "captured": False,
            "device": device,
            "error": str(e),
            "stack_depth": len(_rollback_stacks.get(device, [])),
        }

    result = driver.update_config(device, commands)
    result["rollback_snapshot"] = snapshot_info
    return result


# Esegue un comando read-only di validazione tramite il driver corretto
@mcp.tool()
def execute_validation(device: str, command: str) -> dict:
    """
    Execute a read-only command on a device and return its output.
    """
    driver = _driver_for(device)
    return driver.execute_validation(device, command)


# Ripristina l'ultimo snapshot disponibile per il device richiesto
@mcp.tool()
def rollback(device: str) -> dict:
    """
    Roll back the device to the last saved snapshot.
    """
    return _pop_and_restore_snapshot(device)


# Restituisce informazioni sul numero di snapshot disponibili per device
@mcp.tool()
def get_rollback_status(device: str | None = None) -> dict:
    """
    Return rollback stack information for one device or for the whole lab.
    """
    if device:
        return {
            "device": device,
            "available_snapshots": len(_rollback_stacks.get(device, [])),
        }

    return {
        "devices": {
            dev: len(stack)
            for dev, stack in _rollback_stacks.items()
        }
    }


# Avvia il server MCP inizializzando task, lab, driver map e rollback stacks
def main() -> None:
    global _lab_name, _task_file, _log_file, _project_root, _node_kinds

    parser = argparse.ArgumentParser(description="NetConfBench MCP Server (ANI)")
    parser.add_argument("--lab", required=True, help="Containerlab lab name")
    parser.add_argument("--task", required=True, help="Path to task JSON file")
    parser.add_argument("--log", default="../logs/agent_commands.jsonl", help="Path for command log output")
    args = parser.parse_args()

    _lab_name = args.lab
    _task_file = Path(args.task).resolve()
    _log_file = Path(args.log).resolve()
    _project_root = _task_file.parent.parent.resolve()

    _log_file.parent.mkdir(parents=True, exist_ok=True)

    if not _task_file.exists():
        print(f"ERROR: task file not found: {_task_file}", file=sys.stderr)
        sys.exit(1)

    try:
        _resolve_lab_file()
    except Exception as e:
        print(f"ERROR: could not resolve lab file from task: {e}", file=sys.stderr)
        sys.exit(1)

    _node_kinds = _load_node_kinds_from_lab()
    _init_rollback_stacks()

    print(f"ANI server ready | lab={_lab_name} | task={_task_file}", file=sys.stderr)
    _warn_if_lab_not_running()
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()