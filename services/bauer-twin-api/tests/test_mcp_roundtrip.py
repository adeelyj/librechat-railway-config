from __future__ import annotations

import argparse
import asyncio
import json

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


async def run(url: str, token: str) -> None:
    headers = {"Authorization": f"Bearer {token}"}
    async with httpx.AsyncClient(headers=headers, timeout=60.0) as http_client:
        async with streamable_http_client(url, http_client=http_client) as (read, write, _):
            async with ClientSession(read, write) as session:
                initialized = await session.initialize()
                tools = await session.list_tools()
                names = [tool.name for tool in tools.tools]
                if "search_bauer_twin" not in names:
                    raise RuntimeError(f"search_bauer_twin was not advertised; got {names}")
                tool = next(item for item in tools.tools if item.name == "search_bauer_twin")
                medium_schema = ((tool.inputSchema or {}).get("properties") or {}).get("medium") or {}
                if '"enum"' in json.dumps(medium_schema):
                    raise RuntimeError(f"MCP medium schema is still closed: {medium_schema}")
                result = await session.call_tool(
                    "search_bauer_twin",
                    {
                        "action": "search_similar_projects",
                        "query": "nitrogen booster 420 bar 500 l/min",
                        "limit": 3,
                    },
                )
                if result.isError:
                    raise RuntimeError(f"MCP tool returned an error: {result.content}")
                structured = result.structuredContent or {}
                if not structured and result.content and hasattr(result.content[0], "text"):
                    structured = json.loads(result.content[0].text)
                top_id = ((structured.get("result") or structured).get("results") or [{}])[0].get("project_id")
                if top_id != "SYN-BK-N2-420-500":
                    raise RuntimeError(f"Unexpected MCP top result: {top_id}; payload={structured}")
                helium_result = await session.call_tool(
                    "search_bauer_twin",
                    {
                        "action": "search_similar_projects",
                        "query": "helium booster 420 bar 500 l/min",
                        "medium": "helium",
                        "target_pressure_bar": 420,
                        "capacity_l_min": 500,
                        "topology": "booster",
                        "limit": 3,
                    },
                )
                if helium_result.isError:
                    raise RuntimeError(f"MCP rejected flexible medium input: {helium_result.content}")
                helium_payload = helium_result.structuredContent or {}
                if not helium_payload and helium_result.content and hasattr(helium_result.content[0], "text"):
                    helium_payload = json.loads(helium_result.content[0].text)
                helium_payload = helium_payload.get("result") or helium_payload
                if helium_payload.get("status") != "no_compatible_match" or helium_payload.get("results"):
                    raise RuntimeError(f"Unexpected helium result: {helium_payload}")
                print(
                    f"MCP round trip passed: server={initialized.serverInfo.name}, "
                    f"tool=search_bauer_twin, top_result={top_id}, helium_status=no_compatible_match"
                )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True)
    parser.add_argument("--token", required=True)
    args = parser.parse_args()
    asyncio.run(run(args.url, args.token))
