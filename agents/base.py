"""
Shared agent runtime: the tool-use loop and the trace that records it.

Every agent in this system runs through `run_agent`. Every model call, tool
invocation and SQL statement lands in a Trace, which the UI renders as an audit
panel. That panel is the answer to the only question that matters about an LLM
touching financial numbers: how do you know it did not make them up.
"""

import json
import os
import time
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from typing import Any, Callable

import pandas as pd
from anthropic import Anthropic
from dotenv import load_dotenv

load_dotenv()

MODEL = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-5")
MAX_TOKENS = 4000
MAX_ITERATIONS = 12


# ------------------------------------------------------------------
# Serialisation
# ------------------------------------------------------------------

def json_safe(obj: Any) -> Any:
    """Make engine output JSON-serialisable without losing precision."""
    if isinstance(obj, pd.DataFrame):
        return json.loads(obj.to_json(orient="records", date_format="iso"))
    if isinstance(obj, pd.Series):
        return json_safe(obj.to_frame().T)
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    if isinstance(obj, dict):
        return {str(k): json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [json_safe(v) for v in obj]
    if hasattr(obj, "item"):          # numpy scalar
        try:
            return obj.item()
        except Exception:
            return str(obj)
    return obj


# ------------------------------------------------------------------
# Trace
# ------------------------------------------------------------------

@dataclass
class TraceStep:
    agent: str
    kind: str                 # model_call | tool_call | sql | note
    label: str
    detail: str = ""
    duration_ms: int = 0
    payload: dict = field(default_factory=dict)
    ts: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))


@dataclass
class Trace:
    steps: list = field(default_factory=list)

    def add(self, agent: str, kind: str, label: str, detail: str = "",
            duration_ms: int = 0, **payload) -> None:
        self.steps.append(TraceStep(agent=agent, kind=kind, label=label,
                                    detail=detail, duration_ms=duration_ms,
                                    payload=json_safe(payload)))

    def sql_statements(self) -> list:
        return [s.payload.get("sql", "") for s in self.steps if s.kind == "sql"]

    def agents_used(self) -> list:
        seen, order = set(), []
        for s in self.steps:
            if s.agent not in seen:
                seen.add(s.agent)
                order.append(s.agent)
        return order

    def to_list(self) -> list:
        return [asdict(s) for s in self.steps]

    def render(self) -> str:
        lines = []
        for s in self.steps:
            head = f"[{s.agent}] {s.kind}: {s.label}"
            if s.duration_ms:
                head += f"  ({s.duration_ms} ms)"
            lines.append(head)
            if s.detail:
                lines.append(f"    {s.detail}")
        return "\n".join(lines)


# ------------------------------------------------------------------
# Agent loop
# ------------------------------------------------------------------

@dataclass
class AgentResult:
    text: str
    data: dict = field(default_factory=dict)
    iterations: int = 0


_client = None


def client() -> Anthropic:
    global _client
    if _client is None:
        key = os.getenv("ANTHROPIC_API_KEY")
        if not key or key.startswith("sk-ant-your-key"):
            raise RuntimeError(
                "ANTHROPIC_API_KEY is missing from .env. Add a real key from "
                "console.anthropic.com before running the agent layer."
            )
        _client = Anthropic(api_key=key)
    return _client


def run_agent(name: str, system: str, prompt: str,
              tools: list, handlers: dict[str, Callable],
              trace: Trace, max_iterations: int = MAX_ITERATIONS) -> AgentResult:
    """
    Run one agent to completion.

    The agent may call tools repeatedly; the loop returns once it produces a
    turn with no tool use, or when the iteration cap is hit. Collected tool
    output is returned alongside the text so callers get structured data rather
    than having to parse prose.
    """
    messages = [{"role": "user", "content": prompt}]
    collected: dict = {}
    iterations = 0

    while iterations < max_iterations:
        iterations += 1
        t0 = time.time()
        response = client().messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            temperature=0,          # same question, same route, as far as possible
            system=system,
            tools=tools,
            messages=messages,
        )
        trace.add(name, "model_call", f"turn {iterations}",
                  detail=f"stop_reason={response.stop_reason}",
                  duration_ms=int((time.time() - t0) * 1000),
                  input_tokens=response.usage.input_tokens,
                  output_tokens=response.usage.output_tokens)

        messages.append({"role": "assistant", "content": response.content})

        tool_uses = [b for b in response.content if b.type == "tool_use"]
        if not tool_uses:
            text = "".join(b.text for b in response.content if b.type == "text")
            return AgentResult(text=text.strip(), data=collected,
                               iterations=iterations)

        results = []
        for block in tool_uses:
            handler = handlers.get(block.name)
            t1 = time.time()
            if handler is None:
                payload = {"error": f"unknown tool '{block.name}'"}
            else:
                try:
                    payload = handler(**block.input)
                except Exception as exc:                    # surfaced, not swallowed
                    payload = {"error": f"{type(exc).__name__}: {exc}"}

            # Kept long deliberately: the evaluation harness reads the
            # findings back out of this field, and truncating it made verified
            # figures look unsupported.
            trace.add(name, "tool_call", block.name,
                      detail=json.dumps(json_safe(block.input))[:8000],
                      duration_ms=int((time.time() - t1) * 1000))

            safe = json_safe(payload)
            collected.setdefault(block.name, []).append(safe)
            results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": json.dumps(safe)[:60000],
            })

        messages.append({"role": "user", "content": results})

    trace.add(name, "note", "iteration cap reached",
              detail=f"stopped after {max_iterations} turns")
    return AgentResult(text="Stopped: iteration limit reached.",
                       data=collected, iterations=iterations)