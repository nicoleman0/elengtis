"""MCP target connections and the hardened local container boundary."""
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from datetime import timedelta
import asyncio
import json
import os
from pathlib import Path
import re
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
    container_id: str
    relay_name: str | None = None
    relay_image_id: str | None = None
    endpoint: str | None = None
    cleanup_ok: bool | None = None
    cleanup_errors: list[str] | None = None

    def metadata(self):
        metadata = {
            'isolation': 'isolated_container',
            'image_id': self.image_id,
            'container_id': self.container_id,
            'session': 'fresh_container',
            'reset_asserted': True,
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
        if self.relay_image_id:
            metadata['relay'] = {'trusted': True, 'image_id': self.relay_image_id,
                                 'localhost_only': True}
        return metadata

    def cleanup(self):
        errors = []
        containers = [self.name] + ([self.relay_name] if self.relay_name else [])
        for args in ([['rm', '--force', name] for name in containers] +
                     [['network', 'rm', self.network]]):
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


RELAY_PORT = 3001
RELAY_SCRIPT = '''
import socket, socketserver, sys, threading, time
target, port = sys.argv[1], int(sys.argv[2])
def pipe(source, destination):
    try:
        while data := source.recv(65536):
            destination.sendall(data)
    except OSError:
        pass
    try:
        destination.shutdown(socket.SHUT_WR)
    except OSError:
        pass
class Handler(socketserver.BaseRequestHandler):
    def handle(self):
        for _ in range(300):
            try:
                remote = socket.create_connection((target, port), timeout=1)
                break
            except OSError:
                time.sleep(.1)
        else:
            return
        with remote:
            remote.settimeout(None)
            upstream = threading.Thread(target=pipe, args=(self.request, remote))
            downstream = threading.Thread(target=pipe, args=(remote, self.request))
            upstream.start(); downstream.start()
            upstream.join(); downstream.join()
class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
Server(('0.0.0.0', 3001), Handler).serve_forever()
'''


def docker_relay_args(image, name, target_port):
    """Run a trusted localhost ingress relay; the audited target stays internal-only."""
    return [
        'create', '--pull=never', '--name', name,
        '--label', 'elengtis.managed=true',
        '--publish', f'127.0.0.1::{RELAY_PORT}', '--read-only',
        '--tmpfs=/tmp:rw,noexec,nosuid,size=16m', '--cap-drop=ALL',
        '--security-opt=no-new-privileges:true', '--pids-limit=64', '--memory=64m',
        '--cpus=.25', '--user=65534:65534', '--entrypoint=python3', image,
        '-c', RELAY_SCRIPT, 'target', str(target_port),
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


def _published_port(name):
    output = _docker(['port', name, f'{RELAY_PORT}/tcp'], check=True).stdout.strip()
    match = re.fullmatch(r'127\.0\.0\.1:(\d+)', output)
    if not match:
        raise RuntimeError(f'docker did not publish a localhost relay port: {output!r}')
    return int(match.group(1))


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
        lifecycle = ContainerLifecycle(name, network, image_id, result.stdout.strip())
        if transport.relay_image:
            relay_image = _docker(['image', 'inspect', '--format={{.Id}}',
                                   transport.relay_image], check=True).stdout.strip()
            relay_name = f'elengtis-relay-{uuid.uuid4().hex}'
            lifecycle.relay_name = relay_name
            lifecycle.relay_image_id = relay_image
            relay = _docker(docker_relay_args(
                transport.relay_image, relay_name, transport.container_port), check=True)
            if not relay.stdout.strip():
                raise RuntimeError('docker relay create returned no container ID')
            _docker(['network', 'connect', network, relay_name], check=True)
            _docker(['start', relay_name], check=True)
            return lifecycle, '127.0.0.1', _published_port(relay_name), False
        return lifecycle, _docker_container_ip(name, network), transport.container_port, True
    except Exception:
        try:
            if 'lifecycle' in locals():
                lifecycle.cleanup()
            else:
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


async def _wait_http_ready(client, url, timeout):
    """Wait for an HTTP response, not merely an accepted TCP connection."""
    deadline = asyncio.get_running_loop().time() + timeout
    last_error = None
    while asyncio.get_running_loop().time() < deadline:
        try:
            await client.options(url)
            return
        except httpx.HTTPError as exc:
            last_error = exc
            await asyncio.sleep(.1)
    raise RuntimeError(f'target HTTP endpoint did not become ready: {last_error}')


async def _close_target(stack, lifecycle, relay):
    """Always tear down Docker resources, even when an MCP context fails to close."""
    try:
        if relay:
            relay.close()
            await relay.wait_closed()
    finally:
        try:
            await stack.aclose()
        finally:
            if lifecycle:
                lifecycle.cleanup()


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
                lifecycle, address, port, needs_relay = _open_container(transport)
                if needs_relay:
                    relay = await _localhost_relay(address, port,
                                                   transport.startup_timeout_seconds)
                    host_port = relay.sockets[0].getsockname()[1]
                else:
                    host_port = port
                lifecycle.endpoint = f'127.0.0.1:{host_port}'
                metadata = lifecycle.metadata()
                http = await stack.enter_async_context(
                    httpx.AsyncClient(timeout=transport.startup_timeout_seconds))
                url = f'http://127.0.0.1:{host_port}{transport.path}'
                await _wait_http_ready(http, url, transport.startup_timeout_seconds)
                read, write, _ = await stack.enter_async_context(
                    streamable_http_client(url, http_client=http, terminate_on_close=False))
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
            try:
                await _close_target(stack, lifecycle, relay)
            finally:
                if lifecycle and client:
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
