"""Small, conservative spend guard for capped live pilots."""
import json


class BudgetExceeded(RuntimeError):
    """Raised before a request that could exceed the configured spend cap."""


class PilotBudget:
    def __init__(self, limit, pricing, committed=0.0):
        self.limit = float(limit)
        self.pricing = pricing
        self.committed = float(committed)
        self.reserved = 0.0
        self.unknown_costs = 0

    def estimate(self, model, messages, tools, max_tokens):
        rates = self.pricing[model]
        payload = json.dumps({'messages': messages, 'tools': tools}, sort_keys=True,
                             separators=(',', ':'))
        # One character can be one token; this is deliberately conservative.
        return (len(payload.encode()) * rates['input_per_million'] +
                max_tokens * rates['output_per_million']) / 1_000_000

    def reserve(self, model, messages, tools, max_tokens):
        estimate = self.estimate(model, messages, tools, max_tokens)
        if self.committed + self.reserved + estimate > self.limit + 1e-12:
            raise BudgetExceeded(
                f'budget ${self.limit:.6f} would be exceeded by the next request '
                f'(reserved ${self.committed + self.reserved + estimate:.6f})')
        self.reserved += estimate
        return estimate

    def settle(self, reservation, usage):
        self.reserved -= reservation
        usage = usage or {}
        cost = usage.get('cost_usd')
        if cost is None:
            input_tokens, output_tokens = usage.get('input_tokens'), usage.get('output_tokens')
            rates = usage.get('_rates')
            if rates and input_tokens is not None and output_tokens is not None:
                cost = (input_tokens * rates['input_per_million'] +
                        output_tokens * rates['output_per_million']) / 1_000_000
            else:
                cost = reservation
                self.unknown_costs += 1
        cost = max(0.0, float(cost))
        self.committed += cost
        return cost

    def snapshot(self):
        return {'committed_usd': self.committed, 'reserved_usd': self.reserved,
                'unknown_costs': self.unknown_costs, 'limit_usd': self.limit}
