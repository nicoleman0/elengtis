"""MCP target connections and the hardened local container boundary."""
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from datetime import timedelta
import asyncio
import json
import os
from pathlib import Path
import subprocess
import tempfile
import uuid

import httpx
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import get_default_environment, stdio_client
from mcp.client.streamable_http import streamable_http_client

from elengtis.config import ContainerTransport, StdioTransport
from elengtis.scenario import resolve_value


@dataclass
class ContainerLifecycle:
    name: str
    network: str
    image_id: str
    endpoint: str | None = None
    cleanup_ok: bool | None = None
    cleanup_errors: list[str] | None = None

    def metadata(self):
        return {
            'isolation': 'isolated_container',
            'image_id': self.image_id,
            'network': {'internal': True, 'name': self.network},
            'endpoint': self.endpoint,
            'hardening': {
                'read_only_root': True,
                'tmpfs': '/tmp',
                'cap_drop': 'ALL',
                'no_new_privileges': True,
                'host_mounts': False,
                'devices': False,
                'docker_socket': False,
                'egress': 'denied',
            },
            'cleanup': {'ok': self.cleanup_ok, 'errors': self.cleanup_errors or []},
        }

    def cleanup(self):
        errors = []
        for args in (['rm', '--force', self.name], ['network', 'rm', self.network]):
            try:
                _docker(args, check=True)
            except Exception as exc:  # cleanup evidence must survive teardown failures
                errors.append(f'{type(exc).__name__}: {exc}')
        self.cleanup_errors = errors
        self.cleanup_ok = not errors


def _docker(args, *, check=False):
    return subprocess.run(['docker', *args], capture_output=True, text=True, check=check)


def docker_run_args(transport: ContainerTransport, name, network, env_file):
    return [
        'run', '--detach', '--pull=never', '--name', name,
        '--label', 'elengtis.managed=true', '--label', f'elengtis.network={network}',
        '--network', network, '--network-alias', 'target',
        '--read-only', '--tmpfs=/tmp:rw,noexec,nosuid,size=64m', '--cap-drop=ALL',
        '--security-opt=no-new-privileges:true', '--pids-limit=128', '--memory=512m',
        '--cpus=1', f'--user={transport.uid}:{transport.gid}', '--env-file', env_file,
        transport.image, *transport.command,
    ]


def unmanaged_target_metadata():
    return {
        'isolation': 'externally_managed',
        'session': 'fresh_client',
        'reset_asserted': False,
        'egress_controlled': False,
    }


def target_metadata(client):
    return getattr(client, '_elengtis_target_metadata', {})


def _docker_container_ip(name, network):
    result = _docker(['inspect', '--format={{json .NetworkSettings.Networks}}', name], check=True)
    networks = json.loads(result.stdout)
    address = networks.get(network, {}).get('IPAddress')
    if not address:
        raise RuntimeError(f'docker did not assign an address on the isolated network: {network}')
    return address


def _write_env_file(transport):
    handle = tempfile.NamedTemporaryFile('w', prefix='elengtis-', suffix='.env', delete=False)
    try:
        os.chmod(handle.name, 0o600)
        for name, ref in transport.env.items():
            handle.write(f'{name}={os.environ[ref.env]}\n')
        handle.close()
        return handle.name
    except Exception:
        handle.close()
        Path(handle.name).unlink(missing_ok=True)
        raise


def _open_container(transport):
    image = _docker(['image', 'inspect', '--format={{.Id}}', transport.image], check=True)
    image_id = image.stdout.strip()
    if not image_id:
        raise RuntimeError(f'local Docker image has no immutable ID: {transport.image}')
    network = f'elengtis-{uuid.uuid4().hex}'
    name = f'elengtis-{uuid.uuid4().hex}'
    _docker(['network', 'create', '--driver', 'bridge', '--internal',
             '--label', 'elengtis.managed=true', network], check=True)
    env_file = None
    try:
        env_file = _write_env_file(transport)
        result = _docker(docker_run_args(transport, name, network, env_file), check=True)
        if not result.stdout.strip():
            raise RuntimeError('docker run returned no container ID')
        return ContainerLifecycle(name, network, image_id), _docker_container_ip(name, network)
    except Exception:
        try:
            _docker(['rm', '--force', name])
            _docker(['network', 'rm', network])
        finally:
            if env_file:
                Path(env_file).unlink(missing_ok=True)
        raise
    finally:
        if env_file:
            Path(env_file).unlink(missing_ok=True)


async def _pipe(reader, writer):
    try:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
    finally:
        writer.close()
        await writer.wait_closed()


class _LocalhostRelay:
    def __init__(self, server, handlers):
        self.server = server
        self.handlers = handlers

    @property
    def sockets(self):
        return self.server.sockets

    def close(self):
        self.server.close()
        for task in tuple(self.handlers):
            task.cancel()

    async def wait_closed(self):
        await self.server.wait_closed()
        if self.handlers:
            await asyncio.gather(*tuple(self.handlers), return_exceptions=True)


async def _localhost_relay(address, port, timeout):
    handlers = set()

    async def relay(reader, writer):
        handlers.add(asyncio.current_task())
        try:
            deadline = asyncio.get_running_loop().time() + timeout
            while True:
                try:
                    remote_reader, remote_writer = await asyncio.open_connection(address, port)
                    break
                except OSError:
                    if asyncio.get_running_loop().time() >= deadline:
                        raise
                    await asyncio.sleep(.1)
            tasks = [asyncio.create_task(_pipe(reader, remote_writer)),
                     asyncio.create_task(_pipe(remote_reader, writer))]
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            await asyncio.gather(*done, *pending, return_exceptions=True)
        finally:
            handlers.discard(asyncio.current_task())
            writer.close()
            await writer.wait_closed()

    return _LocalhostRelay(await asyncio.start_server(relay, '127.0.0.1', 0), handlers)


@asynccontextmanager
async def open_target(target, values, request_timeout=10):
    """Open and initialize one fresh client session without shell evaluation."""
    transport = target.transport
    lifecycle = None
    relay = None
    metadata = unmanaged_target_metadata() if transport.type == 'streamable_http' else {}
    client = None
    async with AsyncExitStack() as stack:
        try:
            if isinstance(transport, StdioTransport):
                configured = {name: os.environ[ref.env] for name, ref in transport.env.items()}
                env = get_default_environment() | configured
                params = StdioServerParameters(
                    command=transport.command,
                    args=[str(resolve_value(arg, values)) for arg in transport.args], env=env)
                read, write = await stack.enter_async_context(stdio_client(params))
            elif isinstance(transport, ContainerTransport):
                lifecycle, container_ip = _open_container(transport)
                relay = await _localhost_relay(container_ip, transport.container_port,
                                               transport.startup_timeout_seconds)
                host_port = relay.sockets[0].getsockname()[1]
                lifecycle.endpoint = f'127.0.0.1:{host_port}'
                metadata = lifecycle.metadata()
                http = await stack.enter_async_context(
                    httpx.AsyncClient(timeout=transport.startup_timeout_seconds))
                read, write, _ = await stack.enter_async_context(
                    streamable_http_client(f'http://127.0.0.1:{host_port}{transport.path}',
                                           http_client=http, terminate_on_close=False))
            else:
                headers = {name: os.environ[ref.env] for name, ref in transport.headers.items()}
                http = await stack.enter_async_context(httpx.AsyncClient(headers=headers))
                read, write, _ = await stack.enter_async_context(
                    streamable_http_client(transport.url, http_client=http, terminate_on_close=False))
            timeout = (transport.startup_timeout_seconds
                       if isinstance(transport, ContainerTransport) else request_timeout)
            client = await stack.enter_async_context(ClientSession(
                read, write, read_timeout_seconds=timedelta(seconds=timeout)))
            await client.initialize()
            client._elengtis_target_metadata = metadata
            yield client
        finally:
            if relay:
                relay.close()
                await relay.wait_closed()
            await stack.aclose()
            if lifecycle:
                lifecycle.cleanup()
                if client:
                    client._elengtis_target_metadata = lifecycle.metadata()


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
