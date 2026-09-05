"""MCP target connections; transport details stay out of campaign orchestration."""
from contextlib import AsyncExitStack, asynccontextmanager
from datetime import timedelta
import os

import httpx
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import get_default_environment, stdio_client
from mcp.client.streamable_http import streamable_http_client

from elengtis.config import StdioTransport
from elengtis.scenario import resolve_value


@asynccontextmanager
async def open_target(target, values, request_timeout=10):
    """Open and initialize one fresh client session without shell evaluation."""
    transport = target.transport
    async with AsyncExitStack() as stack:
        if isinstance(transport, StdioTransport):
            configured = {name: os.environ[ref.env] for name, ref in transport.env.items()}
            env = get_default_environment() | configured
            params = StdioServerParameters(
                command=transport.command,
                args=[str(resolve_value(arg, values)) for arg in transport.args], env=env)
            read, write = await stack.enter_async_context(stdio_client(params))
        else:
            headers = {name: os.environ[ref.env] for name, ref in transport.headers.items()}
            http = await stack.enter_async_context(httpx.AsyncClient(headers=headers))
            read, write, _ = await stack.enter_async_context(
                streamable_http_client(transport.url, http_client=http, terminate_on_close=False))
        client = await stack.enter_async_context(ClientSession(
            read, write, read_timeout_seconds=timedelta(seconds=request_timeout)))
        await client.initialize()
        yield client


async def agent_tools(client, allowed):
    """Return only explicitly model-facing tools and reject misspelled names."""
    discovered = (await client.list_tools()).tools
    if allowed == 'all':
        return discovered
    by_name = {tool.name: tool for tool in discovered}
    missing = set(allowed) - by_name.keys()
    if missing:
        raise ValueError(f'Target is missing exercise tools {sorted(missing)}')
    return [by_name[name] for name in allowed]
