import argparse
import json
import re
import sys
from pathlib import Path

from langchain.agents import AgentExecutor, create_react_agent
from langchain_core.prompts import PromptTemplate
from langchain_core.tools import StructuredTool
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


REACT_PROMPT = PromptTemplate.from_template("""
You are a network configuration agent. Your goal is to satisfy the following
network intent using the ANI tools available to you.

## Intent
{intent}

## Available tools
{tools}

## Tool names
{tool_names}

## Instructions
1. Call get_topology to understand the network.
2. Call get_running_config on the relevant devices to inspect their current state.
3. Reason about which commands are needed.
4. Call update_config on the correct device(s) with the required commands.
5. Verify with execute_validation using read-only checks such as ping or route inspection.
6. When done, write a final summary starting with REASONING: that explains
   step-by-step why you chose those commands.

Use this exact format:

Thought: <your reasoning>
Action: <tool name>
Action Input: <tool input as JSON>
Observation: <tool output>
... (repeat as needed)
Thought: I have completed the task.
Final Answer: REASONING: <your explanation>

{agent_scratchpad}
""")


def _make_llm(model_name: str):
    if model_name == "claude":
        from langchain_anthropic import ChatAnthropic
        return ChatAnthropic(model="claude-opus-4-5", temperature=0)
    elif model_name == "openai":
        from langchain_openai import ChatOpenAI
        return ChatOpenAI(model="gpt-4o", temperature=0)
    elif model_name == "gemini":
        from langchain_google_genai import ChatGoogleGenerativeAI
        return ChatGoogleGenerativeAI(model="gemini-1.5-pro", temperature=0)
    else:
        raise ValueError(f"Unknown model: {model_name}. Use: claude | openai | gemini")


def _extract_reasoning(agent_output: str) -> str:
    match = re.search(r"REASONING:\s*(.*)", agent_output, re.DOTALL | re.IGNORECASE)
    return match.group(1).strip() if match else ""


def _extract_text_from_mcp_result(result) -> str:
    texts = [c.text for c in result.content if hasattr(c, "text")]
    return "\n".join(texts)


async def _build_langchain_tools(session) -> list[StructuredTool]:
    tools_response = await session.list_tools()
    lc_tools = []

    for t in tools_response.tools:
        tool_name = t.name
        tool_desc = t.description or ""

        async def acall(tool_name=tool_name, **kwargs):
            result = await session.call_tool(tool_name, arguments=kwargs)
            return _extract_text_from_mcp_result(result)

        lc_tools.append(
            StructuredTool.from_function(
                coroutine=acall,
                name=tool_name,
                description=tool_desc,
            )
        )

    return lc_tools


async def _run_async(lab: str, task_path: Path, model_name: str, log_path: Path) -> dict:
    with open(task_path) as f:
        task = json.load(f)

    intent_text = "\n".join(f"- {i}" for i in task["intents"])

    project_root = Path(__file__).resolve().parent.parent
    mcp_server_path = project_root / "ani" / "mcp_server.py"

    server_params = StdioServerParameters(
        command=sys.executable,
        args=[
            str(mcp_server_path),
            "--lab", lab,
            "--task", str(task_path.resolve()),
            "--log", str(log_path.resolve()),
        ],
    )

    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            lc_tools = await _build_langchain_tools(session)

            llm = _make_llm(model_name)
            agent = create_react_agent(llm, lc_tools, REACT_PROMPT)

            executor = AgentExecutor(
                agent=agent,
                tools=lc_tools,
                verbose=True,
                max_iterations=15,
                handle_parsing_errors=True,
            )

            result = await executor.ainvoke({"intent": intent_text})
            output = result.get("output", "")
            reasoning = _extract_reasoning(output)

            return {
                "output": output,
                "reasoning": reasoning,
            }


def run(lab: str, task_path: Path, model_name: str, log_path: Path) -> dict:
    import asyncio
    return asyncio.run(_run_async(lab, task_path, model_name, log_path))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="NetConfBench Agent Runner")
    parser.add_argument("--lab", required=True, help="Containerlab lab name")
    parser.add_argument("--task", required=True, help="Path to task JSON")
    parser.add_argument("--model", default="claude", choices=["claude", "openai", "gemini"])
    parser.add_argument("--log", default="logs/agent_commands.jsonl", help="Command log output path")
    args = parser.parse_args()

    log_path = Path(args.log).resolve()
    log_path.parent.mkdir(parents=True, exist_ok=True)

    result = run(
        lab=args.lab,
        task_path=Path(args.task).resolve(),
        model_name=args.model,
        log_path=log_path,
    )

    reasoning_file = log_path.parent / "reasoning.txt"
    reasoning_file.write_text(result["reasoning"])

    print("\n" + "=" * 55)
    print("  AGENT COMPLETED")
    print("=" * 55)
    print(f"  Model    : {args.model}")
    print(f"  Lab      : {args.lab}")
    print(f"  Log file : {log_path}")
    print(f"  Reasoning: {result['reasoning'][:120]}{'...' if len(result['reasoning']) > 120 else ''}")
    print("=" * 55)
    print(f"\nReasoning salvato in: {reasoning_file}")