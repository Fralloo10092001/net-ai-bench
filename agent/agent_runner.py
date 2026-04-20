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

PROJECT_ROOT = Path(__file__).resolve().parent.parent
LOGS_DIR = PROJECT_ROOT / "logs"

load_dotenv(PROJECT_ROOT / ".env")


# Estrae il contenuto testuale della risposta MCP in un formato semplice e serializzabile
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


# Legge il task JSON e ne costruisce una versione compatta da passare all'agente
def _load_task_text(task_path: str) -> str:
    with open(task_path) as f:
        task = json.load(f)

    compact_task = {
        "task_name": task.get("task_name") or task.get("name"),
        "intent": task.get("intent") or task.get("intents"),
        "lab_file": task.get("lab_file"),
        "ground_truth_configs": task.get("ground_truth_configs"),
        "testcases": task.get("testcases"),
    }
    return json.dumps(compact_task, indent=2)


# Esporta la configurazione finale di tutti i device usando get_running_config
async def _export_final_configs(session: ClientSession, devices: list[str]) -> dict[str, str]:
    final_configs: dict[str, str] = {}

    for device in devices:
        try:
            result = await session.call_tool("get_running_config", {"device": device})
            text = _extract_mcp_text(result)

            if isinstance(text, list):
                text = "\n".join(str(x) for x in text)

            final_configs[device] = str(text)
        except Exception as e:
            final_configs[device] = f"ERROR: {e}"

    return final_configs


# Costruisce l'istanza agente corretta in base alla modalità richiesta da CLI
def get_agent(mode: str, llm):
    if mode == "react":
        return ReActAgent(llm)
    if mode == "single_turn":
        return SingleTurnAgent(llm)
    if mode == "planner_executor":
        return PlannerExecutorAgent(llm)

    raise ValueError(f"Unsupported agent mode: {mode}")


# Coordina un singolo run: crea la sessione MCP, esegue l'agente e salva gli artefatti
def run(
    lab: str,
    task_path: str,
    provider: str,
    model: str,
    max_steps: int = 25,
    agent_mode: str = "react",
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

    llm = get_model(provider, model)
    task_text = _load_task_text(task_path)

    # Esegue la logica asincrona del run aprendo il subprocess MCP su stdio
    async def _run_async():
        mcp_server_path = PROJECT_ROOT / "ani" / "mcp_server.py"
        agent = get_agent(agent_mode, llm)

        server_params = StdioServerParameters(
            command=sys.executable,
            args=[
                str(mcp_server_path),
                "--lab", lab,
                "--task", str(Path(task_path).resolve()),
                "--log", str(log_path),
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

                topo_result = await session.call_tool("get_topology", {})
                topo_text = _extract_mcp_text(topo_result)

                if isinstance(topo_text, str):
                    topo_data = json.loads(topo_text)
                else:
                    topo_data = topo_text

                devices = topo_data["topology"]["nodes"]
                final_configs = await _export_final_configs(session, devices)

                history_file.write_text(json.dumps(run_result.history, indent=2))
                reasoning_file.write_text(run_result.final_reasoning)

                final_configs_file = LOGS_DIR / "final_configs.json"
                final_configs_file.write_text(json.dumps(final_configs, indent=2))

                run_result_file.write_text(
                    json.dumps(
                        {
                            "mode": run_result.mode,
                            "final_reasoning": run_result.final_reasoning,
                            "history_length": len(run_result.history),
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
                }

    return asyncio.run(_run_async())


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--lab", required=True)
    parser.add_argument("--task", required=True)
    parser.add_argument(
        "--provider",
        required=True,
        choices=["openai", "anthropic", "google", "openrouter"],
    )
    parser.add_argument("--model", required=True)
    parser.add_argument("--max-steps", type=int, default=25)
    parser.add_argument(
        "--agent-mode",
        default="react",
        choices=["single_turn", "react", "planner_executor"],
    )
    args = parser.parse_args()

    result = run(
        lab=args.lab,
        task_path=args.task,
        provider=args.provider,
        model=args.model,
        max_steps=args.max_steps,
        agent_mode=args.agent_mode,
    )

    print("\n" + "=" * 55)
    print("  AGENT COMPLETED")
    print("=" * 55)
    print(f"  Provider   : {args.provider}")
    print(f"  Model      : {args.model}")
    print(f"  Agent mode : {result['agent_mode']}")
    print(f"  Lab        : {args.lab}")
    print(f"  Task       : {args.task}")
    print(f"  Log file   : {result['log_file']}")
    print(f"  History    : {result['history_file']}")
    print(f"  Reasoning  : {result['reasoning_file']}")
    print(f"  Final cfg  : {result['final_configs_file']}")
    print(f"  Run result : {result['run_result_file']}")
    print("=" * 55)