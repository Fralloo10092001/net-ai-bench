import argparse
import csv
import json
import shutil
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

MODELS_FILE = PROJECT_ROOT / "benchmark" / "models.json"
TASKS_FILE = PROJECT_ROOT / "benchmark" / "tasks.json"

AGENT_MODES = ["react", "single_turn", "planner_executor"]


# Legge models.json e restituisce lista di tuple (provider, model)
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


# Legge tasks.json e verifica che sia una lista di path
def load_tasks() -> list[str]:
    if not TASKS_FILE.exists():
        raise FileNotFoundError(f"Tasks config not found: {TASKS_FILE}")

    with open(TASKS_FILE) as f:
        tasks = json.load(f)

    if not isinstance(tasks, list) or not all(isinstance(t, str) for t in tasks):
        raise ValueError(f"{TASKS_FILE} must contain a JSON list of task paths")

    return tasks


# Esegue il runner agente per una specifica modalità
def run_agent(lab: str, task: str, provider: str, model: str, mode: str) -> bool:
    cmd = [
        "python", "-m", "agent.agent_runner",
        "--lab", lab,
        "--task", task,
        "--provider", provider,
        "--model", model,
        "--max-steps", "25",
        "--agent-mode", mode,
    ]

    print(f"\n=== Running {provider}:{model} [{mode}] on {task} ===")

    result = subprocess.run(cmd, capture_output=True, text=True)

    if result.returncode != 0:
        print("ERROR running agent:")
        print(result.stderr)
        return False

    return True


# Esegue evaluator.py e fa parse dello stdout JSON
def run_evaluator(task: str, lab: str) -> dict[str, Any] | None:
    cmd = [
        "python", "benchmark/evaluator.py",
        "--task", task,
        "--lab", lab,
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


# Ricava il nome del lab dal campo lab_file nel task
def extract_lab_from_task(task_path: str) -> str:
    with open(task_path) as f:
        task = json.load(f)

    lab_file = task.get("lab_file")
    if not lab_file:
        raise ValueError(f"Task {task_path} is missing 'lab_file'")

    return Path(lab_file).stem


# Legge run_result.json prodotto da agent_runner
def load_run_result() -> dict[str, Any]:
    run_result_path = LOGS_DIR / "run_result.json"
    if not run_result_path.exists():
        return {}

    try:
        with open(run_result_path) as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
        return {}
    except Exception:
        return {}


# Fa reset del lab prima di ogni run
def reset_lab(lab: str) -> None:
    lab_file = f"labs/{lab}.yml"

    print(f"\n--- Resetting lab {lab} ---")
    subprocess.run(["containerlab", "destroy", "-t", lab_file], check=False)
    subprocess.run(["containerlab", "deploy", "-t", lab_file], check=False)


# Salva gli artifact del run in results/runs/... separati anche per mode
def save_run_artifacts(
    task: str,
    provider: str,
    model: str,
    mode: str,
    eval_result: dict[str, Any] | None = None,
) -> None:
    task_name = Path(task).stem
    safe_model = model.replace("/", "-")
    run_dir = RUNS_DIR / task_name / mode / f"{provider}__{safe_model}"
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


# Scrive il CSV aggregato
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
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
        "estimated_cost_usd",
    ]

    with open(RESULTS_CSV, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in results:
            writer.writerow(row)


# Ranking per provider:model:mode sulla media di summary_score
def print_ranking(results: list[dict[str, Any]]) -> None:
    grouped = defaultdict(list)

    for row in results:
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


def main():
    parser = argparse.ArgumentParser(description="Run NetConfBench benchmark")
    parser.add_argument("--task", help="Run only one task")
    parser.add_argument(
        "--mode",
        choices=AGENT_MODES + ["all"],
        default="all",
        help="Run only one agent mode or all modes",
    )
    args = parser.parse_args()

    models = load_models()

    if args.task:
        tasks = [args.task]
    else:
        tasks = load_tasks()

    if args.mode == "all":
        modes = AGENT_MODES
    else:
        modes = [args.mode]

    results: list[dict[str, Any]] = []

    for task in tasks:
        lab = extract_lab_from_task(task)

        for provider, model in models:
            for mode in modes:
                reset_lab(lab)

                ok = run_agent(lab, task, provider, model, mode)
                if not ok:
                    save_run_artifacts(task, provider, model, mode, None)
                    continue

                eval_result = run_evaluator(task, lab)
                run_result = load_run_result()

                if not eval_result:
                    save_run_artifacts(task, provider, model, mode, None)
                    continue

                testcases = eval_result["details"].get("testcases", [])
                baseline_invariants = eval_result["details"].get("baseline_invariants", [])
                command_details = eval_result["details"].get("command", {})
                reasoning_details = eval_result["details"].get("reasoning", {})

                passed_testcases = sum(1 for tc in testcases if tc.get("passed"))
                total_testcases = len(testcases)

                passed_invariants = sum(1 for inv in baseline_invariants if inv.get("passed"))
                total_invariants = len(baseline_invariants)

                entry = {
                    "task": Path(task).name,
                    "lab": lab,
                    "domain": eval_result.get("domain", ""),
                    "difficulty": eval_result.get("difficulty", ""),
                    "provider": provider,
                    "model": model,
                    "mode": run_result.get("mode", mode),
                    "summary_score": eval_result.get("summary_score"),
                    "composite_score": eval_result.get("composite_score"),
                    "S_testcase": eval_result.get("S_testcase"),
                    "S_command": eval_result.get("S_command"),
                    "S_reasoning": eval_result.get("S_reasoning"),
                    "S_safety": eval_result.get("S_safety"),
                    "outcome_class": eval_result.get("outcome_class", ""),
                    "passed_testcases": passed_testcases,
                    "total_testcases": total_testcases,
                    "passed_invariants": passed_invariants,
                    "total_invariants": total_invariants,
                    "command_precision": command_details.get("precision"),
                    "command_recall": command_details.get("recall"),
                    "steps": reasoning_details.get("total_steps"),
                    "prompt_tokens": run_result.get("prompt_tokens", 0),
                    "completion_tokens": run_result.get("completion_tokens", 0),
                    "total_tokens": run_result.get("total_tokens", 0),
                    "estimated_cost_usd": run_result.get("estimated_cost_usd", 0.0),
                }

                results.append(entry)
                save_run_artifacts(task, provider, model, mode, eval_result)

                time.sleep(2)

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    RESULTS_JSON.write_text(json.dumps(results, indent=2))
    write_csv(results)

    print("\n=== BENCHMARK RESULTS ===")
    for r in results:
        print(
            f"{r['task']} | {r['provider']}:{r['model']} | {r['mode']} "
            f"| score={r['summary_score']:.3f} | safety={r['S_safety']}"
        )

    print_ranking(results)

    print(f"\nSaved JSON to {RESULTS_JSON}")
    print(f"Saved CSV  to {RESULTS_CSV}")
    print(f"Saved per-run artifacts under {RUNS_DIR}")


if __name__ == "__main__":
    main()