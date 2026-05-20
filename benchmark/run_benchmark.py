import argparse
import csv
import json
import os
import re
import shutil
import signal
import subprocess
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]

RESULTS_DIR = PROJECT_ROOT / "results"
RESULTS_JSON = RESULTS_DIR / "results.json"
RESULTS_CSV = RESULTS_DIR / "results.csv"
RUNS_DIR = RESULTS_DIR / "runs"

LOGS_DIR = PROJECT_ROOT / "logs"

RUNTIME_LOG_FILES = [
    "agent_history.json",
    "reasoning.txt",
    "agent_commands.jsonl",
    "final_configs.json",
    "run_result.json",
]

MODELS_FILE = PROJECT_ROOT / "benchmark" / "models.json"
TASKS_FILE = PROJECT_ROOT / "benchmark" / "tasks.json"


# Carica la lista dei modelli da benchmark/models.json
def load_models() -> list[tuple[str, str]]:
    if not MODELS_FILE.exists():
        raise FileNotFoundError(f"Models config not found: {MODELS_FILE}")

    with open(MODELS_FILE) as f:
        models_config = json.load(f)

    models = []
    for item in models_config:
        provider = item.get("provider")
        model = item.get("model")
        if not provider or not model:
            raise ValueError(f"Invalid model entry in {MODELS_FILE}: {item}")
        models.append((provider, model))

    return models


# Carica la lista dei task da benchmark/tasks.json
def load_tasks() -> list[str]:
    if not TASKS_FILE.exists():
        raise FileNotFoundError(f"Tasks config not found: {TASKS_FILE}")

    with open(TASKS_FILE) as f:
        tasks = json.load(f)

    if not isinstance(tasks, list) or not all(isinstance(t, str) for t in tasks):
        raise ValueError(f"{TASKS_FILE} must contain a JSON list of task paths")

    return tasks


# Converte una stringa in un nome sicuro per directory e file
def safe_name(value: str) -> str:
    value = str(value).strip()
    value = value.replace("/", "-")
    value = value.replace("\\", "-")
    value = value.replace("|", "-")
    value = value.replace(":", "-")
    value = value.replace(" ", "_")
    value = re.sub(r"[^A-Za-z0-9_.=-]+", "-", value)
    value = re.sub(r"-+", "-", value)
    value = re.sub(r"_+", "_", value)
    return value.strip("-_")


# Risolve la modalità agente effettiva e se il rollback deve essere abilitato
def resolve_agent_mode(mode: str) -> tuple[str, bool]:
    if mode == "react_rollback":
        return "react", True

    return mode, False


# Restituisce il nome leggibile della modalità agente
def display_mode(mode: str) -> str:
    if mode == "react_rollback":
        return "react con rollback"

    return mode


# Esegue un agente su un task specifico
def run_agent(
    lab: str,
    task: str,
    provider: str,
    model: str,
    mode: str,
    timeout_s: int | None = None,
    planner_provider: str | None = None,
    planner_model: str | None = None,
    executor_provider: str | None = None,
    executor_model: str | None = None,
) -> tuple[bool, str]:
    agent_mode, enable_rollback = resolve_agent_mode(mode)

    cmd = [
        "python",
        "-m",
        "agent.agent_runner",
        "--lab",
        lab,
        "--task",
        task,
        "--provider",
        provider,
        "--model",
        model,
        "--max-steps",
        "25",
        "--agent-mode",
        agent_mode,
    ]

    if enable_rollback:
        cmd.append("--enable-rollback")

    if planner_provider:
        cmd.extend(["--planner-provider", planner_provider])
    if planner_model:
        cmd.extend(["--planner-model", planner_model])
    if executor_provider:
        cmd.extend(["--executor-provider", executor_provider])
    if executor_model:
        cmd.extend(["--executor-model", executor_model])

    print(f"\n=== Running {provider}:{model} [{display_mode(mode)}] on {task} ===")

    process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )

    try:
        stdout, stderr = process.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGTERM)
        try:
            stdout, stderr = process.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            stdout, stderr = process.communicate()

        print(f"TIMEOUT running agent after {timeout_s}s")
        if stderr:
            print(stderr)
        if stdout:
            print("STDOUT:")
            print(stdout)
        return False, "agent_timeout"

    if process.returncode != 0:
        print("ERROR running agent:")
        print(stderr)
        if stdout:
            print("STDOUT:")
            print(stdout)
        return False, "agent_failed"

    return True, ""


# Esegue l'evaluator sul run appena completato
def run_evaluator(task: str, lab: str) -> dict[str, Any] | None:
    cmd = [
        "python",
        "benchmark/evaluator.py",
        "--task",
        task,
        "--lab",
        lab,
    ]

    result = subprocess.run(cmd, capture_output=True, text=True)

    if result.returncode != 0:
        print("ERROR running evaluator:")
        print(result.stderr)
        return None

    try:
        return json.loads(result.stdout)
    except Exception:
        print("ERROR parsing evaluator output:")
        print(result.stdout)
        return None


# Estrae il nome del lab dal task JSON
def extract_lab_from_task(task_path: str) -> str:
    with open(task_path) as f:
        task = json.load(f)

    lab_file = task.get("lab_file")
    if not lab_file:
        raise ValueError(f"Task {task_path} is missing 'lab_file'")

    return Path(lab_file).stem


# Carica il risultato runtime del run agente se disponibile
def load_run_result() -> dict[str, Any]:
    run_result_path = LOGS_DIR / "run_result.json"
    if not run_result_path.exists():
        return {}

    with open(run_result_path) as f:
        return json.load(f)


# Rimuove i log runtime del run precedente per evitare risultati stale
def clear_runtime_logs() -> None:
    for filename in RUNTIME_LOG_FILES:
        path = LOGS_DIR / filename
        if path.exists():
            path.unlink()


# Esegue il write del CSV aggregato dei risultati
def write_csv(results: list[dict[str, Any]]) -> None:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)

    fieldnames = [
        "task",
        "lab",
        "domain",
        "difficulty",
        "provider",
        "model",
        "mode",
        "summary_score",
        "composite_score",
        "S_testcase",
        "S_command",
        "S_reasoning",
        "S_safety",
        "outcome_class",
        "passed_testcases",
        "total_testcases",
        "passed_invariants",
        "total_invariants",
        "command_precision",
        "command_recall",
        "steps",
        "total_tokens",
        "estimated_cost_usd",
    ]

    with open(RESULTS_CSV, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in results:
            writer.writerow(row)


# Stampa un ranking aggregato per modello e modalità
def print_ranking(results: list[dict[str, Any]]) -> None:
    grouped = defaultdict(list)

    for row in results:
        if row["summary_score"] is None:
            continue
        key = f"{row['provider']}:{row['model']}:{row['mode']}"
        grouped[key].append(row["summary_score"])

    ranking = []
    for model_name, scores in grouped.items():
        avg_score = sum(scores) / len(scores)
        ranking.append((model_name, avg_score, len(scores)))

    ranking.sort(key=lambda x: x[1], reverse=True)

    print("\n=== MODEL RANKING (average summary_score) ===")
    for idx, (model_name, avg_score, n) in enumerate(ranking, start=1):
        print(f"{idx}. {model_name} | avg={avg_score:.3f} | runs={n}")


# Distrugge e ricrea il lab per partire da uno stato pulito
def reset_lab(lab: str) -> None:
    lab_file = f"labs/{lab}.yml"

    print(f"\n--- Resetting lab {lab} ---")
    subprocess.run(["containerlab", "destroy", "-t", lab_file], check=False)
    subprocess.run(["containerlab", "deploy", "-t", lab_file], check=False)


# Salva gli artefatti del run in una directory dedicata
def save_run_artifacts(
    task: str,
    provider: str,
    model: str,
    mode: str,
    eval_result: dict[str, Any] | None = None,
) -> None:
    task_name = Path(task).stem

    safe_provider = safe_name(provider)
    safe_model = safe_name(model)
    safe_mode = safe_name(mode)

    run_dir = RUNS_DIR / task_name / f"{safe_provider}__{safe_model}__{safe_mode}"
    run_dir.mkdir(parents=True, exist_ok=True)

    files_to_copy = [
        "agent_history.json",
        "reasoning.txt",
        "agent_commands.jsonl",
        "final_configs.json",
        "run_result.json",
    ]

    for filename in files_to_copy:
        src = LOGS_DIR / filename
        dst = run_dir / filename
        if src.exists():
            shutil.copy2(src, dst)

    if eval_result is not None:
        (run_dir / "evaluation.json").write_text(json.dumps(eval_result, indent=2))


# Costruisce una riga risultati per un run agente fallito
def build_failed_entry(
    task: str,
    lab: str,
    provider: str,
    model: str,
    mode: str,
    run_result: dict[str, Any] | None = None,
    outcome_class: str = "agent_failed",
) -> dict[str, Any]:
    run_result = run_result or {}

    return {
        "task": Path(task).name,
        "lab": lab,
        "domain": "",
        "difficulty": "",
        "provider": run_result.get("provider", provider),
        "model": run_result.get("model", model),
        "mode": display_mode(mode),
        "summary_score": None,
        "composite_score": None,
        "S_testcase": None,
        "S_command": None,
        "S_reasoning": None,
        "S_safety": None,
        "outcome_class": outcome_class,
        "passed_testcases": 0,
        "total_testcases": 0,
        "passed_invariants": 0,
        "total_invariants": 0,
        "command_precision": None,
        "command_recall": None,
        "steps": 0,
        "total_tokens": run_result.get("total_tokens", 0),
        "estimated_cost_usd": run_result.get("estimated_cost_usd", 0.0),
    }


# Costruisce una riga risultati per un run valutato correttamente
def build_success_entry(
    task: str,
    lab: str,
    provider: str,
    model: str,
    mode: str,
    eval_result: dict[str, Any],
    run_result: dict[str, Any],
) -> dict[str, Any]:
    testcases = eval_result["details"]["testcases"]
    passed_testcases = sum(1 for tc in testcases if tc["passed"])
    total_testcases = len(testcases)

    invariants = eval_result["details"].get("baseline_invariants", [])
    passed_invariants = sum(1 for inv in invariants if inv["passed"])
    total_invariants = len(invariants)

    command_details = eval_result["details"]["command"]
    reasoning_details = eval_result["details"]["reasoning"]

    return {
        "task": Path(task).name,
        "lab": lab,
        "domain": eval_result.get("domain", ""),
        "difficulty": eval_result.get("difficulty", ""),
        "provider": run_result.get("provider", provider),
        "model": run_result.get("model", model),
        "mode": display_mode(mode),
        "summary_score": eval_result["summary_score"],
        "composite_score": eval_result.get("composite_score"),
        "S_testcase": eval_result["S_testcase"],
        "S_command": eval_result["S_command"],
        "S_reasoning": eval_result["S_reasoning"],
        "S_safety": eval_result.get("S_safety"),
        "outcome_class": eval_result.get("outcome_class", ""),
        "passed_testcases": passed_testcases,
        "total_testcases": total_testcases,
        "passed_invariants": passed_invariants,
        "total_invariants": total_invariants,
        "command_precision": command_details["precision"],
        "command_recall": command_details["recall"],
        "steps": reasoning_details["total_steps"],
        "total_tokens": run_result.get("total_tokens", 0),
        "estimated_cost_usd": run_result.get("estimated_cost_usd", 0.0),
    }


# Esegue il benchmark su task, modelli e modalità richieste
def main() -> None:
    parser = argparse.ArgumentParser(description="Run NetConfBench benchmark")
    parser.add_argument("--task", help="Run only one task")
    parser.add_argument(
        "--mode",
        choices=[
            "react",
            "react_rollback",
            "single_turn",
            "planner_executor",
            "all",
            "all_with_rollback",
        ],
        default="all",
        help="Agent mode to run",
    )
    parser.add_argument("--planner-provider")
    parser.add_argument("--planner-model")
    parser.add_argument("--executor-provider")
    parser.add_argument("--executor-model")
    parser.add_argument(
        "--agent-timeout",
        type=int,
        default=None,
        help="Timeout in seconds for each agent run",
    )
    args = parser.parse_args()

    models = load_models()

    if args.task:
        tasks = [args.task]
    else:
        tasks = load_tasks()

    if args.mode == "all":
        modes = ["react", "single_turn", "planner_executor"]
    elif args.mode == "all_with_rollback":
        modes = [
            "react",
            "react_rollback",
            "single_turn",
            "planner_executor",
        ]
    else:
        modes = [args.mode]

    results: list[dict[str, Any]] = []

    for task in tasks:
        lab = extract_lab_from_task(task)

        for mode in modes:
            agent_mode, _enable_rollback = resolve_agent_mode(mode)

            for provider, model in models:
                reset_lab(lab)

                planner_provider = args.planner_provider if agent_mode == "planner_executor" else None
                planner_model = args.planner_model if agent_mode == "planner_executor" else None
                executor_provider = args.executor_provider if agent_mode == "planner_executor" else None
                executor_model = args.executor_model if agent_mode == "planner_executor" else None

                clear_runtime_logs()

                ok, failure_class = run_agent(
                    lab=lab,
                    task=task,
                    provider=provider,
                    model=model,
                    mode=mode,
                    timeout_s=args.agent_timeout,
                    planner_provider=planner_provider,
                    planner_model=planner_model,
                    executor_provider=executor_provider,
                    executor_model=executor_model,
                )

                run_result = load_run_result()

                if not ok:
                    entry = build_failed_entry(
                        task,
                        lab,
                        provider,
                        model,
                        mode,
                        run_result,
                        outcome_class=failure_class,
                    )
                    results.append(entry)
                    save_run_artifacts(task, entry["provider"], entry["model"], entry["mode"], None)
                    time.sleep(3)
                    continue

                eval_result = run_evaluator(task, lab)

                if not eval_result:
                    entry = build_failed_entry(task, lab, provider, model, mode, run_result)
                    entry["outcome_class"] = "evaluator_failed"
                    results.append(entry)
                    save_run_artifacts(task, entry["provider"], entry["model"], entry["mode"], None)
                    time.sleep(3)
                    continue

                entry = build_success_entry(
                    task=task,
                    lab=lab,
                    provider=provider,
                    model=model,
                    mode=mode,
                    eval_result=eval_result,
                    run_result=run_result,
                )

                results.append(entry)
                save_run_artifacts(task, entry["provider"], entry["model"], entry["mode"], eval_result)
                time.sleep(3)

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    RESULTS_JSON.write_text(json.dumps(results, indent=2))
    write_csv(results)

    print("\n=== BENCHMARK RESULTS ===")
    for r in results:
        print(
            f"{r['task']} | {r['provider']}:{r['model']} | {r['mode']} "
            f"| score={r['summary_score']}"
        )

    print_ranking(results)

    print(f"\nSaved JSON to {RESULTS_JSON}")
    print(f"Saved CSV  to {RESULTS_CSV}")
    print(f"Saved per-run artifacts under {RUNS_DIR}")


if __name__ == "__main__":
    main()
