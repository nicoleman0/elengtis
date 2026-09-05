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
from elengtis.budget import BudgetExceeded
from elengtis.reference import SYSTEM, TASK

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


def usage_record(message):
    """Keep provider usage/cost metadata in a small JSON-safe record."""
    usage = message.usage_metadata or {}
    metadata = message.response_metadata or {}
    record = {key: usage[key] for key in ('input_tokens', 'output_tokens', 'total_tokens')
              if usage.get(key) is not None}
    if metadata.get('cost') is not None or metadata.get('cost_usd') is not None:
        record['cost_usd'] = metadata.get('cost', metadata.get('cost_usd'))
    if metadata.get('cost_details') is not None:
        record['cost_details'] = metadata['cost_details']
    for key in ('provider', 'model_provider', 'system_fingerprint', 'native_finish_reason'):
        if metadata.get(key) is not None:
            record[key] = metadata[key]
    return record


class LiveProvider:
    """A LangChain chat model behind the reference loop's provider interface.

    Every engine drives the same live model through this, so a difference between
    engines stays attributable to orchestration rather than to a second provider.
    """

    def __init__(self, chat, budget=None):
        self.chat = chat
        self.budget = budget

    async def complete(self, model, messages, tools):
        reservation = self.budget.reserve(
            model, messages, tools, self.chat.max_tokens or self.chat.max_completion_tokens or 0
        ) if self.budget else None
        try:
            response = await self.chat.ainvoke(to_langchain(messages), tools=tools)
        except Exception:
            if self.budget:
                self.budget.settle(reservation, None)
            raise
        usage = usage_record(response)
        if self.budget:
            usage['_rates'] = self.budget.pricing[model]
            usage['budget_charge_usd'] = self.budget.settle(reservation, usage)
            usage.pop('_rates', None)
        return {'content': text_of(response),
                'tool_calls': [{'name': call['name'], 'arguments': call['args']}
                               for call in response.tool_calls], 'usage': usage}


class RecordingChatModel(BaseChatModel):
    """A provider behind LangChain's chat model interface, recording its requests.

    Recording here, rather than through a callback handler, keeps the comparison
    against the reference loop's `requests` exact.
    """

    provider: Any
    model_name: str
    requests: list = Field(default_factory=list)
    usages: list = Field(default_factory=list)

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
        usage = response.get('usage')
        self.usages.append(usage)
        usage_metadata = {key: usage[key] for key in ('input_tokens', 'output_tokens', 'total_tokens')
                          if usage and usage.get(key) is not None} or None
        response_metadata = {key: usage[key] for key in ('cost_details', 'provider', 'model_provider',
                                                          'system_fingerprint', 'native_finish_reason')
                             if usage and usage.get(key) is not None}
        if usage and usage.get('cost_usd') is not None:
            response_metadata['cost'] = usage['cost_usd']
        message = AIMessage(content=response.get('content') or '', tool_calls=[
            {'id': f'call_{turn}_{i}', 'name': call['name'], 'args': call.get('arguments') or {}}
            for i, call in enumerate(response.get('tool_calls', []))],
            usage_metadata=usage_metadata, response_metadata=response_metadata)
        return ChatResult(generations=[ChatGeneration(message=message)])


async def run_episode(provider, model, client, step_budget, system_prompt=SYSTEM,
                      user_prompt=TASK, allowed_tools='all'):
    """The Phase 1A graph, with LangChain supplying the model and the MCP tools."""
    tools = await load_mcp_tools(client)
    if allowed_tools != 'all':
        by_name = {tool.name: tool for tool in tools}
        missing = set(allowed_tools) - by_name.keys()
        if missing:
            raise ValueError(f'Target is missing exercise tools {sorted(missing)}')
        tools = [by_name[name] for name in allowed_tools]
    for tool in tools:
        # The adapter raises ToolException for an isError result, which LangGraph's
        # default handling re-raises and would abort the episode; the benchmark feeds
        # tool failures back to the model as tool content instead.
        tool.handle_tool_error = True
    by_name = {tool.name: tool for tool in tools}
    schemas = [convert_to_openai_tool(tool) for tool in tools]
    chat = RecordingChatModel(provider=provider, model_name=model).bind_tools(tools)

    async def complete(messages):
        response = await chat.ainvoke(to_langchain(messages))
        return {'content': response.content,
                'tool_calls': [{'name': call['name'], 'arguments': call['args']}
                               for call in response.tool_calls], 'usage': usage_record(response)}

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
        step_budget=step_budget, system_prompt=system_prompt, user_prompt=user_prompt))


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


async def run_agent_episode(provider, model, client, step_budget, system_prompt=SYSTEM,
                            user_prompt=TASK, allowed_tools='all'):
    """LangChain's prebuilt agent over the same scenario, for behavioural comparison.

    Two middlewares restore baseline semantics the prebuilt agent does not have:
    a model-turn budget, and tool errors returned to the model rather than raised.
    Its parallel tool dispatch has no such knob and remains a recorded difference.
    """
    tools = await load_mcp_tools(client)
    if allowed_tools != 'all':
        by_name = {tool.name: tool for tool in tools}
        missing = set(allowed_tools) - by_name.keys()
        if missing:
            raise ValueError(f'Target is missing exercise tools {sorted(missing)}')
        tools = [by_name[name] for name in allowed_tools]
    chat = RecordingChatModel(provider=provider, model_name=model)
    agent = create_agent(
        model=chat, tools=tools, system_prompt=system_prompt,
        middleware=[ModelCallLimitMiddleware(run_limit=step_budget, exit_behavior='end'),
                    ToolErrorMiddleware(on_error=lambda exc, request: f'ERROR: {exc}')])
    errors, messages, termination = [], [], None
    try:
        messages = (await agent.ainvoke({'messages': [HumanMessage(user_prompt)]}))['messages']
    except Exception as exc:  # pylint: disable=broad-exception-caught
        if isinstance(exc, BudgetExceeded):
            raise
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
    metrics = {'model_turns': len(chat.requests),
               'tool_calls': sum(len(turn['calls']) for turn in trajectory),
               'termination': termination, 'errors': errors,
               'usage': [usage for usage in chat.usages if usage]}
    return metrics, {'tools': [convert_to_openai_tool(tool) for tool in tools],
                     'requests': chat.requests, 'messages': to_baseline(messages),
                     'trajectory': trajectory, 'usage': metrics['usage']}
