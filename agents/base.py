"""Shared agent: system prompt + allowed tools + an LLM tool-calling loop (OpenAI API)."""
import inspect
import json
from dataclasses import dataclass
from typing import Callable

from core.messages import Task, Result

_TYPES = {str: "string", int: "integer", float: "number", bool: "boolean"}


@dataclass
class Tool:
    name: str
    description: str
    input_schema: dict
    fn: Callable

    def spec(self) -> dict:
        return {"type": "function",
                "function": {"name": self.name, "description": self.description,
                             "parameters": self.input_schema}}


def make_tool(fn: Callable, description: str, name: str = None) -> Tool:
    """Build a tool spec from a function signature (type hints + defaults)."""
    sig = inspect.signature(fn)
    props, required = {}, []
    for p in sig.parameters.values():
        props[p.name] = {"type": _TYPES.get(p.annotation, "string")}
        if p.default is inspect.Parameter.empty:
            required.append(p.name)
    schema = {"type": "object", "properties": props, "required": required}
    return Tool(name or fn.__name__, description, schema, fn)


class LLM:
    """Thin wrapper over the OpenAI Chat Completions API (swappable in tests)."""

    def __init__(self, model: str):
        from openai import OpenAI
        self.model = model
        self.client = OpenAI()                       # reads OPENAI_API_KEY from env

    def chat(self, system, messages, tools=None):
        """One turn of a tool-calling conversation. Returns the assistant message."""
        kwargs = dict(model=self.model,
                      messages=[{"role": "system", "content": system}] + messages)
        if tools:
            kwargs["tools"] = tools
            kwargs["parallel_tool_calls"] = False    # keep dependent AWS steps strictly ordered
        try:
            return self.client.chat.completions.create(**kwargs).choices[0].message
        except Exception as e:
            # Some (reasoning) models reject parallel_tool_calls. Retry without it; the agent
            # loop still runs multiple tool calls one at a time, in order.
            if "parallel_tool_calls" in str(e) and "parallel_tool_calls" in kwargs:
                del kwargs["parallel_tool_calls"]
                return self.client.chat.completions.create(**kwargs).choices[0].message
            raise

    def complete_json(self, system, user) -> str:
        """Single JSON-mode completion (no tools). Used by the Planner."""
        r = self.client.chat.completions.create(
            model=self.model, response_format={"type": "json_object"},
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}])
        return r.choices[0].message.content


RESULT_INSTRUCTIONS = """
When you are finished, reply with ONE JSON object and nothing else:
{"status": "success" | "failed" | "denied", "summary": "<one or two sentences>", "data": {...}}
Use "denied" if a tool returned status denied_by_human. Never claim success without tool evidence.
"""


def extract_json(text: str):
    """Find the first valid JSON object in the text (ignores prose or extra braces around it)."""
    if not text:
        return None
    dec = json.JSONDecoder()
    pos = text.find("{")
    while pos != -1:
        try:
            obj, _ = dec.raw_decode(text, pos)
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            pass
        pos = text.find("{", pos + 1)
    return None


class Agent:
    def __init__(self, name, system_prompt, tools, llm, logger, max_steps=25):
        self.name = name
        self.system_prompt = system_prompt.strip() + "\n" + RESULT_INSTRUCTIONS
        self.tools = {t.name: t for t in tools}
        self.llm = llm
        self.logger = logger
        self.max_steps = max_steps

    def run(self, task: Task) -> Result:
        messages = [{"role": "user", "content": task.to_json()}]
        specs = [t.spec() for t in self.tools.values()]
        retried_format = False
        for _ in range(self.max_steps):
            msg = self.llm.chat(self.system_prompt, messages, specs)

            if msg.tool_calls:
                messages.append({
                    "role": "assistant", "content": msg.content,
                    "tool_calls": [{"id": tc.id, "type": "function",
                                    "function": {"name": tc.function.name,
                                                 "arguments": tc.function.arguments}}
                                   for tc in msg.tool_calls]})
                for tc in msg.tool_calls:
                    name = tc.function.name
                    try:
                        args = json.loads(tc.function.arguments or "{}")
                    except json.JSONDecodeError:
                        args = None
                    self.logger.log(self.name, "tool_call", {"tool": name, "args": args})
                    tool = self.tools.get(name)
                    if tool is None:
                        out, err = {"error": f"tool '{name}' not available to {self.name}"}, True
                    elif args is None:
                        out, err = {"error": "tool arguments were not valid JSON"}, True
                    else:
                        try:
                            out, err = tool.fn(**args), False
                        except Exception as e:       # report failures to the LLM, don't crash
                            out, err = {"error": f"{type(e).__name__}: {e}"}, True
                    self.logger.log(self.name, "tool_result",
                                    {"tool": name, "error": err, "output": out})
                    messages.append({"role": "tool", "tool_call_id": tc.id,
                                     "content": json.dumps(out, default=str)[:20000]})
                continue

            parsed = extract_json(msg.content or "")
            if not parsed and not retried_format:
                # Ask the model once to resend its answer as valid JSON before giving up.
                retried_format = True
                self.logger.log(self.name, "invalid_json_retry", {"raw": (msg.content or "")[:500]})
                messages.append({"role": "assistant", "content": msg.content or ""})
                messages.append({"role": "user", "content":
                                 "Your last reply was not one valid JSON object. Reply again with "
                                 "ONLY the single JSON object described in your instructions: "
                                 "no prose, no code fences, exactly one proposed fix."})
                continue
            if not parsed:
                return Result(task.id, self.name, "failed", "agent did not return valid JSON",
                              {"raw": (msg.content or "")[:500]})
            return Result(task.id, self.name, parsed.get("status", "failed"),
                          parsed.get("summary", ""), parsed.get("data", {}))

        return Result(task.id, self.name, "failed", f"exceeded {self.max_steps} steps")