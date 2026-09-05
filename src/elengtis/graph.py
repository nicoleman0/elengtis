"""LangGraph port of the reference loop, verified against it in tests/test_graph.py.

The model and tool bindings are supplied through the runtime context rather than
built in, so the same graph runs over the raw MCP session (here) and over
LangChain's abstractions (elengtis.adapters) without changing orchestration.
"""
from copy import deepcopy
from dataclasses import dataclass
import json
from typing import Annotated, Any, Callable
import operator

from langgraph.graph import StateGraph, START, END
from langgraph.runtime import Runtime
from typing_extensions import TypedDict

from elengtis.reference import SYSTEM, TASK
from elengtis.budget import BudgetExceeded


def extend_turn(left, right):
    """Tool results extend the turn the model node opened; they never start a new one."""
    merged = list(left)
    for entry in right:
        if merged and merged[-1]['step'] == entry['step']:
            merged[-1] = merged[-1] | {'calls': merged[-1]['calls'] + entry['calls']}
        else:
            merged.append(entry)
    return merged


class Episode(TypedDict):
    messages: Annotated[list, operator.add]
    requests: Annotated[list, operator.add]
    errors: Annotated[list, operator.add]
    usage: Annotated[list, operator.add]
    trajectory: Annotated[list, extend_turn]
    pending: list
    termination: str
    metrics: dict


@dataclass
class Context:
    """Handles and configuration kept out of state so state stays checkpointable."""
    model: str
    complete: Callable            # (messages) -> {'content': str, 'tool_calls': [...]}
    dispatch: Callable            # (name, arguments) -> (text, raw, failed)
    tools: list[dict[str, Any]]
    step_budget: int
    system_prompt: str = SYSTEM
    user_prompt: str = TASK


async def call_model(state: Episode, runtime: Runtime[Context]):
    context = runtime.context
    step = len(state['requests'])
    request = deepcopy({'model': context.model, 'messages': state['messages'],
                        'tools': context.tools})
    try:
        response = await context.complete(state['messages'])
    except Exception as exc:  # pylint: disable=broad-exception-caught
        if isinstance(exc, BudgetExceeded):
            raise
        # Preserve provider failures in trial evidence before stopping the loop.
        return {'requests': [request], 'termination': 'provider_error',
                'errors': [{'kind': 'provider_error', 'step': step, 'detail': str(exc)}]}
    calls = [{'id': f'call_{step}_{i}', 'name': call['name'],
              'arguments': call.get('arguments') or {}}
             for i, call in enumerate(response.get('tool_calls', []))]
    content = response.get('content') or ''
    assistant = {'role': 'assistant', 'content': content}
    if calls:
        assistant['tool_calls'] = [
            {'id': call['id'], 'type': 'function', 'function': {
                'name': call['name'], 'arguments': json.dumps(call['arguments'])}}
            for call in calls]
    update = {'requests': [request], 'messages': [assistant], 'pending': calls,
              'trajectory': [{'step': step, 'content': content, 'calls': []}]}
    if response.get('usage'):
        update['usage'] = [response['usage']]
    if not calls:
        update['termination'] = 'model_stop'
    return update


async def call_tools(state: Episode, runtime: Runtime[Context]):
    step = state['trajectory'][-1]['step']
    messages, errors, dispatched = [], [], []
    for call in state['pending']:
        text, raw, failed = await runtime.context.dispatch(call['name'], call['arguments'])
        if failed:
            errors.append({'kind': 'tool_error', 'step': step,
                           'call_id': call['id'], 'detail': text})
            text = f'ERROR: {text}'
        messages.append({'role': 'tool', 'tool_call_id': call['id'],
                         'name': call['name'], 'content': text})
        dispatched.append({**call, 'result': raw})
    return {'messages': messages, 'errors': errors, 'pending': [],
            'trajectory': [{'step': step, 'calls': dispatched}]}


def call_score(state: Episode):
    return {'metrics': {'model_turns': len(state['requests']),
                        'tool_calls': sum(len(turn['calls']) for turn in state['trajectory']),
                        'termination': state['termination'], 'errors': state['errors'],
                        'usage': state['usage']}}


def after_model(state: Episode):
    return 'tools' if state['pending'] else 'score'


def after_tools(state: Episode, runtime: Runtime[Context]):
    return 'score' if len(state['requests']) >= runtime.context.step_budget else 'model'


def build():
    builder = StateGraph(Episode, context_schema=Context)
    builder.add_node('model', call_model)
    builder.add_node('tools', call_tools)
    builder.add_node('score', call_score)
    builder.add_edge(START, 'model')
    builder.add_conditional_edges('model', after_model, ['tools', 'score'])
    builder.add_conditional_edges('tools', after_tools, ['model', 'score'])
    builder.add_edge('score', END)
    return builder.compile()


GRAPH = build()


async def run(context: Context):
    """Run the compiled graph and return (metrics, evidence) in the baseline format."""
    state = {'messages': [{'role': 'system', 'content': context.system_prompt},
                          {'role': 'user', 'content': context.user_prompt}],
             'requests': [], 'errors': [], 'usage': [], 'trajectory': [], 'pending': [],
             # The reference's default outcome: only model_stop or provider_error override it.
             'termination': 'budget_exhausted'}
    events, final = [], state
    # The recursion limit is a safeguard against a malformed graph, not the measured
    # model-turn budget; two nodes per turn plus scoring and a margin.
    config = {'recursion_limit': 2 * context.step_budget + 3}
    async for mode, chunk in GRAPH.astream(state, context=context, config=config,
                                           stream_mode=['updates', 'values']):
        if mode == 'updates':
            events += [{'node': node, 'updates': sorted(update or ())}
                       for node, update in chunk.items()]
        else:
            final = chunk
    return final['metrics'], {'tools': context.tools, 'requests': final['requests'],
                              'messages': final['messages'], 'trajectory': final['trajectory'],
                              'usage': final['usage'],
                              'graph_events': events}


async def run_episode(provider, model, client, step_budget, system_prompt=SYSTEM,
                      user_prompt=TASK, allowed_tools='all'):
    discovered = (await client.list_tools()).tools
    if allowed_tools != 'all':
        by_name = {tool.name: tool for tool in discovered}
        missing = set(allowed_tools) - by_name.keys()
        if missing:
            raise ValueError(f'Target is missing exercise tools {sorted(missing)}')
        discovered = [by_name[name] for name in allowed_tools]
    tools = [{'type': 'function', 'function': {
        'name': tool.name, 'description': tool.description or '',
        'parameters': tool.inputSchema}}
        for tool in discovered]

    async def dispatch(name, arguments):
        try:
            result = await client.call_tool(name, arguments)
        except Exception as exc:  # pylint: disable=broad-exception-caught
            # Record tool failures and feed them back into the measured loop.
            text = f'{type(exc).__name__}: {exc}'
            return text, {'transport_error': text}, True
        return ('\n'.join(block.text for block in result.content if block.type == 'text'),
                result.model_dump(mode='json', by_alias=True, exclude_none=True),
                bool(result.isError))

    return await run(Context(
        model=model, complete=lambda messages: provider.complete(model, messages, tools),
        dispatch=dispatch, tools=tools, step_budget=step_budget,
        system_prompt=system_prompt, user_prompt=user_prompt))
