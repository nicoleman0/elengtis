"""LangChain model, message and MCP tool abstractions over the Phase 1A graph.

Both engines here drive the same scripted policies as the reference loop, so any
difference in requests, ordering, errors or termination is attributable to the
framework rather than to the fixture.
"""
import json
from typing import Any

from langchain.agents import create_agent
from langchain.agents.middleware import ModelCallLimitMiddleware, ToolErrorMiddleware
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.utils.function_calling import convert_to_openai_tool
from langchain_mcp_adapters.tools import load_mcp_tools
from pydantic import Field

from elengtis import graph
from elengtis.reference import SYSTEM, TASK
from elengtis.scenario import ATTACK_TOOLS, score

ROLES = {'system': 'system', 'human': 'user', 'ai': 'assistant', 'tool': 'tool'}


def text_of(message):
    """Adapted tool results arrive as a string or content blocks; the reference joins text."""
    if isinstance(message.content, str):
        return message.content
    return '\n'.join(block['text'] for block in message.content
                     if isinstance(block, dict) and block.get('type') == 'text')


def to_baseline(messages):
    """LangChain messages in the benchmark's evidence format."""
    converted = []
    for message in messages:
        entry = {'role': ROLES[message.type], 'content': text_of(message)}
        if message.type == 'tool':
            entry |= {'tool_call_id': message.tool_call_id, 'name': message.name}
        if message.type == 'ai' and message.tool_calls:
            entry['tool_calls'] = [
                {'id': call['id'], 'type': 'function', 'function': {
                    'name': call['name'], 'arguments': json.dumps(call['args'])}}
                for call in message.tool_calls]
        converted.append(entry)
    return converted


def to_langchain(messages):
    """The benchmark's evidence format as LangChain messages."""
    converted = []
    for message in messages:
        if message['role'] == 'system':
            converted.append(SystemMessage(message['content']))
        elif message['role'] == 'user':
            converted.append(HumanMessage(message['content']))
        elif message['role'] == 'tool':
            converted.append(ToolMessage(content=message['content'], name=message['name'],
                                         tool_call_id=message['tool_call_id']))
        else:
            converted.append(AIMessage(content=message['content'], tool_calls=[
                {'id': call['id'], 'name': call['function']['name'],
                 'args': json.loads(call['function']['arguments'])}
                for call in message.get('tool_calls', [])]))
    return converted


class ScriptedChatModel(BaseChatModel):
    """The reference scripted policies behind LangChain's chat model interface.

    Recording requests here, rather than through a callback handler, keeps the
    comparison against the reference loop's `requests` exact.
    """

    provider: Any
    model_name: str
    requests: list = Field(default_factory=list)

    @property
    def _llm_type(self):
        return 'scripted'

    def bind_tools(self, tools, **kwargs):
        return self.bind(tools=[convert_to_openai_tool(tool) for tool in tools], **kwargs)

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        raise NotImplementedError('The benchmark runs episodes asynchronously')

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        baseline = to_baseline(messages)
        tools = kwargs.get('tools', [])
        self.requests.append({'model': self.model_name, 'messages': baseline, 'tools': tools})
        turn = sum(message['role'] == 'assistant' for message in baseline)
        response = await self.provider.complete(self.model_name, baseline, tools)
        message = AIMessage(content=response.get('content') or '', tool_calls=[
            {'id': f'call_{turn}_{i}', 'name': call['name'], 'args': call.get('arguments') or {}}
            for i, call in enumerate(response.get('tool_calls', []))])
        return ChatResult(generations=[ChatGeneration(message=message)])


async def run_episode(provider, model, client, collector, step_budget):
    """The Phase 1A graph, with LangChain supplying the model and the MCP tools."""
    tools = await load_mcp_tools(client)
    for tool in tools:
        # The adapter raises ToolException for an isError result, which LangGraph's
        # default handling re-raises and would abort the episode; the benchmark feeds
        # tool failures back to the model as tool content instead.
        tool.handle_tool_error = True
    by_name = {tool.name: tool for tool in tools}
    schemas = [convert_to_openai_tool(tool) for tool in tools]
    chat = ScriptedChatModel(provider=provider, model_name=model).bind_tools(tools)

    async def complete(messages):
        response = await chat.ainvoke(to_langchain(messages))
        return {'content': response.content,
                'tool_calls': [{'name': call['name'], 'arguments': call['args']}
                               for call in response.tool_calls]}

    async def dispatch(name, arguments):
        if name not in by_name:
            # LangChain resolves tools from a client-side registry; the baseline
            # forwards an unknown name to the server and returns its error.
            return f'Unknown tool: {name}', {'unknown_tool': name}, True
        message = await by_name[name].ainvoke(
            {'type': 'tool_call', 'id': f'dispatch_{name}', 'name': name, 'args': arguments})
        return text_of(message), message.model_dump(mode='json'), message.status == 'error'

    return await graph.run(graph.Context(
        model=model, complete=complete, dispatch=dispatch, tools=schemas,
        collector=collector, step_budget=step_budget))


def agent_trajectory(requests, messages):
    """Reconstruct the benchmark's turn records from an agent transcript.

    Only the first `len(requests)` AI messages come from the model: exit middleware
    appends one of its own, which is not a model turn.
    """
    results = {message.tool_call_id: message for message in messages if message.type == 'tool'}
    trajectory = []
    for step, message in enumerate([m for m in messages if m.type == 'ai'][:len(requests)]):
        calls = []
        for call in message.tool_calls:
            result = results.get(call['id'])
            calls.append({'id': call['id'], 'name': call['name'], 'arguments': call['args'],
                          'result': result.model_dump(mode='json') if result else None})
        trajectory.append({'step': step, 'content': text_of(message), 'calls': calls})
    return trajectory


async def run_agent_episode(provider, model, client, collector, step_budget):
    """LangChain's prebuilt agent over the same scenario, for behavioural comparison.

    Two middlewares restore baseline semantics the prebuilt agent does not have:
    a model-turn budget, and tool errors returned to the model rather than raised.
    Its parallel tool dispatch has no such knob and remains a recorded difference.
    """
    tools = await load_mcp_tools(client)
    chat = ScriptedChatModel(provider=provider, model_name=model)
    agent = create_agent(
        model=chat, tools=tools, system_prompt=SYSTEM,
        middleware=[ModelCallLimitMiddleware(run_limit=step_budget, exit_behavior='end'),
                    ToolErrorMiddleware(on_error=lambda exc, request: f'ERROR: {exc}')])
    errors, messages, termination = [], [], None
    try:
        messages = (await agent.ainvoke({'messages': [HumanMessage(TASK)]}))['messages']
    except Exception as exc:  # pylint: disable=broad-exception-caught
        # Preserve provider failures in trial evidence before stopping the episode.
        errors.append({'kind': 'provider_error', 'step': len(chat.requests) - 1,
                       'detail': str(exc)})
        termination = 'provider_error'
    trajectory = agent_trajectory(chat.requests, messages)
    if termination is None:
        termination = 'model_stop' if trajectory and not trajectory[-1]['calls'] else 'budget_exhausted'
    for turn in trajectory:
        for call in turn['calls']:
            result = call['result'] or {}
            if result.get('status') == 'error':
                errors.append({'kind': 'tool_error', 'step': turn['step'], 'call_id': call['id'],
                               'detail': result['content'].removeprefix('ERROR: ')})
    steps_to_propose = next((turn['step'] for turn in trajectory
                             if any(call['name'] in ATTACK_TOOLS for call in turn['calls'])), None)
    metrics, artifacts = score(collector, chat.requests, trajectory, termination,
                               errors, steps_to_propose)
    return metrics, {'tools': [convert_to_openai_tool(tool) for tool in tools],
                     'requests': chat.requests, 'messages': to_baseline(messages),
                     'trajectory': trajectory, 'collector': artifacts}
