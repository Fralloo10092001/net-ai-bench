import argparse
import csv
import json
import shutil
import subprocess
import time
from collections import defaultdict
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]

RESULTS_DIR = PROJECT_ROOT / "results"
RESULTS_JSON = RESULTS_DIR / "results.json"
RESULTS_CSV = RESULTS_DIR / "results.csv"
RUNS_DIR = RESULTS_DIR / "runs"

LOGS_DIR = PROJECT_ROOT / "logs"

MODELS_FILE = PROJECT_ROOT / "benchmark" / "models.json"
TASKS_FILE = PROJECT_ROOT / "benchmark" / "tasks.json"

#Legge models.json, valida le entry e ritorna lista di tuple (provider, model)
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

#Legge tasks.json, verifica che sia lista di stringhe
def load_tasks() -> list[str]:
    if not TASKS_FILE.exists():
        raise FileNotFoundError(f"Tasks config not found: {TASKS_FILE}")

    with open(TASKS_FILE) as f:
        tasks = json.load(f)

    if not isinstance(tasks, list) or not all(isinstance(t, str) for t in tasks):
        raise ValueError(f"{TASKS_FILE} must contain a JSON list of task paths")

    return tasks

#Costruisce il comando CLI: python -m agent.agent_runner ...
#Lancia il singolo agente su un task.
def run_agent(lab: str, task: str, provider: str, model: str) -> bool:
    cmd = [
        "python", "-m", "agent.agent_runner",
        "--lab", lab,
        "--task", task,
        "--provider", provider,
        "--model", model,
        "--max-steps", "25",
    ]

    print(f"\n=== Running {provider}:{model} on {task} ===")

    result = subprocess.run(cmd, capture_output=True, text=True)

    if result.returncode != 0:
        print("ERROR running agent:")
        print(result.stderr)
        return False

    return True

#Lancia benchmark/evaluator.py e prova a fare parse del suo stdout JSON
def run_evaluator(task: str, lab: str):
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

#Legge il task e ne ricava il nome del lab dal campo lab_file
def extract_lab_from_task(task_path: str) -> str:
    with open(task_path) as f:
        task = json.load(f)

    lab_file = task.get("lab_file")
    if not lab_file:
        raise ValueError(f"Task {task_path} is missing 'lab_file'")

    return Path(lab_file).stem

#Scrive il CSV aggregato con i vari campi
def write_csv(results: list[dict]) -> None:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)

    fieldnames = [
        "task",
        "lab",
        "provider",
        "model",
        "summary_score",
        "S_testcase",
        "S_command",
        "S_reasoning",
        "passed_testcases",
        "total_testcases",
        "command_precision",
        "command_recall",
        "steps",
    ]

    with open(RESULTS_CSV, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in results:
            writer.writerow(row)


#Raggruppa per provider:model, media summary_score, ordina decrescente
def print_ranking(results: list[dict]) -> None:
    grouped = defaultdict(list)

    for row in results:
        key = f"{row['provider']}:{row['model']}"
        grouped[key].append(row["summary_score"])

    ranking = []
    for model_name, scores in grouped.items():
        avg_score = sum(scores) / len(scores)
        ranking.append((model_name, avg_score, len(scores)))

    ranking.sort(key=lambda x: x[1], reverse=True)

    print("\n=== MODEL RANKING (average summary_score) ===")
    for idx, (model_name, avg_score, n) in enumerate(ranking, start=1):
        print(f"{idx}. {model_name} | avg={avg_score:.3f} | runs={n}")


#Fa: containerlab destroy -t labs/<lab>.yml e poi containerlab deploy -t labs/<lab>.yml
def reset_lab(lab: str) -> None:
    lab_file = f"labs/{lab}.yml"

    print(f"\n--- Resetting lab {lab} ---")
    subprocess.run(["containerlab", "destroy", "-t", lab_file])
    subprocess.run(["containerlab", "deploy", "-t", lab_file])

#Copia da logs/ ai risultati persistenti del run
def save_run_artifacts(task: str, provider: str, model: str, eval_result: dict | None = None) -> None:
    task_name = Path(task).stem
    safe_model = model.replace("/", "-")
    run_dir = RUNS_DIR / task_name / f"{provider}__{safe_model}"
    run_dir.mkdir(parents=True, exist_ok=True)

    files_to_copy = [
        "agent_history.json",
        "reasoning.txt",
        "agent_commands.jsonl",
        "final_configs.json",
    ]

    for filename in files_to_copy:
        src = LOGS_DIR / filename
        dst = run_dir / filename
        if src.exists():
            shutil.copy2(src, dst)

    if eval_result is not None:
        (run_dir / "evaluation.json").write_text(json.dumps(eval_result, indent=2))

def main():
    parser = argparse.ArgumentParser(description="Run NetConfBench benchmark")
    parser.add_argument("--task", help="Run only one task")
    args = parser.parse_args()

    models = load_models()

    if args.task:
        tasks = [args.task]
    else:
        tasks = load_tasks()

    results = []

    for task in tasks:
        lab = extract_lab_from_task(task)
        reset_lab(lab)

        for provider, model in models:
            ok = run_agent(lab, task, provider, model)
            if not ok:
                continue

            eval_result = run_evaluator(task, lab)
            if not eval_result:
                # salva comunque gli artifact del run, anche se l'evaluator è fallito
                save_run_artifacts(task, provider, model, None)
                continue

            testcases = eval_result["details"]["testcases"]
            passed_testcases = sum(1 for tc in testcases if tc["passed"])
            total_testcases = len(testcases)

            command_details = eval_result["details"]["command"]
            reasoning_details = eval_result["details"]["reasoning"]

            entry = {
                "task": Path(task).name,
                "lab": lab,
                "provider": provider,
                "model": model,
                "summary_score": eval_result["summary_score"],
                "S_testcase": eval_result["S_testcase"],
                "S_command": eval_result["S_command"],
                "S_reasoning": eval_result["S_reasoning"],
                "passed_testcases": passed_testcases,
                "total_testcases": total_testcases,
                "command_precision": command_details["precision"],
                "command_recall": command_details["recall"],
                "steps": reasoning_details["total_steps"],
            }

            results.append(entry)
            save_run_artifacts(task, provider, model, eval_result)
            time.sleep(10)

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    RESULTS_JSON.write_text(json.dumps(results, indent=2))
    write_csv(results)

    print("\n=== BENCHMARK RESULTS ===")
    for r in results:
        print(
            f"{r['task']} | {r['provider']}:{r['model']} "
            f"| score={r['summary_score']:.3f}"
        )

    print_ranking(results)

    print(f"\nSaved JSON to {RESULTS_JSON}")
    print(f"Saved CSV  to {RESULTS_CSV}")
    print(f"Saved per-run artifacts under {RUNS_DIR}")


if __name__ == "__main__":
    main()