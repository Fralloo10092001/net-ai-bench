import argparse
import asyncio
import json
import sys
import os
from pathlib import Path
from typing import Any, Dict

from dotenv import load_dotenv
from mcp import StdioServerParameters
from mcp.client.session import ClientSession
from mcp.client.stdio import stdio_client

from .model_registry import get_model

PROJECT_ROOT = Path(__file__).resolve().parent.parent
LOGS_DIR = PROJECT_ROOT / "logs"

load_dotenv(PROJECT_ROOT / ".env")


SYSTEM_PROMPT = """
You are a network automation agent.

You can inspect topology, inspect device configuration, apply configuration,
and validate connectivity.

IMPORTANT:
- Devices may be Linux-based (use ip addr, ip route, sysctl, etc.)
- Do NOT assume Cisco CLI
- Use get_running_config early to infer command style
- You MUST use only one of the exact tool names provided to you
- Do NOT invent tool names
You MUST strictly follow the topology and addressing implied by the task.

Rules:
- Do NOT invent new subnets or IP ranges
- Each link corresponds to ONE subnet
- Assign IPs consistently across the same link
- Do NOT reuse subnets across different links
- Always derive addressing from the topology

Validation rules:
- Always use bounded commands (e.g., ping -c 3 -W 2)
- Never run infinite commands
Interface mapping rule:
- For each link A:ethX <-> B:ethY → assign same subnet
- Different links MUST use different subnets
Tool signatures:
- get_topology(devices: list[str] | None = None)
- get_running_config(device: str)
- update_config(device: str, commands: list[str])
- execute_validation(device: str, command: str)

Use the exact argument names shown above.
Do not use names like device_name, config_commands, or validation_commands.
When the intent specifies exact IP addresses, gateways, or subnets, you must use those exact values.
Do not invent alternative host addresses such as .10 if the task specifies .2.

You must ALWAYS respond in valid JSON only.

Schema:
{
  "thought": "...",
  "action": "tool_call" | "finish",
  "tool_name": "...",
  "tool_args": { ... },
  "final_reasoning": "..." 
}

Rules:
- If action == "tool_call", tool_name must be exactly one valid tool name
- If action == "finish", include final_reasoning
- Do not use markdown
- Do not wrap JSON in code fences
"""


def _parse_agent_decision(text: str) -> Dict[str, Any]:
    try:
        data = json.loads(text)
    except Exception:
        raise ValueError(f"Model output is not valid JSON:\n{text}")

    required = {"thought", "action"}
    missing = required - set(data.keys())
    if missing:
        raise ValueError(f"Missing keys in model output: {sorted(missing)}")

    if data["action"] == "tool_call":
        for key in ["tool_name", "tool_args"]:
            if key not in data:
                raise ValueError(f"Missing key in tool_call output: {key}")
        if not isinstance(data["tool_args"], dict):
            raise ValueError("tool_args must be a JSON object")

    elif data["action"] == "finish":
        if "final_reasoning" not in data:
            data["final_reasoning"] = ""

    else:
        raise ValueError(f"Invalid action: {data['action']}")

    return data


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


async def _list_tool_names(session: ClientSession) -> list[str]:
    tools = await session.list_tools()
    return [t.name for t in tools.tools]


async def _agent_loop(session: ClientSession, llm, max_steps: int = 25):
    history = []
    valid_tool_names = await _list_tool_names(session)

    for step in range(1, max_steps + 1):
        prompt = (
            SYSTEM_PROMPT
            + "\n\nValid tool names:\n"
            + json.dumps(valid_tool_names, indent=2)
            + "\n\nHistory:\n"
            + json.dumps(history, indent=2)
        )

        response = await llm.ainvoke(prompt)
        raw_output = response.content if hasattr(response, "content") else str(response)

        if isinstance(raw_output, list):
            raw_output = "".join(str(x) for x in raw_output)

        try:
            decision = _parse_agent_decision(str(raw_output))
        except Exception as e:
            history.append({
                "step": step,
                "thought": "Model output could not be parsed",
                "action": "parse_error",
                "raw_output": str(raw_output),
                "observation": f"ERROR: {e}",
            })
            continue

        entry = {
            "step": step,
            "thought": decision["thought"],
            "action": decision["action"],
        }

        if decision["action"] == "finish":
            entry["final_reasoning"] = decision.get("final_reasoning", "") or decision["thought"]
            history.append(entry)
            return history, entry["final_reasoning"]

        tool_name = decision["tool_name"]
        tool_args = decision["tool_args"]

        entry["tool_name"] = tool_name
        entry["tool_args"] = tool_args

        if tool_name not in valid_tool_names:
            entry["observation"] = (
                f"ERROR: invalid tool name '{tool_name}'. "
                f"Use one of: {valid_tool_names}"
            )
            history.append(entry)
            continue

        try:
            result = await session.call_tool(tool_name, tool_args)
            entry["observation"] = _extract_mcp_text(result)
        except Exception as e:
            entry["observation"] = f"Error executing tool {tool_name}: {e}"

        history.append(entry)

    return history, "Agent reached maximum number of steps before finishing."

async def _export_final_configs(session: ClientSession, devices: list[str]) -> dict[str, str]:
    final_configs = {}

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
def run(
    lab: str,
    task_path: str,
    provider: str,
    model: str,
    max_steps: int = 25,
):
    LOGS_DIR.mkdir(parents=True, exist_ok=True)

    log_path = (LOGS_DIR / "agent_commands.jsonl").resolve()
    history_file = (LOGS_DIR / "agent_history.json").resolve()
    reasoning_file = (LOGS_DIR / "reasoning.txt").resolve()

    for p in [log_path, history_file, reasoning_file]:
        if p.exists():
            p.unlink()

    print(f"DEBUG log_path={log_path}")
    print(f"DEBUG history_file={history_file}")
    print(f"DEBUG reasoning_file={reasoning_file}")

    llm = get_model(provider, model)

    async def _run_async():
        mcp_server_path = PROJECT_ROOT / "ani" / "mcp_server.py"

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

                history, reasoning = await _agent_loop(
                    session=session,
                    llm=llm,
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

                history_file.write_text(json.dumps(history, indent=2))
                reasoning_file.write_text(reasoning)

                final_configs_file = LOGS_DIR / "final_configs.json"
                final_configs_file.write_text(json.dumps(final_configs, indent=2))

                return {
                    "history_file": str(history_file),
                    "reasoning_file": str(reasoning_file),
                    "log_file": str(log_path),
                    "final_configs_file": str(final_configs_file),
                }

    return asyncio.run(_run_async())


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--lab", required=True)
    parser.add_argument("--task", required=True)
    parser.add_argument("--provider", required=True, choices=["openai", "anthropic", "google", "openrouter"])
    parser.add_argument("--model", required=True)
    parser.add_argument("--max-steps", type=int, default=25)
    args = parser.parse_args()

    result = run(
        lab=args.lab,
        task_path=args.task,
        provider=args.provider,
        model=args.model,
        max_steps=args.max_steps,
    )
    

    print("\n" + "=" * 55)
    print("  AGENT COMPLETED")
    print("=" * 55)
    print(f"  Provider  : {args.provider}")
    print(f"  Model     : {args.model}")
    print(f"  Lab       : {args.lab}")
    print(f"  Task      : {args.task}")
    print(f"  Log file  : {result['log_file']}")
    print(f"  History   : {result['history_file']}")
    print(f"  Reasoning : {result['reasoning_file']}")
    print(f"  Final cfg : {result['final_configs_file']}")
    print("=" * 55)