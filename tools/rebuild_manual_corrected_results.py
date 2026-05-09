import csv
import json
import re
import shutil
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
RESULTS = ROOT / "results"
RUNS = RESULTS / "runs"
SNAPSHOTS = ROOT / "results_snapshots"

OUT_DIR = RESULTS / "manual_complete_corrected"
OUT_RUNS = OUT_DIR / "runs"
OUT_RESULTS_JSON = OUT_DIR / "results.json"
OUT_RESULTS_CSV = OUT_DIR / "results.csv"
OUT_CORRECTIONS = OUT_DIR / "ping_loss_corrections.json"
OUT_MANIFEST = OUT_DIR / "manifest.json"

ROOT_RESULTS_JSON = RESULTS / "results.json"
ROOT_RESULTS_CSV = RESULTS / "results.csv"
ROOT_ANALYSIS = RESULTS / "manual_results_analysis.txt"

TASK_ORDER = [
    "floating_dnat",
    "floating_haproxy",
    "frr_basic",
    "fw_isav",
    "fw_ports",
    "isis_ipv6",
    "isis_misconfigured",
    "routing_linear",
    "routing_linear_linkdown",
    "ssh_filtering",
    "vrf_isolation",
]

MODES = ["react", "react con rollback", "single_turn", "planner_executor"]

ACTIVE_SIMPLE_MODELS = {
    ("google", "gemini-2.5-flash"),
    ("deepseek", "deepseek-v4-pro"),
    ("openai", "gpt-5.4-mini"),
}

ACTIVE_PLANNER_MODELS = {
    ("google/google", "gemini-2.5-flash | gemini-2.5-flash"),
    ("deepseek/deepseek", "deepseek-v4-pro | deepseek-v4-pro"),
    ("openai/openai", "gpt-5.4-mini | gpt-5.4-mini"),
}

CSV_FIELDS = [
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


def load_json(path: Path) -> Any:
    with open(path) as f:
        return json.load(f)


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2))


def row_key(row: dict[str, Any]) -> tuple[str, str, str, str]:
    return (
        Path(row["task"]).stem,
        str(row["provider"]),
        str(row["model"]),
        str(row["mode"]),
    )


def run_dir_name(row: dict[str, Any]) -> str:
    return "__".join(
        [
            safe_name(str(row["provider"])),
            safe_name(str(row["model"])),
            safe_name(str(row["mode"])),
        ]
    )


def is_active_row(row: dict[str, Any]) -> bool:
    pair = (str(row["provider"]), str(row["model"]))
    if row["mode"] == "planner_executor":
        return pair in ACTIVE_PLANNER_MODELS
    return pair in ACTIVE_SIMPLE_MODELS


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
    return 0.0 if denominator == 0 else numerator / denominator


def correct_ping_false_positives(
    eval_data: dict[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    corrected = json.loads(json.dumps(eval_data))
    corrections: list[dict[str, Any]] = []

    testcases = corrected.get("details", {}).get("testcases", [])
    for index, tc in enumerate(testcases):
        expected = str(tc.get("expected_output", "")).strip()
        actual = str(tc.get("actual_output", ""))
        if tc.get("passed") is True and expected == "0% packet loss" and "100% packet loss" in actual:
            tc["passed"] = False
            corrections.append(
                {
                    "testcase_index": index,
                    "testcase_name": tc.get("name"),
                    "device": tc.get("device"),
                    "command": tc.get("command"),
                    "expected_output": expected,
                    "actual_packet_loss": "100% packet loss",
                }
            )

    if not corrections:
        return corrected, corrections

    total_testcases = len(testcases)
    passed_testcases = sum(1 for tc in testcases if tc.get("passed") is True)
    s_testcase = passed_testcases / total_testcases if total_testcases else 0.0

    s_safety = corrected.get("S_safety")
    s_command = corrected.get("S_command") or 0.0
    s_reasoning = corrected.get("S_reasoning")

    corrected["S_testcase"] = round(s_testcase, 4)
    corrected["composite_score"] = None if s_safety is None else round(s_testcase * s_safety, 4)
    corrected["summary_score"] = round(
        compute_summary_score(s_testcase, s_command, s_reasoning, s_safety),
        4,
    )
    corrected["outcome_class"] = classify_outcome(s_testcase, s_safety)
    return corrected, corrections


def update_row_from_eval(row: dict[str, Any], eval_data: dict[str, Any]) -> dict[str, Any]:
    updated = dict(row)
    testcases = eval_data.get("details", {}).get("testcases", [])
    invariants = eval_data.get("details", {}).get("baseline_invariants", [])
    command = eval_data.get("details", {}).get("command", {})
    reasoning = eval_data.get("details", {}).get("reasoning", {})

    updated["summary_score"] = eval_data.get("summary_score")
    updated["composite_score"] = eval_data.get("composite_score")
    updated["S_testcase"] = eval_data.get("S_testcase")
    updated["S_command"] = eval_data.get("S_command")
    updated["S_reasoning"] = eval_data.get("S_reasoning")
    updated["S_safety"] = eval_data.get("S_safety")
    updated["outcome_class"] = eval_data.get("outcome_class")
    updated["passed_testcases"] = sum(1 for tc in testcases if tc.get("passed") is True)
    updated["total_testcases"] = len(testcases)
    updated["passed_invariants"] = sum(1 for inv in invariants if inv.get("passed") is True)
    updated["total_invariants"] = len(invariants)
    updated["command_precision"] = command.get("precision")
    updated["command_recall"] = command.get("recall")
    updated["steps"] = reasoning.get("total_steps", updated.get("steps", 0))
    return updated


def collect_rows() -> tuple[list[dict[str, Any]], dict[tuple[str, str, str, str], Path]]:
    rows_by_key: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    source_by_key: dict[tuple[str, str, str, str], Path] = {}

    sources = [
        (
            SNAPSHOTS / "floating_dnat_20260507-202449" / "results.json",
            SNAPSHOTS / "floating_dnat_20260507-202449" / "runs",
            {"floating_dnat"},
        ),
        (
            SNAPSHOTS / "partial_benchmark_20260508-211419" / "reconstructed_partial_results.json",
            SNAPSHOTS / "partial_benchmark_20260508-211419" / "raw_results" / "runs",
            {"floating_haproxy", "frr_basic", "fw_isav"},
        ),
        (
            RESULTS / "results.json",
            RUNS,
            {
                "fw_ports",
                "isis_ipv6",
                "isis_misconfigured",
                "routing_linear",
                "routing_linear_linkdown",
                "ssh_filtering",
                "vrf_isolation",
            },
        ),
    ]

    for result_file, runs_root, task_names in sources:
        for row in load_json(result_file):
            task_name = Path(row["task"]).stem
            if task_name not in task_names:
                continue
            if not is_active_row(row):
                continue
            key = row_key(row)
            rows_by_key[key] = dict(row)
            source_by_key[key] = runs_root / task_name / run_dir_name(row)

    ordered: list[dict[str, Any]] = []
    missing: list[tuple[str, str, str, str]] = []
    for task in TASK_ORDER:
        task_rows = [row for key, row in rows_by_key.items() if key[0] == task]
        for mode in MODES:
            for provider, model in [
                ("google", "gemini-2.5-flash"),
                ("deepseek", "deepseek-v4-pro"),
                ("openai", "gpt-5.4-mini"),
            ]:
                if mode == "planner_executor":
                    p = f"{provider}/{provider}"
                    m = f"{model} | {model}"
                else:
                    p = provider
                    m = model
                key = (task, p, m, mode)
                if key in rows_by_key:
                    ordered.append(rows_by_key[key])
                else:
                    missing.append(key)

        # Preserve unexpected active rows only if the ordering logic missed them.
        known = {row_key(row) for row in ordered if Path(row["task"]).stem == task}
        for row in sorted(task_rows, key=lambda item: row_key(item)):
            if row_key(row) not in known:
                ordered.append(row)

    if missing:
        print("WARNING missing rows:")
        for item in missing:
            print("  ", item)

    return ordered, source_by_key


def copy_and_correct_runs(
    rows: list[dict[str, Any]],
    source_by_key: dict[tuple[str, str, str, str], Path],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    if OUT_RUNS.exists():
        shutil.rmtree(OUT_RUNS)
    OUT_RUNS.mkdir(parents=True, exist_ok=True)

    corrected_rows: list[dict[str, Any]] = []
    corrections: list[dict[str, Any]] = []
    missing_artifacts: list[dict[str, Any]] = []

    for row in rows:
        key = row_key(row)
        src = source_by_key.get(key)
        dst = OUT_RUNS / key[0] / run_dir_name(row)

        if src and src.exists():
            shutil.copytree(src, dst, dirs_exist_ok=True)
        else:
            dst.mkdir(parents=True, exist_ok=True)
            missing_artifacts.append({"key": key, "expected_source": str(src)})

        eval_path = dst / "evaluation.json"
        if eval_path.exists():
            eval_data = load_json(eval_path)
            corrected_eval, eval_corrections = correct_ping_false_positives(eval_data)
            if eval_corrections:
                write_json(eval_path, corrected_eval)
                for item in eval_corrections:
                    item.update(
                        {
                            "task": row["task"],
                            "provider": row["provider"],
                            "model": row["model"],
                            "mode": row["mode"],
                            "run_dir": str(dst.relative_to(OUT_DIR)),
                        }
                    )
                corrections.extend(eval_corrections)
                row = update_row_from_eval(row, corrected_eval)
        corrected_rows.append(row)

    return corrected_rows, corrections, missing_artifacts


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field) for field in CSV_FIELDS})


def average(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def build_analysis(rows: list[dict[str, Any]], corrections: list[dict[str, Any]]) -> str:
    by_task: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_model: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_mode: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_task[row["task"]].append(row)
        by_model[f"{row['provider']}:{row['model']}"].append(row)
        by_mode[row["mode"]].append(row)

    lines: list[str] = []
    lines.append("Analisi risultati manual_complete_corrected")
    lines.append("=" * 45)
    lines.append("")
    lines.append(f"Generato: {datetime.now().isoformat(timespec='seconds')}")
    lines.append(f"Run totali aggregate: {len(rows)}")
    lines.append(f"Task presenti: {', '.join(task + '.json' for task in TASK_ORDER)}")
    lines.append(f"Correzioni ping 0%/100% applicate: {len(corrections)} testcase")
    lines.append("")

    outcome_counts = Counter(row["outcome_class"] for row in rows)
    lines.append("Distribuzione outcome:")
    for outcome, count in sorted(outcome_counts.items()):
        lines.append(f"- {outcome}: {count}")
    lines.append("")

    lines.append("Risultati per task:")
    for task in TASK_ORDER:
        task_file = f"{task}.json"
        items = by_task.get(task_file, [])
        scores = [float(row["summary_score"]) for row in items if row.get("summary_score") is not None]
        successes = sum(1 for row in items if row.get("outcome_class") == "clean_success")
        failures = sum(1 for row in items if row.get("outcome_class") == "clean_fail")
        timeouts = sum(1 for row in items if row.get("outcome_class") == "agent_timeout")
        other = len(items) - successes - failures - timeouts
        lines.append(
            f"- {task_file}: runs={len(items)}, clean_success={successes}, "
            f"clean_fail={failures}, timeout={timeouts}, other={other}, "
            f"avg_summary={average(scores):.4f}"
        )
    lines.append("")

    lines.append("Risultati per modello/provider:")
    for model_key, items in sorted(by_model.items()):
        scores = [float(row["summary_score"]) for row in items if row.get("summary_score") is not None]
        successes = sum(1 for row in items if row.get("outcome_class") == "clean_success")
        timeouts = sum(1 for row in items if row.get("outcome_class") == "agent_timeout")
        lines.append(
            f"- {model_key}: runs={len(items)}, clean_success={successes}, "
            f"timeout={timeouts}, avg_summary={average(scores):.4f}"
        )
    lines.append("")

    lines.append("Risultati per modalita':")
    for mode, items in sorted(by_mode.items()):
        scores = [float(row["summary_score"]) for row in items if row.get("summary_score") is not None]
        successes = sum(1 for row in items if row.get("outcome_class") == "clean_success")
        lines.append(
            f"- {mode}: runs={len(items)}, clean_success={successes}, "
            f"avg_summary={average(scores):.4f}"
        )
    lines.append("")

    lines.append("Lettura sintetica:")
    lines.append(
        "I task di forwarding lineare e floating HAProxy risultano molto stabili: quasi tutti "
        "i modelli li completano con successo, segno che la parte di configurazione IP, route "
        "statiche e bilanciamento base e' ben benchmarkabile."
    )
    lines.append(
        "I firewall task separano meglio le modalita': ReAct e ReAct con rollback tendono a "
        "funzionare meglio del single_turn, perche' possono osservare lo stato e correggere ACL "
        "o regole nftables in modo incrementale."
    )
    lines.append(
        "I task IS-IS sono i piu' critici: molti run non arrivano alla convergenza o mancano "
        "dettagli FRR specifici come redistribute ipv6 connected level-2 o l'attivazione di "
        "IS-IS sull'interfaccia corretta. Questo e' utile per la tesi, perche' mostra che i "
        "task dinamici di control plane richiedono osservazione, sintassi precisa e diagnosi "
        "di stato, non solo configurazioni superficiali."
    )
    lines.append(
        "DeepSeek V4 Pro e' spesso competitivo quando completa il run, ma e' anche quello piu' "
        "soggetto a timeout nei task lunghi. Gemini Flash e GPT-5.4 Mini sono piu' regolari "
        "nei tempi; GPT-5.4 Mini risulta sorprendentemente buono su alcuni task firewall e "
        "routing semplici, ma non risolve stabilmente IS-IS."
    )
    lines.append(
        "Il bug sul matching testuale del ping non richiede di ripetere la run: la correzione "
        "e' deterministica, locale ai testcase interessati e conserva gli output raw. I risultati "
        "corretti sono quindi piu' affidabili del CSV originale senza consumare nuovi token."
    )
    return "\n".join(lines) + "\n"


def main() -> None:
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = SNAPSHOTS / f"pre_manual_rebuild_{timestamp}" / "raw_results"
    if backup.exists():
        raise RuntimeError(f"Backup already exists: {backup}")
    shutil.copytree(RESULTS, backup, ignore=shutil.ignore_patterns("manual_complete_corrected"))

    rows, source_by_key = collect_rows()
    corrected_rows, corrections, missing_artifacts = copy_and_correct_runs(rows, source_by_key)

    write_json(OUT_RESULTS_JSON, corrected_rows)
    write_csv(OUT_RESULTS_CSV, corrected_rows)
    write_json(OUT_CORRECTIONS, corrections)
    write_json(
        OUT_MANIFEST,
        {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "backup_raw_results": str(backup),
            "task_order": [task + ".json" for task in TASK_ORDER],
            "rows": len(corrected_rows),
            "expected_rows": len(TASK_ORDER) * len(MODES) * 3,
            "correction_rule": "If testcase expected_output is exactly '0% packet loss', actual_output contains '100% packet loss', and passed=true, set passed=false and recompute testcase-dependent scores.",
            "ping_false_positive_corrections": len(corrections),
            "missing_artifacts": missing_artifacts,
            "sources": {
                "floating_dnat": "results_snapshots/floating_dnat_20260507-202449",
                "floating_haproxy_frr_basic_fw_isav": "results_snapshots/partial_benchmark_20260508-211419",
                "remaining_tasks": "results/",
            },
        },
    )

    # Make the canonical aggregate files point to the complete corrected dataset.
    write_json(ROOT_RESULTS_JSON, corrected_rows)
    write_csv(ROOT_RESULTS_CSV, corrected_rows)
    ROOT_ANALYSIS.write_text(build_analysis(corrected_rows, corrections))

    print(f"Wrote {OUT_RESULTS_JSON}")
    print(f"Wrote {OUT_RESULTS_CSV}")
    print(f"Wrote {OUT_RUNS}")
    print(f"Wrote {ROOT_RESULTS_JSON}")
    print(f"Wrote {ROOT_RESULTS_CSV}")
    print(f"Wrote {ROOT_ANALYSIS}")
    print(f"Raw backup: {backup}")
    print(f"Rows: {len(corrected_rows)}")
    print(f"Corrections: {len(corrections)}")
    if missing_artifacts:
        print(f"Missing artifacts: {len(missing_artifacts)}")


if __name__ == "__main__":
    main()
