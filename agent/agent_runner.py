from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from mcp import StdioServerParameters
from mcp.client.session import ClientSession
from mcp.client.stdio import stdio_client

from .model_registry import get_model
from .planner_executor import PlannerExecutorAgent
from .react import ReActAgent
from .single_turn import SingleTurnAgent
from .usage_utils import estimate_cost_usd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
LOGS_DIR = PROJECT_ROOT / "logs"

load_dotenv(PROJECT_ROOT / ".env")


# Estrae testo semplice da un risultato MCP
def _extract_mcp_text(result: Any) -> Any:
    if not hasattr(result, "content"):
        return str(result)

    extracted = []

    for item in result.content:
        if hasattr(item, "text"):
            extracted.append(item.text)
        else:
            extracted.append(str(item))

    if len(extracted) == 1:
        return extracted[0]

    return extracted


# Carica una rappresentazione pubblica del task per i prompt degli agenti
def _load_task_text(task_path: str) -> str:
    with open(task_path) as f:
        task = json.load(f)

    compact_task = {
        "task_name": task.get("task_name") or task.get("name"),
        "intent": task.get("intent") or task.get("intents"),
        "lab_file": task.get("lab_file"),
        "difficulty": task.get("difficulty"),
    }

    if task.get("description"):
        compact_task["description"] = task.get("description")

    if task.get("constraints"):
        compact_task["constraints"] = task.get("constraints")

    return json.dumps(compact_task, indent=2)


# Restituisce token budget consigliati per provider/modello/modalità
def _get_token_budgets(
    provider: str,
    model: str,
    mode: str,
) -> tuple[int, int, int]:
    provider = provider.lower()
    model = model.lower()

    # default generici
    planner = 2200
    executor = 1600
    validator = 700

    if provider == "google":
        planner = 4096
        executor = 2200
        validator = 900

        if "pro" in model:
            executor = 3000

    elif provider == "openai":
        planner = 2400
        executor = 1800
        validator = 700

        if "gpt-4o" in model:
            planner = 2600
            executor = 2000

    elif provider == "openrouter":
        planner = 1600
        executor = 1200
        validator = 600

    elif provider == "deepseek":
        planner = 3000
        executor = 2200
        validator = 800

    elif provider == "nvidia":
        planner = 4096
        executor = 3000
        validator = 1000

    elif provider == "anthropic":
        planner = 3000
        executor = 2200
        validator = 800

    # per modalità semplici serve solo executor-like budget
    if mode in {"react", "single_turn"}:
        planner = executor

    return planner, executor, validator


# Esporta le configurazioni finali di tutti i nodi della topologia
async def _export_final_configs(
    session: ClientSession,
    devices: list[str],
) -> dict[str, str]:
    final_configs: dict[str, str] = {}

    for device in devices:
        try:
            result = await session.call_tool(
                "get_running_config",
                {"device": device},
            )

            text = _extract_mcp_text(result)

            if isinstance(text, list):
                text = "\n".join(str(x) for x in text)

            final_configs[device] = str(text)

        except Exception as e:
            final_configs[device] = f"ERROR: {e}"

    return final_configs


# Costruisce l'istanza corretta dell'agente in base alla modalità selezionata
def get_agent(
    mode: str,
    llm,
    planner_llm=None,
    executor_llm=None,
    validator_llm=None,
    planner_provider: str | None = None,
    planner_model: str | None = None,
    executor_provider: str | None = None,
    executor_model: str | None = None,
    enable_rollback: bool = False,
):
    if mode == "react":
        return ReActAgent(
            llm,
            enable_rollback=enable_rollback,
        )

    if mode == "single_turn":
        return SingleTurnAgent(llm)

    if mode == "planner_executor":
        return PlannerExecutorAgent(
            llm=llm,
            planner_llm=planner_llm,
            executor_llm=executor_llm,
            validator_llm=validator_llm,
            planner_provider=planner_provider,
            planner_model=planner_model,
            executor_provider=executor_provider,
            executor_model=executor_model,
            enable_rollback=enable_rollback,
        )

    raise ValueError(f"Unsupported agent mode: {mode}")


# Esegue un singolo run agente su una coppia lab/task
def run(
    lab: str,
    task_path: str,
    provider: str,
    model: str,
    max_steps: int = 25,
    agent_mode: str = "react",
    planner_provider: str | None = None,
    planner_model: str | None = None,
    executor_provider: str | None = None,
    executor_model: str | None = None,
    enable_rollback: bool = False,
):
    LOGS_DIR.mkdir(parents=True, exist_ok=True)

    log_path = (LOGS_DIR / "agent_commands.jsonl").resolve()
    history_file = (LOGS_DIR / "agent_history.json").resolve()
    reasoning_file = (LOGS_DIR / "reasoning.txt").resolve()
    run_result_file = (LOGS_DIR / "run_result.json").resolve()

    for p in [log_path, history_file, reasoning_file, run_result_file]:
        if p.exists():
            p.unlink()

    print(f"DEBUG log_path={log_path}")
    print(f"DEBUG history_file={history_file}")
    print(f"DEBUG reasoning_file={reasoning_file}")
    print(f"DEBUG run_result_file={run_result_file}")

    planner_budget, executor_budget, validator_budget = _get_token_budgets(
        provider=provider,
        model=model,
        mode=agent_mode,
    )

    # modello unico per modalità non planner/executor
    llm = get_model(
        provider,
        model,
        max_tokens=executor_budget,
    )

    task_text = _load_task_text(task_path)

    effective_provider = provider
    effective_model = model

    planner_llm = None
    executor_llm = None
    validator_llm = None

    if agent_mode == "planner_executor":
        planner_provider = planner_provider or provider
        planner_model = planner_model or model

        executor_provider = executor_provider or provider
        executor_model = executor_model or model

        planner_budget, _, _ = _get_token_budgets(
            planner_provider,
            planner_model,
            "planner_executor",
        )

        _, executor_budget, validator_budget = _get_token_budgets(
            executor_provider,
            executor_model,
            "planner_executor",
        )

        planner_llm = get_model(
            planner_provider,
            planner_model,
            max_tokens=planner_budget,
        )

        executor_llm = get_model(
            executor_provider,
            executor_model,
            max_tokens=executor_budget,
        )

        validator_llm = get_model(
            executor_provider,
            executor_model,
            max_tokens=validator_budget,
        )

        effective_provider = f"{planner_provider}/{executor_provider}"
        effective_model = f"{planner_model} | {executor_model}"

    async def _run_async():
        mcp_server_path = PROJECT_ROOT / "ani" / "mcp_server.py"

        agent = get_agent(
            mode=agent_mode,
            llm=llm,
            planner_llm=planner_llm,
            executor_llm=executor_llm,
            validator_llm=validator_llm,
            planner_provider=planner_provider,
            planner_model=planner_model,
            executor_provider=executor_provider,
            executor_model=executor_model,
            enable_rollback=enable_rollback,
        )

        server_params = StdioServerParameters(
            command=sys.executable,
            args=[
                str(mcp_server_path),
                "--lab",
                lab,
                "--task",
                str(Path(task_path).resolve()),
                "--log",
                str(log_path),
            ],
        )

        async with stdio_client(server_params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()

                run_result = await agent.run(
                    session=session,
                    task_text=task_text,
                    max_steps=max_steps,
                )

                topo_result = await session.call_tool(
                    "get_topology",
                    {},
                )

                topo_text = _extract_mcp_text(topo_result)

                if isinstance(topo_text, str):
                    topo_data = json.loads(topo_text)
                else:
                    topo_data = topo_text

                devices = topo_data["topology"]["nodes"]

                final_configs = await _export_final_configs(
                    session,
                    devices,
                )

                history_file.write_text(
                    json.dumps(run_result.history, indent=2)
                )

                reasoning_file.write_text(
                    run_result.final_reasoning
                )

                final_configs_file = LOGS_DIR / "final_configs.json"

                final_configs_file.write_text(
                    json.dumps(final_configs, indent=2)
                )

                usage = run_result.usage
                usage_breakdown = run_result.usage_breakdown or {}

                estimated_cost_usd = estimate_cost_usd(
                    effective_provider,
                    effective_model,
                    usage,
                    usage_breakdown=usage_breakdown,
                )

                run_result_file.write_text(
                    json.dumps(
                        {
                            "mode": run_result.mode,
                            "final_reasoning": run_result.final_reasoning,
                            "history_length": len(run_result.history),
                            "total_tokens": usage["total_tokens"],
                            "estimated_cost_usd": estimated_cost_usd,
                            "provider": effective_provider,
                            "model": effective_model,
                            "usage_breakdown": usage_breakdown,
                            "metadata": run_result.metadata,
                        },
                        indent=2,
                    )
                )

                return {
                    "history_file": str(history_file),
                    "reasoning_file": str(reasoning_file),
                    "log_file": str(log_path),
                    "final_configs_file": str(final_configs_file),
                    "run_result_file": str(run_result_file),
                    "agent_mode": run_result.mode,
                    "provider": effective_provider,
                    "model": effective_model,
                    "total_tokens": usage["total_tokens"],
                    "estimated_cost_usd": estimated_cost_usd,
                }

    return asyncio.run(_run_async())


if __name__ == "__main__":
    parser = argparse.ArgumentParser()

    parser.add_argument("--lab", required=True)
    parser.add_argument("--task", required=True)

    parser.add_argument(
        "--provider",
        required=True,
        choices=[
            "openai",
            "anthropic",
            "google",
            "openrouter",
            "deepseek",
            "nvidia",
        ],
    )

    parser.add_argument("--model", required=True)

    parser.add_argument(
        "--max-steps",
        type=int,
        default=25,
    )

    parser.add_argument(
        "--agent-mode",
        default="react",
        choices=[
            "single_turn",
            "react",
            "planner_executor",
        ],
    )

    parser.add_argument(
        "--planner-provider",
        choices=[
            "openai",
            "anthropic",
            "google",
            "openrouter",
            "deepseek",
            "nvidia",
        ],
    )

    parser.add_argument("--planner-model")

    parser.add_argument(
        "--executor-provider",
        choices=[
            "openai",
            "anthropic",
            "google",
            "openrouter",
            "deepseek",
            "nvidia",
        ],
    )

    parser.add_argument("--executor-model")

    parser.add_argument(
        "--enable-rollback",
        action="store_true",
        help="Enable rollback-related MCP tools for supported agent modes",
    )

    args = parser.parse_args()

    result = run(
        lab=args.lab,
        task_path=args.task,
        provider=args.provider,
        model=args.model,
        max_steps=args.max_steps,
        agent_mode=args.agent_mode,
        planner_provider=args.planner_provider,
        planner_model=args.planner_model,
        executor_provider=args.executor_provider,
        executor_model=args.executor_model,
        enable_rollback=args.enable_rollback,
    )

    print("\n" + "=" * 55)
    print("  AGENT COMPLETED")
    print("=" * 55)
    print(f"  Provider   : {result['provider']}")
    print(f"  Model      : {result['model']}")
    print(f"  Agent mode : {result['agent_mode']}")
    print(f"  Lab        : {args.lab}")
    print(f"  Task       : {args.task}")
    print(f"  Log file   : {result['log_file']}")
    print(f"  History    : {result['history_file']}")
    print(f"  Reasoning  : {result['reasoning_file']}")
    print(f"  Final cfg  : {result['final_configs_file']}")
    print(f"  Run result : {result['run_result_file']}")
    print("=" * 55)
