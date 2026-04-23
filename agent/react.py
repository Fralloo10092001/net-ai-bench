from __future__ import annotations

import json
from typing import Any

from mcp.client.session import ClientSession

from .base import Agent, RunResult
from .usage_utils import add_usage, extract_usage_from_response


SYSTEM_PROMPT = """
You are a network automation agent.

Your job is to solve the task by inspecting the topology, inspecting current device configuration, applying only the necessary configuration changes, and validating the expected final connectivity.

IMPORTANT:
- Devices may use different network operating systems and CLIs.
- Do NOT assume Cisco CLI.
- Use only commands appropriate for the device type and platform described in the task context.
- You MUST use only the exact tool names provided.
- Do NOT invent tool names.
- You MUST strictly follow the topology and the exact addressing required by the task.
- Preserve healthy pre-existing system state unless the task explicitly requires changing it.
- Do not remove or replace existing management connectivity unless the task explicitly requires it.
- Do not change management interfaces, management routes, or control-plane reachability if the task can be solved without touching them.
- Prefer additive and minimal changes over destructive replacements.
- If a working route or interface is unrelated to the task goal, leave it unchanged.

You must use the exact argument names shown above.
Do not use names like device_name, config_commands, validation_commands, or null tool_args.
If you output invalid JSON, the system will fail. Ensure all required fields are present.

Expected behavior:
- Inspect topology before making configuration changes.
- Inspect current running configuration of relevant devices early.
- Compare current state with the task requirements before acting.
- Apply only missing or incorrect configuration.
- Validate the final task goal using bounded commands.
- Finish as soon as the task has been successfully validated.

Topology and addressing rules:
- Each link corresponds to exactly one subnet.
- The two interfaces on the same link must belong to the same subnet.
- Different links must use different subnets.
- Do NOT invent new subnets or IP ranges.
- If the task specifies exact IPs, gateways, or subnets, you must use those exact values.
- Do NOT invent alternative host addresses such as .10 if the task specifies .2.
- Do NOT swap interface-to-subnet mappings.

Configuration rules:
- Prefer reading running configuration before acting.
- Do NOT re-apply configuration that is already correct.
- If a command fails because the desired state is already present, treat that as a signal to inspect state, not as a reason to redesign the network.
- Apply minimal changes needed to satisfy the task.
- Do NOT perform unrelated diagnostics unless necessary.
- Before deleting or replacing an existing route, address, or setting, verify that the change is explicitly required by the task.
- If task connectivity can be achieved without removing an existing default route or management path, you must prefer that safer solution.
- Existing management default routes on eth0 are considered protected state unless the task explicitly requires changing them.
- Do not remove or replace the default route via the management network just because the task also specifies a data-plane gateway.
- Prefer host-specific or subnet-specific routing changes over replacing management default routes whenever possible.
- Avoid modifying eth0 or management-facing interfaces unless the task explicitly requires it.
- If you remove a management default route and then observe errors, loss of expected behavior, or a need to recover, prefer rollback(device) over continuing with speculative fixes.

Validation rules:
- Always use bounded validation commands, for example: ping -c 3 -W 2 <destination>
- Never run infinite commands.
- Validate the actual task goal, not arbitrary intermediate checks, unless intermediate checks are needed for debugging.
- After successful validation of the task goal, immediately return action = "finish".
- Do not exhaustively replay every testcase if the main task objective has already been proven.
- Prefer the minimum number of validation steps needed to prove success.

You must ALWAYS respond in valid JSON only.

Schema:
{
  "thought": "...",
  "action": "tool_call" | "finish",
  "tool_name": "...",
  "tool_args": { ... },
  "final_reasoning": "..."
}

Output rules:
- If action == "tool_call", tool_name must be exactly one valid tool name and tool_args must be a JSON object.
- If action == "finish", include final_reasoning.
- Do not use markdown.
- Do not wrap JSON in code fences.
- Do not output any extra text before or after the JSON.
- Never return tool_args = null. Use {} when no arguments are needed.
- If the main requested connectivity or outcome has already been validated successfully, finish immediately.
- Do not spend extra steps re-checking every interface and route unless needed to debug a failure.
""".strip()


# Clean model output by removing optional markdown fences.
def _clean_model_output(text: str) -> str:
    text = text.strip()

    if text.startswith("```"):
        lines = text.splitlines()

        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]

        text = "\n".join(lines).strip()

    return text


# Parse the model JSON decision.
# Converte l'output del modello in una decisione JSON validata
def _parse_agent_decision(text: str) -> dict[str, Any]:
    text = _clean_model_output(text)

    try:
        data = json.loads(text)
    except Exception:
        raise ValueError(f"Model output is not valid JSON:\n{text}")

    if "action" not in data:
        raise ValueError("Missing key in model output: ['action']")

    if "thought" not in data or not isinstance(data.get("thought"), str):
        data["thought"] = ""

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


# Extract plain text from an MCP tool result.
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


# Build platform-specific guidance from the task text.
def _build_platform_guidance(task_text: str) -> str:
    lower = task_text.lower()

    if "nokia_srlinux" in lower or "srl" in lower:
        return """
PLATFORM GUIDANCE:
- This task may involve Nokia SR Linux devices.
- For SR Linux, do not assume Linux interface configuration commands such as ip addr add or ip route add for interface setup.
- Prefer SR Linux CLI style configuration and SR Linux operational validation commands when interacting with SR Linux devices.
- SR Linux validation output may differ from standard Linux ping output.
- Use Linux-style commands only for Linux-based nodes if the task includes them.
""".strip()

    if "openbsd" in lower:
        return """
PLATFORM GUIDANCE:
- This task may involve OpenBSD devices.
- Use commands appropriate for OpenBSD where needed.
- Do not assume Linux-only command behavior on OpenBSD devices.
""".strip()

    return """
PLATFORM GUIDANCE:
- This task uses Linux-like devices.
- Linux networking commands such as ip addr, ip route, ip link, sysctl, and ping are appropriate when supported by the device.
""".strip()


# Build the available-tools section dynamically, depending on whether rollback is enabled.
def _build_available_tools_text(enable_rollback: bool) -> str:
    lines = [
        "Available tools:",
        "- get_topology(devices: list[str] | None = None)",
        "- get_running_config(device: str)",
        "- update_config(device: str, commands: list[str])",
        "- execute_validation(device: str, command: str)",
    ]

    if enable_rollback:
        lines.extend([
            "- rollback(device: str)",
            "- get_rollback_status(device: str | None = None)",
        ])

    return "\n".join(lines)


# Build the step prompt for a single ReAct turn.
def _build_step_prompt(
    task_text: str,
    valid_tool_names: list[str],
    history: list[dict[str, Any]],
    enable_rollback: bool,
    parse_warning: str = "",
) -> str:
    parts = []

    if parse_warning:
        parts.append(parse_warning)

    platform_guidance = _build_platform_guidance(task_text)
    available_tools_text = _build_available_tools_text(enable_rollback)

    rollback_guidance = ""
    if enable_rollback:
        rollback_guidance = """
ROLLBACK GUIDANCE:
- If a recent configuration change appears to have broken a device state, management reachability, or task progress, you may use rollback(device) to restore the most recent saved snapshot for that device.
- Prefer rollback over piling on additional speculative fixes when a recent change likely caused the problem.
- Before using rollback, use the execution history and current observations to identify which device was most likely affected by the failed or harmful change.
- Use get_rollback_status when useful to check whether rollback snapshots are available.
""".strip()

    parts.append(
        f"""
CURRENT TASK:
{task_text}

{platform_guidance}

{available_tools_text}

VALID TOOL NAMES:
{json.dumps(valid_tool_names, indent=2)}

EXECUTION HISTORY:
{json.dumps(history, indent=2)}

{rollback_guidance}

INSTRUCTIONS FOR THIS STEP:
- Read the CURRENT TASK carefully.
- Use the exact IP addresses, gateways, and subnets required by the task.
- Do not invent new subnets or alternative host addresses.
- Inspect current state before changing configuration.
- Apply only missing or incorrect configuration.
- Do not repeat commands that are already satisfied.
- Use only valid tool names.
- Use only exact argument names required by the tools.
- Validation must use bounded commands appropriate for the platform and device type.
- Finish immediately after the task goal has been successfully validated.
- Return raw JSON only.
- Do not use ```json fences.
- tool_args must always be a JSON object, never null.
- Preserve healthy pre-existing system state unless the task explicitly requires changing it.
- Avoid removing existing default routes or management paths unless clearly and explicitly required by the task.
- Treat management default routes on eth0 as protected unless the task explicitly instructs you to replace them.
- If connectivity can be achieved with specific routes instead of replacing a management default route, prefer the safer specific-route solution.- Return exactly one JSON object matching the required schema.
""".strip()
    )

    return "\n\n".join(part for part in parts if part.strip())


# List all available MCP tools from the session.
async def _list_tool_names(session: ClientSession) -> list[str]:
    tools = await session.list_tools()
    return [t.name for t in tools.tools]


# Remove rollback tools from the valid tool list when rollback is disabled.
def _filter_tool_names(valid_tool_names: list[str], enable_rollback: bool) -> list[str]:
    if enable_rollback:
        return valid_tool_names

    blocked = {"rollback", "get_rollback_status"}
    return [name for name in valid_tool_names if name not in blocked]


# Count rollback-related tool usage in the agent history.
def _collect_rollback_metrics(history: list[dict[str, Any]]) -> dict[str, int]:
    rollback_calls = 0
    rollback_status_calls = 0

    for step in history:
        if step.get("action") != "tool_call":
            continue

        tool_name = step.get("tool_name")
        if tool_name == "rollback":
            rollback_calls += 1
        elif tool_name == "get_rollback_status":
            rollback_status_calls += 1

    return {
        "rollback_calls": rollback_calls,
        "rollback_status_calls": rollback_status_calls,
    }


class ReActAgent(Agent):
    # Initialize the ReAct agent, optionally enabling rollback tools.
    def __init__(self, llm, enable_rollback: bool = False):
        self.llm = llm
        self.enable_rollback = enable_rollback

    # Run the full ReAct loop against the MCP session.
    async def run(self, session: ClientSession, task_text: str, max_steps: int) -> RunResult:
        history: list[dict[str, Any]] = []
        valid_tool_names = await _list_tool_names(session)
        valid_tool_names = _filter_tool_names(valid_tool_names, self.enable_rollback)

        usage_totals = {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
        }

        for step in range(1, max_steps + 1):
            recent_history = history[-4:]

            parse_warning = ""
            if recent_history and recent_history[-1].get("action") == "parse_error":
                parse_warning = """
IMPORTANT LAST ERROR:
Your previous response was rejected because it was not raw JSON.
Return ONLY raw JSON.
Do NOT use markdown fences.
tool_args must be {} or another JSON object, never null.
""".strip()

            prompt = SYSTEM_PROMPT + "\n\n" + _build_step_prompt(
                task_text=task_text,
                valid_tool_names=valid_tool_names,
                history=recent_history,
                enable_rollback=self.enable_rollback,
                parse_warning=parse_warning,
            )

            response = await self.llm.ainvoke(prompt)
            usage_totals = add_usage(usage_totals, extract_usage_from_response(response))

            raw_output = response.content if hasattr(response, "content") else str(response)

            if isinstance(raw_output, list):
                raw_output = "".join(str(x) for x in raw_output)

            try:
                decision = _parse_agent_decision(str(raw_output))
            except Exception as e:
                history.append(
                    {
                        "step": step,
                        "thought": "Model output could not be parsed",
                        "action": "parse_error",
                        "raw_output": str(raw_output),
                        "observation": f"ERROR: {e}",
                    }
                )
                continue

            entry: dict[str, Any] = {
                "step": step,
                "thought": decision["thought"],
                "action": decision["action"],
            }

            if decision["action"] == "finish":
                entry["final_reasoning"] = decision.get("final_reasoning", "") or decision["thought"]
                history.append(entry)

                rollback_metrics = _collect_rollback_metrics(history)

                return RunResult(
                    history=history,
                    final_reasoning=entry["final_reasoning"],
                    mode="react",
                    usage=usage_totals,
                    metadata={
                        "rollback_enabled": self.enable_rollback,
                        **rollback_metrics,
                    },
                )

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

        rollback_metrics = _collect_rollback_metrics(history)

        return RunResult(
            history=history,
            final_reasoning="Agent reached maximum number of steps before finishing.",
            mode="react",
            usage=usage_totals,
            metadata={
                "rollback_enabled": self.enable_rollback,
                **rollback_metrics,
            },
        )