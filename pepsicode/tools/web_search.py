from __future__ import annotations

import json
import os
import urllib.error

from pepsicode.cancellation import cancellable_response
from pepsicode.execution.external import tavily_search
from pepsicode.tooling import ToolDefinition, ToolResult

MAX_RESULTS = 10


def _validate(input_data: dict) -> dict:
    query = input_data.get("query")
    if not isinstance(query, str) or not query.strip():
        raise ValueError("query is required and must be non-empty")
    num_results = int(input_data.get("num_results", 5))
    if num_results < 1 or num_results > MAX_RESULTS:
        raise ValueError(f"num_results must be between 1 and {MAX_RESULTS}")
    return {"query": query.strip(), "num_results": num_results}


def _run(input_data: dict, context) -> ToolResult:
    key = os.environ.get("TAVILY_API_KEY", "").strip()
    if not key:
        return ToolResult(False, "Tavily API key is not configured. Set TAVILY_API_KEY and restart PepsiCode.")

    try:
        with cancellable_response(
            tavily_search(input_data["query"], input_data["num_results"], key, context=context)
        ) as response:
            raw = response.read(1_000_001)
            context.effect_state = "completed"
        if len(raw) > 1_000_000:
            return ToolResult(False, "Tavily response exceeded the size limit.")
        payload = json.loads(raw)
        if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
            return ToolResult(False, "Tavily returned an invalid response.")
        results = payload["results"][: input_data["num_results"]]
        lines = [f"Search results for: {input_data['query']}", ""]
        for result in results:
            if not isinstance(result, dict):
                continue
            title, url, content = result.get("title"), result.get("url"), result.get("content")
            if not isinstance(title, str) or not isinstance(url, str) or not url.startswith(("https://", "http://")):
                continue
            snippet = content[:1000] if isinstance(content, str) else ""
            lines.extend([f"{len(lines) // 4 + 1}. {title[:300]}", f"   URL: {url[:2000]}", f"   {snippet}", ""])
        if len(lines) == 2:
            return ToolResult(False, "Tavily returned no usable results.")
        return ToolResult(True, "\n".join(lines))
    except urllib.error.HTTPError as error:
        if 400 <= error.code < 500:
            context.effect_state = "completed"
        explanations = {
            401: "Invalid Tavily API key.",
            429: "Tavily rate limit reached.",
            432: "Tavily usage limit reached.",
            433: "Tavily usage limit reached.",
        }
        return ToolResult(False, explanations.get(error.code, f"Tavily request failed (HTTP {error.code})."))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return ToolResult(False, "Tavily returned an invalid JSON response.")
    except urllib.error.URLError:
        return ToolResult(False, "Tavily request failed; check network access.")


web_search_tool = ToolDefinition(
    name="web_search",
    description="Search the web using Tavily. Requires TAVILY_API_KEY; searches consume Tavily credits.",
    input_schema={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "The search query"},
            "num_results": {"type": "number", "description": "Number of results to return (1-10, default: 5)"},
        },
        "required": ["query"],
    },
    validator=_validate,
    run=_run,
)
