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


# Restituisce il kind Containerlab associato a un device
def _kind_for(device: str) -> str:
    return _node_kinds.get(device, "linux")


# Indica se il rollback reale è supportato per il kind del device
def _rollback_supported_for_kind(kind: str) -> bool:
    return kind == "linux"


# Costruisce un blocco informativo uniforme sul supporto rollback di un device
def _rollback_support_info(device: str) -> dict[str, Any]:
    kind = _kind_for(device)
    return {
        "device": device,
        "driver_kind": kind,
        "rollback_supported": _rollback_supported_for_kind(kind),
    }


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
    support_info = _rollback_support_info(device)
    snapshot = driver.capture_rollback_state(device)

    snapshot["driver_kind"] = support_info["driver_kind"]
    snapshot["rollback_supported"] = support_info["rollback_supported"]

    _rollback_stacks.setdefault(device, []).append(snapshot)
    return snapshot


def _filesystem_snapshot_info(snapshot: dict[str, Any]) -> dict[str, Any]:
    fs_state = snapshot.get("filesystem", {})
    if not fs_state:
        return {
            "captured": False,
        }

    return {
        "captured": bool(fs_state.get("available")),
        "strategy": fs_state.get("strategy", ""),
        "tracked_paths": fs_state.get("tracked_paths", []),
        "archived_paths": fs_state.get("archived_paths", []),
        "archive_size": fs_state.get("archive_size", 0),
        "manifest_entries": len(fs_state.get("manifest", [])),
        "error": fs_state.get("error", ""),
    }


# Ripristina l'ultimo snapshot disponibile per un device
def _pop_and_restore_snapshot(device: str) -> dict[str, Any]:
    support_info = _rollback_support_info(device)
    stack = _rollback_stacks.get(device, [])

    if not stack:
        return {
            "status": "error",
            "device": device,
            "message": "no rollback snapshot available",
            "remaining_snapshots": 0,
            **support_info,
        }

    snapshot = stack.pop()
    driver = _driver_for(device)
    restore_result = driver.restore_rollback_state(device, snapshot)

    return {
        "status": restore_result.get("status", "unknown"),
        "device": device,
        "restore_result": restore_result,
        "remaining_snapshots": len(stack),
        **support_info,
    }


# Ritorna la topologia intera o filtrata per devices
@mcp.tool()
def get_topology(devices: list[str] | None = None) -> dict:
    """
    Restituisce la topologia del lab (nodi e link) dal YAML del lab.
    Se devices è None o vuoto, restituisce la topologia completa.
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
    Restituisce la configurazione attiva di un device.
    """
    driver = _driver_for(device)
    return driver.get_running_config(device)


# Applica una lista di comandi dopo aver salvato automaticamente uno snapshot rollback del device
@mcp.tool()
def update_config(device: str, commands: list[str]) -> dict:
    """
    Applica una lista di comandi a un device.
    Prima della modifica viene catturato automaticamente uno snapshot di rollback.

    Per scrivere file di configurazione allowlistati, usare il formato esatto:
    printf %s <base64-utf8> | base64 -d | tee <path> >/dev/null
    Path scrivibili: /etc/frr/frr.conf, /etc/frr/daemons, /etc/frr/vtysh.conf,
    /etc/haproxy/haproxy.cfg, /usr/local/etc/haproxy/haproxy.cfg, /etc/nftables.conf.
    """
    driver = _driver_for(device)
    support_info = _rollback_support_info(device)

    snapshot_info: dict[str, Any]
    try:
        snapshot = _push_snapshot(device)
        snapshot_info = {
            "captured": True,
            "device": device,
            "stack_depth": len(_rollback_stacks.get(device, [])),
            "snapshot_kind": snapshot.get("kind", support_info["driver_kind"]),
            "filesystem": _filesystem_snapshot_info(snapshot),
            **support_info,
        }
    except Exception as e:
        snapshot_info = {
            "captured": False,
            "device": device,
            "error": str(e),
            "stack_depth": len(_rollback_stacks.get(device, [])),
            **support_info,
        }

    result = driver.update_config(device, commands)
    result["rollback_snapshot"] = snapshot_info
    return result


# Esegue un comando read-only di validazione tramite il driver corretto
@mcp.tool()
def execute_validation(device: str, command: str) -> dict:
    """
    Esegue un comando in sola lettura su un device e restituisce il suo output.
    """
    driver = _driver_for(device)
    return driver.execute_validation(device, command)


# Ripristina l'ultimo snapshot disponibile per il device richiesto
@mcp.tool()
def rollback(device: str) -> dict:
    """
    Ripristina il device all'ultimo snapshot salvato.
    """
    return _pop_and_restore_snapshot(device)


# Restituisce informazioni sul numero di snapshot disponibili e sul supporto rollback
@mcp.tool()
def get_rollback_status(device: str | None = None) -> dict:
    """
    Restituisce informazioni sullo stack di rollback per un device o per l'intero lab.
    """
    if device:
        support_info = _rollback_support_info(device)
        return {
            **support_info,
            "available_snapshots": len(_rollback_stacks.get(device, [])),
        }

    return {
        "devices": {
            dev: {
                **_rollback_support_info(dev),
                "available_snapshots": len(stack),
            }
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
