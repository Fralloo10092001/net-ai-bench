from __future__ import annotations

import json
from typing import Any

from mcp.client.session import ClientSession

from .base import Agent, RunResult
from .usage_utils import add_usage, extract_usage_from_response


PLANNER_SYSTEM_PROMPT = """
You are the PLANNER for a network automation agent.

You must produce a concise execution plan for the task.
You do NOT call tools.
You do NOT output commands.
You only output a JSON array of plan steps.

Each step must have this schema:
{
  "step_id": 1,
  "description": "What should be achieved in this step",
  "target_device": "device-name"
}

Rules:
- Return valid JSON only.
- Return a JSON array.
- step_id must be an integer.
- description must be a string.
- target_device must be a string.
- Keep the plan concise and actionable.
- Prefer device-scoped steps.
- Do not use markdown.
- The plan should preserve healthy pre-existing system state unless the task explicitly requires changing it.
- Do not plan destructive changes to management interfaces, management routes, or unrelated control-plane state unless explicitly required by the task.
- Prefer additive and minimal plans over replacement-oriented plans.
""".strip()


EXECUTOR_SYSTEM_PROMPT = """
You are the EXECUTOR for a network automation agent.

You are executing one step of a previously generated plan.
You may use tools to inspect state, apply configuration, validate progress, and optionally recover from a recent harmful change.

IMPORTANT:
- Devices may use different CLIs and operating systems.
- Use only the exact tool names provided.
- Do NOT invent tool names.
- Apply only the changes needed for the current plan step.
- If the current plan step is complete, return action="finish".
- Preserve healthy pre-existing system state unless the current plan step explicitly requires changing it.
- Do not remove or replace existing management connectivity unless explicitly required by the task.
- Prefer additive and minimal changes over destructive replacements.
- Before deleting or replacing an existing route, address, or setting, verify that it is necessary for the current plan step.
- Avoid modifying management interfaces, management routes, or unrelated control-plane state if the step can be completed without touching them.

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
- If action == "tool_call", tool_name must be valid and tool_args must be a JSON object.
- If action == "finish", include final_reasoning.
- No markdown.
- No code fences.
- No extra text.
""".strip()


VALIDATOR_SYSTEM_PROMPT = """
You are the FINAL VALIDATOR for a network automation agent.

You will receive:
- the task
- the finished execution plan
- testcase results

Your job is to summarize whether the overall task goal appears satisfied.

Return valid JSON only with schema:
{
  "final_reasoning": "..."
}

Do not use markdown.
""".strip()


# Pulisce l'output del modello rimuovendo eventuali fence markdown
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


# Converte il task JSON serializzato in un dizionario
def _load_task_dict(task_text: str) -> dict[str, Any]:
    return json.loads(task_text)


# Valida e converte l'output del planner in un piano JSON
def _parse_plan(text: str) -> list[dict[str, Any]]:
    text = _clean_model_output(text)

    try:
        data = json.loads(text)
    except Exception:
        raise ValueError(f"Planner output is not valid JSON:\n{text}")

    if not isinstance(data, list):
        raise ValueError("Planner output must be a JSON array")

    for item in data:
        if not isinstance(item, dict):
            raise ValueError("Each plan step must be an object")
        for key in ["step_id", "description", "target_device"]:
            if key not in item:
                raise ValueError(f"Missing key in plan step: {key}")

    return data


# Valida e converte l'output dell'executor in una decisione JSON
def _parse_executor_decision(text: str) -> dict[str, Any]:
    text = _clean_model_output(text)

    try:
        data = json.loads(text)
    except Exception:
        raise ValueError(f"Executor output is not valid JSON:\n{text}")

    if "action" not in data:
        raise ValueError("Missing key in executor output: ['action']")

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


# Valida e converte l'output del validator finale
def _parse_validator_output(text: str) -> str:
    text = _clean_model_output(text)

    try:
        data = json.loads(text)
    except Exception:
        return "Final validation completed."

    if isinstance(data, dict) and isinstance(data.get("final_reasoning"), str):
        return data["final_reasoning"]

    return "Final validation completed."


# Elenca tutti i tool MCP disponibili
async def _list_tool_names(session: ClientSession) -> list[str]:
    tools = await session.list_tools()
    return [t.name for t in tools.tools]


# Costruisce dinamicamente la sezione dei tool disponibili per l'executor
def _build_executor_available_tools_text(enable_rollback: bool) -> str:
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


# Filtra i tool di rollback quando il rollback è disabilitato
def _filter_tool_names(valid_tool_names: list[str], enable_rollback: bool) -> list[str]:
    if enable_rollback:
        return valid_tool_names

    blocked = {"rollback", "get_rollback_status"}
    return [name for name in valid_tool_names if name not in blocked]


# Conta l'uso dei tool di rollback all'interno della history
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


# Costruisce il prompt di un singolo step dell'executor
def _build_executor_prompt(
    task_text: str,
    full_plan: list[dict[str, Any]],
    current_step: dict[str, Any],
    valid_tool_names: list[str],
    history: list[dict[str, Any]],
    enable_rollback: bool,
    parse_warning: str = "",
) -> str:
    parts = []

    if parse_warning:
        parts.append(parse_warning)

    available_tools_text = _build_executor_available_tools_text(enable_rollback)

    rollback_guidance = ""
    if enable_rollback:
        rollback_guidance = """
ROLLBACK GUIDANCE:
- If a recent configuration change appears to have broken a device state, management reachability, or task progress, you may use rollback(device) to restore the most recent saved snapshot for that device.
- Prefer rollback over piling on additional speculative fixes when a recent change likely caused the problem.
- Before using rollback, use the execution history and current observations to identify which device was most likely affected by the failed or harmful change.
- Use get_rollback_status when useful to check whether rollback snapshots are available.
- Use rollback only from the executor. The planner does not perform recovery actions directly.
""".strip()

    parts.append(
        f"""
CURRENT TASK:
{task_text}

FULL PLAN:
{json.dumps(full_plan, indent=2)}

CURRENT PLAN STEP:
{json.dumps(current_step, indent=2)}

{available_tools_text}

VALID TOOL NAMES:
{json.dumps(valid_tool_names, indent=2)}

EXECUTION HISTORY FOR THIS PLAN STEP:
{json.dumps(history, indent=2)}

{rollback_guidance}

INSTRUCTIONS:
- Focus on completing only the CURRENT PLAN STEP.
- Prefer the target_device specified by the plan.
- Inspect state before changing configuration when needed.
- Apply only missing or incorrect configuration.
- Validate enough to know whether the step is complete.
- Return finish as soon as the current plan step is completed.
- Return raw JSON only.
""".strip()
    )

    return "\n\n".join(part for part in parts if part.strip())


# Esegue i testcase finali usando il tool di validazione
async def _run_testcases(
    session: ClientSession,
    testcases: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []

    for testcase in testcases:
        device = testcase.get("device")
        command = testcase.get("command")
        expected_output = testcase.get("expected_output")
        name = testcase.get("name", f"{device}:{command}")

        entry: dict[str, Any] = {
            "name": name,
            "device": device,
            "command": command,
            "expected_output": expected_output,
            "status": "error",
        }

        if not device or not command:
            entry["status"] = "invalid_testcase"
            results.append(entry)
            continue

        try:
            tool_result = await session.call_tool(
                "execute_validation",
                {"device": device, "command": command},
            )
            output = _extract_mcp_text(tool_result)

            if isinstance(output, list):
                output = "\n".join(str(x) for x in output)

            output_str = str(output)
            entry["output"] = output_str
            entry["status"] = "pass" if expected_output and expected_output in output_str else "fail"
        except Exception as e:
            entry["status"] = "error"
            entry["output"] = f"ERROR: {e}"

        results.append(entry)

    return results


class PlannerExecutorAgent(Agent):
    # Inizializza l'agente planner-executor con planner, executor e validator opzionali
    def __init__(
        self,
        llm,
        executor_step_budget: int = 3,
        planner_llm=None,
        executor_llm=None,
        validator_llm=None,
        planner_provider: str | None = None,
        planner_model: str | None = None,
        executor_provider: str | None = None,
        executor_model: str | None = None,
        enable_rollback: bool = False,
    ):
        self.llm = llm
        self.planner_llm = planner_llm or llm
        self.executor_llm = executor_llm or llm
        self.validator_llm = validator_llm or self.executor_llm
        self.executor_step_budget = executor_step_budget
        self.planner_provider = planner_provider
        self.planner_model = planner_model
        self.executor_provider = executor_provider
        self.executor_model = executor_model
        self.enable_rollback = enable_rollback

    # Esegue l'intero workflow planner-executor sulla sessione MCP
    async def run(self, session: ClientSession, task_text: str, max_steps: int) -> RunResult:
        del max_steps

        history: list[dict[str, Any]] = []
        valid_tool_names = await _list_tool_names(session)
        valid_tool_names = _filter_tool_names(valid_tool_names, self.enable_rollback)
        task = _load_task_dict(task_text)

        usage_totals = {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
        }
        usage_breakdown = {
            "planner": {
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
            },
            "executor": {
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
            },
            "validator": {
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
            },
        }

        planner_prompt = f"""
{PLANNER_SYSTEM_PROMPT}

CURRENT TASK:
{task_text}

Return only the JSON plan array.
""".strip()

        planner_response = await self.planner_llm.ainvoke(planner_prompt)
        planner_usage = extract_usage_from_response(planner_response)
        usage_breakdown["planner"] = add_usage(usage_breakdown["planner"], planner_usage)
        usage_totals = add_usage(usage_totals, planner_usage)

        planner_raw = planner_response.content if hasattr(planner_response, "content") else str(planner_response)

        if isinstance(planner_raw, list):
            planner_raw = "".join(str(x) for x in planner_raw)

        try:
            plan = _parse_plan(str(planner_raw))
        except Exception as e:
            history.append(
                {
                    "step": 1,
                    "thought": "Planner output could not be parsed",
                    "action": "parse_error",
                    "raw_output": str(planner_raw),
                    "observation": f"ERROR: {e}",
                }
            )
            return RunResult(
                history=history,
                final_reasoning="Planner-executor agent failed because planner output was invalid.",
                mode="planner_executor",
                usage=usage_totals,
                usage_breakdown=usage_breakdown,
                metadata={
                    "planner_provider": self.planner_provider,
                    "planner_model": self.planner_model,
                    "executor_provider": self.executor_provider,
                    "executor_model": self.executor_model,
                    "rollback_enabled": self.enable_rollback,
                    **_collect_rollback_metrics(history),
                },
            )

        history.append(
            {
                "step": 1,
                "thought": "Generated execution plan.",
                "action": "plan_created",
                "plan": plan,
            }
        )

        global_step = 2

        for plan_step in plan:
            local_history: list[dict[str, Any]] = []

            for _ in range(self.executor_step_budget):
                parse_warning = ""
                if local_history and local_history[-1].get("action") == "parse_error":
                    parse_warning = """
IMPORTANT LAST ERROR:
Your previous response was rejected because it was not raw JSON.
Return ONLY raw JSON.
tool_args must be a JSON object, never null.
""".strip()

                prompt = EXECUTOR_SYSTEM_PROMPT + "\n\n" + _build_executor_prompt(
                    task_text=task_text,
                    full_plan=plan,
                    current_step=plan_step,
                    valid_tool_names=valid_tool_names,
                    history=local_history[-4:],
                    enable_rollback=self.enable_rollback,
                    parse_warning=parse_warning,
                )

                response = await self.executor_llm.ainvoke(prompt)
                executor_usage = extract_usage_from_response(response)
                usage_breakdown["executor"] = add_usage(usage_breakdown["executor"], executor_usage)
                usage_totals = add_usage(usage_totals, executor_usage)

                raw_output = response.content if hasattr(response, "content") else str(response)

                if isinstance(raw_output, list):
                    raw_output = "".join(str(x) for x in raw_output)

                try:
                    decision = _parse_executor_decision(str(raw_output))
                except Exception as e:
                    entry = {
                        "step": global_step,
                        "thought": "Executor output could not be parsed",
                        "action": "parse_error",
                        "raw_output": str(raw_output),
                        "observation": f"ERROR: {e}",
                        "plan_step": plan_step,
                    }
                    local_history.append(entry)
                    history.append(entry)
                    global_step += 1
                    continue

                entry: dict[str, Any] = {
                    "step": global_step,
                    "thought": decision["thought"],
                    "action": decision["action"],
                    "plan_step": plan_step,
                }

                if decision["action"] == "finish":
                    entry["final_reasoning"] = decision.get("final_reasoning", "") or decision["thought"]
                    local_history.append(entry)
                    history.append(entry)
                    global_step += 1
                    break

                tool_name = decision["tool_name"]
                tool_args = decision["tool_args"]

                entry["tool_name"] = tool_name
                entry["tool_args"] = tool_args

                if tool_name not in valid_tool_names:
                    entry["observation"] = (
                        f"ERROR: invalid tool name '{tool_name}'. "
                        f"Use one of: {valid_tool_names}"
                    )
                    local_history.append(entry)
                    history.append(entry)
                    global_step += 1
                    continue

                try:
                    result = await session.call_tool(tool_name, tool_args)
                    entry["observation"] = _extract_mcp_text(result)
                except Exception as e:
                    entry["observation"] = f"Error executing tool {tool_name}: {e}"

                local_history.append(entry)
                history.append(entry)
                global_step += 1

        testcases = task.get("testcases") or []
        testcase_results = await _run_testcases(session, testcases)

        passed = sum(1 for x in testcase_results if x["status"] == "pass")
        total = len(testcase_results)

        history.append(
            {
                "step": global_step,
                "thought": "Executed final testcase validation after completing all planned steps.",
                "action": "validation_summary",
                "observation": {
                    "passed": passed,
                    "total": total,
                    "results": testcase_results,
                },
            }
        )
        global_step += 1

        validator_prompt = f"""
{VALIDATOR_SYSTEM_PROMPT}

CURRENT TASK:
{task_text}

FINISHED PLAN:
{json.dumps(plan, indent=2)}

TESTCASE RESULTS:
{json.dumps(testcase_results, indent=2)}

Return the final JSON object now.
""".strip()

        validator_response = await self.validator_llm.ainvoke(validator_prompt)
        validator_usage = extract_usage_from_response(validator_response)
        usage_breakdown["validator"] = add_usage(usage_breakdown["validator"], validator_usage)
        usage_totals = add_usage(usage_totals, validator_usage)

        validator_raw = validator_response.content if hasattr(validator_response, "content") else str(validator_response)

        if isinstance(validator_raw, list):
            validator_raw = "".join(str(x) for x in validator_raw)

        final_reasoning = _parse_validator_output(str(validator_raw))

        if total > 0:
            final_reasoning = (
                f"{final_reasoning}\n\n"
                f"Final validation result: passed {passed}/{total} testcases."
            )

        history.append(
            {
                "step": global_step,
                "thought": final_reasoning,
                "action": "finish",
                "final_reasoning": final_reasoning,
            }
        )

        return RunResult(
            history=history,
            final_reasoning=final_reasoning,
            mode="planner_executor",
            usage=usage_totals,
            usage_breakdown=usage_breakdown,
            metadata={
                "planner_provider": self.planner_provider,
                "planner_model": self.planner_model,
                "executor_provider": self.executor_provider,
                "executor_model": self.executor_model,
                "rollback_enabled": self.enable_rollback,
                **_collect_rollback_metrics(history),
            },
        )