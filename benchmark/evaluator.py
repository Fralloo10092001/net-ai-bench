import argparse
import json
import subprocess
import yaml
from pathlib import Path
from typing import Any


BASE_DIR = Path(__file__).resolve().parent.parent
LOGS_DIR = BASE_DIR / "logs"

#Carica JSON
def load_json(path: Path) -> dict[str, Any]:
    with open(path) as f:
        return json.load(f)

#Carica file JSON Lines, usato per agent_commands.jsonl
def load_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []

    rows = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows



def clab_name(lab: str, node: str) -> str:
    return f"clab-{lab}-{node}"


def exec_in_container(
    lab: str,
    node: str,
    command: str,
    kind: str = "linux",
    timeout: int = 15,
) -> str:
    container = clab_name(lab, node)

    if kind == "nokia_srlinux":
        if command.startswith("show ") or command.startswith("info") or command.startswith("ping "):
            result = subprocess.run(
                ["docker", "exec", container, "bash", "-lc", f'sr_cli -c "{command}"'],
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        else:
            result = subprocess.run(
                ["docker", "exec", container, "bash", "-lc", command],
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
    else:
        result = subprocess.run(
            ["docker", "exec", container, "sh", "-c", command],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )

    stdout = result.stdout.strip()
    stderr = result.stderr.strip()

    if kind == "nokia_srlinux":
        stdout = sanitize_srl_output(stdout)
        stderr = sanitize_srl_output(stderr)

    if stdout and stderr:
        return f"{stdout}\n{stderr}"
    if stdout:
        return stdout
    return stderr

#Legge il lab YAML associato al task
def load_lab_yaml_from_task(task: dict) -> dict:
    project_root = Path(__file__).resolve().parents[1]
    lab_file = task.get("lab_file")
    if not lab_file:
        raise ValueError("Task JSON must contain 'lab_file'")

    lab_path = (project_root / lab_file).resolve()
    with open(lab_path) as f:
        data = yaml.safe_load(f)

    if not isinstance(data, dict):
        raise ValueError(f"Invalid lab YAML structure in {lab_path}")

    return data

#Ricava node -> kind
def load_node_kinds(task: dict) -> dict[str, str]:
    lab = load_lab_yaml_from_task(task)
    topo = lab.get("topology", {})
    nodes = topo.get("nodes", {})

    result = {}
    for node_name, node_data in nodes.items():
        result[node_name] = node_data.get("kind", "linux")

    return result

#Rimuove righe non desiderate
def sanitize_srl_output(text: str) -> str:
    lines = text.splitlines()
    filtered = [
        line for line in lines
        if "Parsing error: Unknown token 'commit'" not in line
    ]
    return "\n".join(filtered).strip()

"""Per ogni testcase:
legge device
legge command
legge expected_output
identifica kind
esegue comando nel container
controlla se expected_output in actual_output """
def run_testcases(task: dict[str, Any], lab: str) -> tuple[float, list[dict[str, Any]]]:
    testcases = task.get("testcases", [])
    if not testcases:
        return 0.0, []

    node_kinds = load_node_kinds(task)

    results = []
    passed = 0

    for tc in testcases:
        name = tc["name"]
        device = tc["device"]
        command = tc["command"]
        expected_output = tc["expected_output"]

        kind = node_kinds.get(device, "linux")
        actual_output = exec_in_container(lab, device, command, kind=kind)

        ok = expected_output in actual_output
        if ok:
            passed += 1

        results.append({
            "name": name,
            "device": device,
            "kind": kind,
            "command": command,
            "expected_output": expected_output,
            "actual_output": actual_output,
            "passed": ok,
        })

    score = passed / len(testcases)
    return score, results

#Normalizza spazi
def normalize_command(cmd: str) -> str:
    return " ".join(cmd.strip().split())

#Prende i comandi attesi da ground_truth_configs
def extract_ground_truth_commands(task: dict[str, Any]) -> list[str]:
    gt = task.get("ground_truth_configs", {})
    commands = []

    for _device, cmd_list in gt.items():
        for cmd in cmd_list:
            commands.append(normalize_command(cmd))

    return commands

#Prende i comandi realmente eseguiti dal log
def extract_applied_commands(command_log_rows: list[dict[str, Any]]) -> list[str]:
    commands = []
    for row in command_log_rows:
        cmd = row.get("command", "")
        if cmd:
            commands.append(normalize_command(cmd))
    return commands


def compute_command_score(
    task: dict[str, Any],
    command_log_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    node_kinds = load_node_kinds(task)
    contains_srl = any(kind == "nokia_srlinux" for kind in node_kinds.values())

    if contains_srl:
        return {
            "score": 1.0,
            "precision": 1.0,
            "recall": 1.0,
            "ground_truth_commands": [],
            "applied_commands": extract_applied_commands(command_log_rows),
            "correct_commands": [],
            "missing_commands": [],
            "extra_commands": [],
            "note": "SR Linux command scoring temporarily skipped",
        }

    gt_commands = extract_ground_truth_commands(task)
    applied_commands = extract_applied_commands(command_log_rows)

    gt_set = set(gt_commands)
    applied_set = set(applied_commands)

    correct = gt_set & applied_set

    precision = len(correct) / len(applied_set) if applied_set else 0.0
    recall = len(correct) / len(gt_set) if gt_set else 0.0

    if precision + recall == 0:
        f1 = 0.0
    else:
        f1 = 2 * precision * recall / (precision + recall)

    return {
        "score": f1,
        "precision": precision,
        "recall": recall,
        "ground_truth_commands": sorted(gt_set),
        "applied_commands": sorted(applied_set),
        "correct_commands": sorted(correct),
        "missing_commands": sorted(gt_set - applied_set),
        "extra_commands": sorted(applied_set - gt_set),
    }

#Usa segnali proxy, non embedding o cosine similarity.
#Non misura il reasoning “vero”, ma premia comportamento agentico ragionevole
def compute_reasoning_score(history: list[dict[str, Any]]) -> dict[str, Any]:
    if not history:
        return {
            "score": 0.0,
            "signals": {},
        }

    tool_sequence = [
        step.get("tool_name")
        for step in history
        if step.get("action") == "tool_call"
    ]

    observations = [str(step.get("observation", "")) for step in history]
    actions = [step.get("action", "") for step in history]

    starts_with_topology = 1.0 if tool_sequence and tool_sequence[0] == "get_topology" else 0.0

    used_running_cfg_early = 0.0
    for i, name in enumerate(tool_sequence[:3]):
        if name == "get_running_config":
            used_running_cfg_early = 1.0
            break

    validated = 1.0 if "execute_validation" in tool_sequence else 0.0

    finished = 1.0 if actions and actions[-1] == "finish" else 0.0

    # errori e penalità
    error_count = sum("error" in obs.lower() for obs in observations)
    parse_error_count = sum(step.get("action") == "parse_error" for step in history)
    invalid_tool_count = sum("invalid tool name" in obs.lower() for obs in observations)

    total_steps = len(history)
    error_penalty = error_count / max(total_steps, 1)
    parse_penalty = parse_error_count / max(total_steps, 1)
    invalid_tool_penalty = invalid_tool_count / max(total_steps, 1)

    if total_steps <= 6:
        efficiency = 1.0
    elif total_steps <= 10:
        efficiency = 0.8
    elif total_steps <= 15:
        efficiency = 0.6
    else:
        efficiency = 0.3

    score = (
        starts_with_topology * 0.15 +
        used_running_cfg_early * 0.20 +
        validated * 0.20 +
        finished * 0.10 +
        efficiency * 0.15 +
        (1 - error_penalty) * 0.10 +
        (1 - parse_penalty) * 0.05 +
        (1 - invalid_tool_penalty) * 0.05
    )

    score = max(0.0, min(1.0, score))

    return {
        "score": score,
        "signals": {
            "starts_with_topology": starts_with_topology,
            "used_running_cfg_early": used_running_cfg_early,
            "validated": validated,
            "finished": finished,
            "efficiency": efficiency,
            "error_penalty": error_penalty,
            "parse_penalty": parse_penalty,
            "invalid_tool_penalty": invalid_tool_penalty,
            "total_steps": total_steps,
        },
    }

""" Carica:
        task
        history
        command log

        Calcola:
        S_testcase
        S_command
        S_reasoning

        Poi:
        summary_score = 0.5 * testcase + 0.3 * command + 0.2 * reasoning

        Ritorna JSON finale con dettagli. """
def evaluate(
    task_path: Path,
    lab: str,
    history_path: Path | None = None,
    commands_path: Path | None = None,
) -> dict[str, Any]:
    task = load_json(task_path)

    if history_path is None:
        history_path = LOGS_DIR / "agent_history.json"
    if commands_path is None:
        commands_path = LOGS_DIR / "agent_commands.jsonl"

    history = load_json(history_path)
    command_rows = load_jsonl(commands_path)

    testcase_score, testcase_results = run_testcases(task, lab)
    command_info = compute_command_score(task, command_rows)
    reasoning_info = compute_reasoning_score(history)

    s_testcase = testcase_score
    s_command = command_info["score"]
    s_reasoning = reasoning_info["score"]

    summary_score = (
        0.5 * s_testcase +
        0.3 * s_command +
        0.2 * s_reasoning
    )

    return {
        "task_name": task.get("task_name", task_path.stem),
        "lab": lab,
        "S_testcase": round(s_testcase, 4),
        "S_command": round(s_command, 4),
        "S_reasoning": round(s_reasoning, 4),
        "summary_score": round(summary_score, 4),
        "details": {
            "testcases": testcase_results,
            "command": {
                "precision": round(command_info["precision"], 4),
                "recall": round(command_info["recall"], 4),
                "correct_commands": command_info["correct_commands"],
                "missing_commands": command_info["missing_commands"],
                "extra_commands": command_info["extra_commands"],
                "note": command_info.get("note", ""),
            },
            "reasoning": reasoning_info["signals"],
        },
    }

#Permette di eseguire evaluator da terminale
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="NetConfBench-style evaluator")
    parser.add_argument("--task", required=True, help="Path to task JSON")
    parser.add_argument("--lab", required=True, help="Containerlab lab name")
    parser.add_argument("--history", default=str(LOGS_DIR / "agent_history.json"))
    parser.add_argument("--commands", default=str(LOGS_DIR / "agent_commands.jsonl"))
    args = parser.parse_args()

    result = evaluate(
        task_path=Path(args.task).resolve(),
        lab=args.lab,
        history_path=Path(args.history).resolve(),
        commands_path=Path(args.commands).resolve(),
    )

    print(json.dumps(result, indent=2))