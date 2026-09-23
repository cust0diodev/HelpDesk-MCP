"""Valida handshake stdio e ferramentas; nao consulta nenhuma API real."""
import asyncio
import os
from pathlib import Path
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def main():
    params = StdioServerParameters(command=sys.executable,
        args=[str(Path(__file__).with_name("server.py"))],
        env={**os.environ, "TACTICAL_API_KEY": "", "TACTICAL_BASE_URL": "https://invalid.example"})
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            listed = await session.list_tools()
            names = {tool.name for tool in listed.tools}
            assert names == {"get_tactical_alerts", "get_tactical_agents", "get_turbodesk_tickets", "get_zabbix_problems", "get_grafana_catalog", "get_grafana_dashboard_metadata"}, names
            assert all(tool.annotations.readOnlyHint for tool in listed.tools)
            result = await session.call_tool("get_tactical_alerts", {})
            assert result.isError, "Sem chave, deve falhar sem consultar rede."
            print("OK: handshake MCP, schemas, tools somente consulta e erro de configuracao.")


if __name__ == "__main__":
    asyncio.run(main())
