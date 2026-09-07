"""Optional execution progress and cancellation hooks for all runners."""
from __future__ import annotations


class Observer:
    def event(self, phase: str, payload: dict | None = None) -> None: pass
    def cancelled(self) -> bool: return False


NO_OBSERVER = Observer()
