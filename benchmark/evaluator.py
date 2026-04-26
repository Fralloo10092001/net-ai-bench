import argparse
import json
import re
import subprocess
import yaml
from pathlib import Path
from typing import Any

from sentence_transformers import SentenceTransformer


BASE_DIR = Path(__file__).resolve().parent.parent
LOGS_DIR = BASE_DIR / "logs"

EMBEDDING_MODEL_NAME = "sentence-transformers/all-MiniLM-L6-v2"
_embedding_model: SentenceTransformer | None = None


# Carica un file JSON e restituisce il contenuto deserializzato
def load_json(path: Path) -> dict[str, Any]:
    with open(path) as f:
        return json.load(f)


# Carica un file JSON Lines e restituisce tutte le righe come lista di dizionari
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


# Carica un file di testo e restituisce il contenuto come stringa
def load_text(path: Path) -> str:
    if not path.exists():
        return ""

    return path.read_text().strip()


# Restituisce il modello di embedding in lazy loading per evitare ricarichi inutili
def get_embedding_model() -> SentenceTransformer:
    global _embedding_model

    if _embedding_model is None:
        _embedding_model = SentenceTransformer(EMBEDDING_MODEL_NAME)

    return _embedding_model


# Costruisce un testo di reasoning a partire dal trace dell'agente quando reasoning.txt non basta
def build_reasoning_text_from_history(history: list[dict[str, Any]]) -> str:
    if not history:
        return ""

    parts: list[str] = []

    for step in history:
        thought = str(step.get("thought", "")).strip()
        action = str(step.get("action", "")).strip()

        if thought:
            parts.append(f"ACTION={action} THOUGHT={thought}")

        if action == "finish":
            final_reasoning = str(step.get("final_reasoning", "")).strip()
            if final_reasoning:
                parts.append(f"FINAL_REASONING={final_reasoning}")

    return "\n".join(parts).strip()


# Estrae il testo di reasoning dell'agente preferendo reasoning.txt e usando il trace come fallback
def extract_agent_reasoning_text(
    history: list[dict[str, Any]],
    reasoning_text: str,
) -> str:
    reasoning_text = reasoning_text.strip()

    if reasoning_text:
        return reasoning_text

    return build_reasoning_text_from_history(history)


# Calcola la cosine similarity tra due testi usando sentence-transformers
def compute_text_cosine_similarity(text_a: str, text_b: str) -> float:
    model = get_embedding_model()

    embeddings = model.encode(
        [text_a, text_b],
        normalize_embeddings=True,
    )

    score = float(embeddings[0] @ embeddings[1])
    return max(0.0, min(1.0, score))


# Costruisce il nome reale del container Containerlab a partire da lab e node
def clab_name(lab: str, node: str) -> str:
    return f"clab-{lab}-{node}"


# Ripulisce l'output SR Linux rimuovendo linee spurie note
def sanitize_srl_output(text: str) -> str:
    lines = text.splitlines()
    filtered = [
        line for line in lines
        if "Parsing error: Unknown token 'commit'" not in line
    ]
    return "\n".join(filtered).strip()


# Esegue un comando dentro un container del lab e restituisce stdout/stderr normalizzato
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


# Legge il file lab YAML associato al task JSON
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


# Ricava la mappa node -> kind leggendo la topologia del lab YAML
def load_node_kinds(task: dict) -> dict[str, str]:
    lab = load_lab_yaml_from_task(task)
    topo = lab.get("topology", {})
    nodes = topo.get("nodes", {})

    result = {}
    for node_name, node_data in nodes.items():
        result[node_name] = node_data.get("kind", "linux")

    return result


# Esegue tutti i testcases funzionali del task e restituisce score e dettagli
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


# Esegue i baseline invariants del task e restituisce score e dettagli di safety
def evaluate_baseline_invariants(task: dict[str, Any], lab: str) -> tuple[float | None, list[dict[str, Any]]]:
    invariants = task.get("baseline_invariants", [])
    if not invariants:
        return None, []

    node_kinds = load_node_kinds(task)

    results = []
    passed = 0

    for inv in invariants:
        name = inv["name"]
        device = inv["device"]
        command = inv["command"]
        expected_output = inv["expected_output"]

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

    score = passed / len(invariants)
    return score, results


# Normalizza la spaziatura di base di un comando
def normalize_whitespace(cmd: str) -> str:
    return " ".join(cmd.strip().split())


# Normalizza una route Linux in forma canonica
def canonicalize_ip_route_add_command(cmd: str) -> str:
    tokens = normalize_whitespace(cmd).split()

    if len(tokens) < 4:
        return normalize_whitespace(cmd)

    prefix = tokens[3]
    via = None
    dev = None
    metric = None

    idx = 4
    while idx < len(tokens):
        token = tokens[idx]
        if token == "via" and idx + 1 < len(tokens):
            via = tokens[idx + 1]
            idx += 2
            continue
        if token == "dev" and idx + 1 < len(tokens):
            dev = tokens[idx + 1]
            idx += 2
            continue
        if token == "metric" and idx + 1 < len(tokens):
            metric = tokens[idx + 1]
            idx += 2
            continue
        idx += 1

    parts = ["ip", "route", "add", prefix]
    if via:
        parts.extend(["via", via])
    if dev:
        parts.extend(["dev", dev])
    if metric:
        parts.extend(["metric", metric])

    return " ".join(parts)


# Normalizza un comando ip addr add in forma canonica
def canonicalize_ip_addr_add_command(cmd: str) -> str:
    tokens = normalize_whitespace(cmd).split()

    if len(tokens) < 6:
        return normalize_whitespace(cmd)

    address = tokens[3]
    dev = None

    idx = 4
    while idx < len(tokens):
        token = tokens[idx]
        if token == "dev" and idx + 1 < len(tokens):
            dev = tokens[idx + 1]
            break
        idx += 1

    if not dev:
        return normalize_whitespace(cmd)

    return f"ip addr add {address} dev {dev}"


# Normalizza un comando echo usato per abilitare ip_forward
def canonicalize_sysctl_echo_command(cmd: str) -> str:
    normalized = normalize_whitespace(cmd)

    if re.fullmatch(r"echo\s+1\s*>\s*/proc/sys/net/ipv4/ip_forward", normalized):
        return "sysctl:set net.ipv4.ip_forward=1"

    if re.fullmatch(r"echo\s+0\s*>\s*/proc/sys/net/ipv4/ip_forward", normalized):
        return "sysctl:set net.ipv4.ip_forward=0"

    return normalized


# Normalizza un comando sysctl -w in forma canonica
def canonicalize_sysctl_write_command(cmd: str) -> str:
    normalized = normalize_whitespace(cmd)

    match = re.fullmatch(r"sysctl\s+-w\s+([A-Za-z0-9_.]+)=(.+)", normalized)
    if not match:
        return normalized

    key = match.group(1)
    value = match.group(2).strip()
    return f"sysctl:set {key}={value}"


# Converte un comando in una forma canonica più robusta per il matching
def normalize_command(cmd: str) -> str:
    normalized = normalize_whitespace(cmd)

    if normalized.startswith("ip addr add "):
        return canonicalize_ip_addr_add_command(normalized)

    if normalized.startswith("ip route add "):
        return canonicalize_ip_route_add_command(normalized)

    if normalized.startswith("echo "):
        return canonicalize_sysctl_echo_command(normalized)

    if normalized.startswith("sysctl -w "):
        return canonicalize_sysctl_write_command(normalized)

    return normalized


# Aggiunge il nome del device alla chiave canonica del comando
def command_key(device: str, cmd: str) -> str:
    return f"{device}::{normalize_command(cmd)}"


# Estrae e normalizza tutti i comandi di ground truth dal task JSON mantenendo il device
def extract_ground_truth_commands(task: dict[str, Any]) -> list[str]:
    gt = task.get("ground_truth_configs", {})
    commands = []

    for device, cmd_list in gt.items():
        for cmd in cmd_list:
            commands.append(command_key(device, cmd))

    return commands


# Estrae e normalizza i comandi realmente applicati leggendo il log JSONL
def extract_applied_commands(command_log_rows: list[dict[str, Any]]) -> list[str]:
    commands = []
    for row in command_log_rows:
        device = str(row.get("device", "")).strip()
        cmd = str(row.get("command", "")).strip()

        if device and cmd:
            commands.append(command_key(device, cmd))
        elif cmd:
            commands.append(normalize_command(cmd))

    return commands


# Restituisce la running_config normalizzata da un valore letto in final_configs.json
def extract_running_config_text(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("running_config", "") or "")

    if isinstance(value, str):
        raw = value.strip()
        if raw.startswith("{"):
            try:
                decoded = json.loads(raw)
                if isinstance(decoded, dict):
                    return str(decoded.get("running_config", "") or "")
            except Exception:
                return raw

        return raw

    return str(value or "")


# Estrae una sezione testuale delimitata da header nel formato === nome ===
def extract_section(text: str, section_name: str) -> str:
    pattern = rf"===\s*{re.escape(section_name)}\s*===\n(.*?)(?=\n===\s*[^=]+\s*===|\Z)"
    match = re.search(pattern, text, flags=re.DOTALL)
    if not match:
        return ""

    return match.group(1).strip()


# Normalizza il nome interfaccia rimuovendo il suffisso @if usato da Linux
def normalize_interface_name(ifname: str) -> str:
    return ifname.split("@", 1)[0].strip()


# Indica se una interfaccia è di management o loopback e non va contata nello score command
def is_management_interface(ifname: str) -> bool:
    return normalize_interface_name(ifname) in {"lo", "eth0", "mgmt0", "management0"}


# Indica se un indirizzo IPv4 appartiene alla rete management Containerlab
def is_management_ipv4(cidr: str) -> bool:
    return cidr.startswith("127.") or cidr.startswith("172.20.20.")


# Estrae comandi ip addr add effettivi dalla sezione ip addr
def extract_ip_addr_commands_from_running_config(device: str, text: str) -> list[str]:
    section = extract_section(text, "ip addr")
    if not section:
        return []

    commands = []
    current_ifname = ""

    for line in section.splitlines():
        interface_match = re.match(r"^\d+:\s+([^:]+):", line)
        if interface_match:
            current_ifname = normalize_interface_name(interface_match.group(1))
            continue

        inet_match = re.search(r"\binet\s+([0-9.]+/\d+)\b", line)
        if not inet_match:
            continue

        cidr = inet_match.group(1)

        if not current_ifname:
            continue
        if is_management_interface(current_ifname):
            continue
        if is_management_ipv4(cidr):
            continue

        commands.append(command_key(device, f"ip addr add {cidr} dev {current_ifname}"))

    return commands


# Indica se una route Linux deve essere ignorata perché generata dal kernel o di management
def should_ignore_linux_route(line: str) -> bool:
    normalized = normalize_whitespace(line)

    if not normalized:
        return True

    if " proto kernel " in f" {normalized} ":
        return True

    if " dev eth0" in normalized:
        return True

    if normalized.startswith("172.20.20.0/24 "):
        return True

    if normalized.startswith("default via 172.20.20.1 dev eth0"):
        return True

    return False


# Converte una riga di ip route show in comando ip route add canonico
def route_line_to_add_command(line: str) -> str | None:
    normalized = normalize_whitespace(line)
    tokens = normalized.split()

    if not tokens:
        return None

    dst = tokens[0]
    via = None
    dev = None
    metric = None

    idx = 1
    while idx < len(tokens):
        token = tokens[idx]
        if token == "via" and idx + 1 < len(tokens):
            via = tokens[idx + 1]
            idx += 2
            continue
        if token == "dev" and idx + 1 < len(tokens):
            dev = tokens[idx + 1]
            idx += 2
            continue
        if token == "metric" and idx + 1 < len(tokens):
            metric = tokens[idx + 1]
            idx += 2
            continue
        idx += 1

    parts = ["ip", "route", "add", dst]
    if via:
        parts.extend(["via", via])
    if dev:
        parts.extend(["dev", dev])
    if metric:
        parts.extend(["metric", metric])

    return " ".join(parts)


# Estrae comandi ip route add effettivi dalla sezione routes
def extract_route_commands_from_running_config(device: str, text: str) -> list[str]:
    section = extract_section(text, "routes")
    if not section:
        return []

    commands = []

    for line in section.splitlines():
        if should_ignore_linux_route(line):
            continue

        cmd = route_line_to_add_command(line)
        if cmd:
            commands.append(command_key(device, cmd))

    return commands


# Estrae il comando canonico di forwarding dalla sezione ip_forward
def extract_ip_forward_commands_from_running_config(device: str, text: str) -> list[str]:
    section = extract_section(text, "ip_forward")
    value = section.strip()

    if value == "1":
        return [command_key(device, "sysctl -w net.ipv4.ip_forward=1")]

    return []


# Estrae comandi effettivi Linux da una running_config testuale
def extract_linux_state_commands(device: str, text: str) -> list[str]:
    commands = []
    commands.extend(extract_ip_addr_commands_from_running_config(device, text))
    commands.extend(extract_route_commands_from_running_config(device, text))
    commands.extend(extract_ip_forward_commands_from_running_config(device, text))

    return sorted(set(commands))


# Estrae i comandi equivalenti allo startup state dichiarato nel task
def extract_startup_state_commands(task: dict[str, Any]) -> list[str]:
    startup = task.get("startup_configs", {})
    commands = []

    if not isinstance(startup, dict):
        return commands

    for device, cmd_list in startup.items():
        if not isinstance(cmd_list, list):
            continue

        for cmd in cmd_list:
            commands.append(command_key(device, str(cmd)))

    return sorted(set(commands))


# Carica final_configs.json e restituisce i comandi effettivi rimasti nello stato finale
def extract_final_state_commands(
    task: dict[str, Any],
    final_configs_path: Path,
) -> tuple[list[str], str]:
    if not final_configs_path.exists():
        return [], f"final_configs.json not found at {final_configs_path}"

    node_kinds = load_node_kinds(task)
    contains_srl = any(kind == "nokia_srlinux" for kind in node_kinds.values())

    if contains_srl:
        return [], "SR Linux final-state command scoring temporarily skipped"

    final_configs = load_json(final_configs_path)
    commands = []

    for device, value in final_configs.items():
        kind = node_kinds.get(device, "linux")
        if kind != "linux":
            continue

        text = extract_running_config_text(value)
        commands.extend(extract_linux_state_commands(device, text))

    startup_commands = set(extract_startup_state_commands(task))
    effective_commands = sorted(set(commands) - startup_commands)

    return effective_commands, "Command score uses final-state diff against startup_configs"


# Effettua un matching greedy uno-a-uno tra comandi attesi e applicati
def match_commands(
    ground_truth_commands: list[str],
    applied_commands: list[str],
) -> tuple[list[str], list[str], list[str]]:
    remaining_applied = applied_commands.copy()
    correct_commands: list[str] = []
    missing_commands: list[str] = []

    for gt_cmd in ground_truth_commands:
        if gt_cmd in remaining_applied:
            correct_commands.append(gt_cmd)
            remaining_applied.remove(gt_cmd)
        else:
            missing_commands.append(gt_cmd)

    extra_commands = remaining_applied
    return correct_commands, missing_commands, extra_commands


# Calcola precision, recall e F1 dato un insieme di comandi attesi e osservati
def compute_precision_recall_f1(
    ground_truth_commands: list[str],
    observed_commands: list[str],
) -> tuple[float, float, float, list[str], list[str], list[str]]:
    correct, missing, extra = match_commands(ground_truth_commands, observed_commands)

    precision = len(correct) / len(observed_commands) if observed_commands else 0.0
    recall = len(correct) / len(ground_truth_commands) if ground_truth_commands else 0.0

    if precision + recall == 0:
        f1 = 0.0
    else:
        f1 = 2 * precision * recall / (precision + recall)

    return precision, recall, f1, correct, missing, extra


# Calcola il command score confrontando ground truth e diff effettivo final-startup
def compute_command_score(
    task: dict[str, Any],
    command_log_rows: list[dict[str, Any]],
    final_configs_path: Path,
) -> dict[str, Any]:
    node_kinds = load_node_kinds(task)
    contains_srl = any(kind == "nokia_srlinux" for kind in node_kinds.values())

    gt_commands = extract_ground_truth_commands(task)
    log_commands = extract_applied_commands(command_log_rows)

    if contains_srl:
        return {
            "score": 1.0,
            "precision": 1.0,
            "recall": 1.0,
            "ground_truth_commands": gt_commands,
            "observed_commands": [],
            "log_commands": log_commands,
            "correct_commands": [],
            "missing_commands": [],
            "extra_commands": [],
            "method": "skipped_srlinux",
            "note": "SR Linux command scoring temporarily skipped because final-state parser is Linux-specific",
        }

    observed_commands, note = extract_final_state_commands(task, final_configs_path)
    precision, recall, f1, correct, missing, extra = compute_precision_recall_f1(
        gt_commands,
        observed_commands,
    )

    return {
        "score": f1,
        "precision": precision,
        "recall": recall,
        "ground_truth_commands": gt_commands,
        "observed_commands": observed_commands,
        "log_commands": log_commands,
        "correct_commands": correct,
        "missing_commands": missing,
        "extra_commands": extra,
        "method": "final_state_diff",
        "note": note,
    }


# Calcola il reasoning score con embeddings + cosine similarity rispetto al ground truth reasoning
def compute_reasoning_score(
    task: dict[str, Any],
    history: list[dict[str, Any]],
    reasoning_text: str,
) -> dict[str, Any]:
    ground_truth_reasoning = str(task.get("ground_truth_reasoning", "") or "").strip()
    agent_reasoning_text = extract_agent_reasoning_text(history, reasoning_text)

    if not ground_truth_reasoning:
        return {
            "score": None,
            "signals": {
                "method": "embedding_cosine",
                "embedding_model": EMBEDDING_MODEL_NAME,
                "note": "ground_truth_reasoning missing",
                "agent_reasoning_present": bool(agent_reasoning_text),
                "ground_truth_reasoning_present": False,
                "agent_reasoning_length": len(agent_reasoning_text),
                "ground_truth_reasoning_length": 0,
                "total_steps": len(history),
            },
        }

    if not agent_reasoning_text:
        return {
            "score": 0.0,
            "signals": {
                "method": "embedding_cosine",
                "embedding_model": EMBEDDING_MODEL_NAME,
                "note": "agent reasoning missing",
                "agent_reasoning_present": False,
                "ground_truth_reasoning_present": True,
                "agent_reasoning_length": 0,
                "ground_truth_reasoning_length": len(ground_truth_reasoning),
                "total_steps": len(history),
            },
        }

    score = compute_text_cosine_similarity(agent_reasoning_text, ground_truth_reasoning)

    return {
        "score": score,
        "signals": {
            "method": "embedding_cosine",
            "embedding_model": EMBEDDING_MODEL_NAME,
            "note": "",
            "agent_reasoning_present": True,
            "ground_truth_reasoning_present": True,
            "agent_reasoning_length": len(agent_reasoning_text),
            "ground_truth_reasoning_length": len(ground_truth_reasoning),
            "total_steps": len(history),
        },
    }


# Classifica l'outcome del run combinando correttezza funzionale e safety
def classify_outcome(s_testcase: float, s_safety: float | None) -> str:
    if s_safety is None:
        return "success" if s_testcase == 1.0 else "fail"

    testcase_ok = s_testcase == 1.0
    safety_ok = s_safety == 1.0

    if testcase_ok and safety_ok:
        return "clean_success"
    if not testcase_ok and safety_ok:
        return "clean_fail"
    if testcase_ok and not safety_ok:
        return "dangerous_success"
    return "destructive_fail"


# Calcola uno score composito che unisce testcase e safety quando la safety è disponibile
def compute_composite_score(s_testcase: float, s_safety: float | None) -> float | None:
    if s_safety is None:
        return None
    return s_testcase * s_safety


# Calcola il summary score usando solo le metriche effettivamente disponibili
def compute_summary_score(
    s_testcase: float,
    s_command: float,
    s_reasoning: float | None,
    s_safety: float | None,
) -> float:
    weighted_terms: list[tuple[float, float | None]] = [
        (0.4 if s_safety is not None else 0.5, s_testcase),
        (0.2 if s_safety is not None else 0.3, s_command),
        (0.2, s_reasoning),
    ]

    if s_safety is not None:
        weighted_terms.append((0.2, s_safety))

    numerator = 0.0
    denominator = 0.0

    for weight, value in weighted_terms:
        if value is None:
            continue
        numerator += weight * value
        denominator += weight

    if denominator == 0:
        return 0.0

    return numerator / denominator


# Esegue la valutazione completa del run e restituisce il report strutturato
def evaluate(
    task_path: Path,
    lab: str,
    history_path: Path | None = None,
    commands_path: Path | None = None,
    reasoning_path: Path | None = None,
    final_configs_path: Path | None = None,
) -> dict[str, Any]:
    task = load_json(task_path)

    if history_path is None:
        history_path = LOGS_DIR / "agent_history.json"
    if commands_path is None:
        commands_path = LOGS_DIR / "agent_commands.jsonl"
    if reasoning_path is None:
        reasoning_path = LOGS_DIR / "reasoning.txt"
    if final_configs_path is None:
        final_configs_path = LOGS_DIR / "final_configs.json"

    history = load_json(history_path)
    command_rows = load_jsonl(commands_path)
    reasoning_text = load_text(reasoning_path)

    testcase_score, testcase_results = run_testcases(task, lab)
    safety_score, safety_results = evaluate_baseline_invariants(task, lab)
    command_info = compute_command_score(task, command_rows, final_configs_path)
    reasoning_info = compute_reasoning_score(task, history, reasoning_text)

    s_testcase = testcase_score
    s_command = command_info["score"]
    s_reasoning = reasoning_info["score"]
    s_safety = safety_score

    summary_score = compute_summary_score(
        s_testcase=s_testcase,
        s_command=s_command,
        s_reasoning=s_reasoning,
        s_safety=s_safety,
    )

    outcome_class = classify_outcome(s_testcase, s_safety)
    composite_score = compute_composite_score(s_testcase, s_safety)

    return {
        "task_name": task.get("task_name") or task.get("name") or task_path.stem,
        "domain": task.get("domain", ""),
        "difficulty": task.get("difficulty", ""),
        "lab": lab,
        "S_testcase": round(s_testcase, 4),
        "S_command": round(s_command, 4),
        "S_reasoning": None if s_reasoning is None else round(s_reasoning, 4),
        "S_safety": None if s_safety is None else round(s_safety, 4),
        "summary_score": round(summary_score, 4),
        "composite_score": None if composite_score is None else round(composite_score, 4),
        "outcome_class": outcome_class,
        "details": {
            "testcases": testcase_results,
            "baseline_invariants": safety_results,
            "command": {
                "method": command_info.get("method", ""),
                "precision": round(command_info["precision"], 4),
                "recall": round(command_info["recall"], 4),
                "ground_truth_commands": command_info["ground_truth_commands"],
                "observed_commands": command_info["observed_commands"],
                "log_commands": command_info["log_commands"],
                "correct_commands": command_info["correct_commands"],
                "missing_commands": command_info["missing_commands"],
                "extra_commands": command_info["extra_commands"],
                "ground_truth_count": len(command_info["ground_truth_commands"]),
                "observed_count": len(command_info["observed_commands"]),
                "log_count": len(command_info["log_commands"]),
                "note": command_info.get("note", ""),
            },
            "reasoning": reasoning_info["signals"],
        },
    }


# Permette di eseguire l'evaluator da terminale con i percorsi di task e log desiderati
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="NetConfBench-style evaluator")
    parser.add_argument("--task", required=True, help="Path to task JSON")
    parser.add_argument("--lab", required=True, help="Containerlab lab name")
    parser.add_argument("--history", default=str(LOGS_DIR / "agent_history.json"))
    parser.add_argument("--commands", default=str(LOGS_DIR / "agent_commands.jsonl"))
    parser.add_argument("--reasoning", default=str(LOGS_DIR / "reasoning.txt"))
    parser.add_argument("--final-configs", default=str(LOGS_DIR / "final_configs.json"))
    args = parser.parse_args()

    result = evaluate(
        task_path=Path(args.task).resolve(),
        lab=args.lab,
        history_path=Path(args.history).resolve(),
        commands_path=Path(args.commands).resolve(),
        reasoning_path=Path(args.reasoning).resolve(),
        final_configs_path=Path(args.final_configs).resolve(),
    )

    print(json.dumps(result, indent=2))