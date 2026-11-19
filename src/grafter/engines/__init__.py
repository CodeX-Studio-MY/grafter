from grafter.engines.base import AgentEngine, RunResult, Task

__all__ = ["AgentEngine", "RunResult", "Task", "get_engine"]


def get_engine(name: str) -> AgentEngine:
    if name == "openhands":
        from grafter.engines.openhands import OpenHandsEngine

        return OpenHandsEngine()
    raise ValueError(f"unknown engine: {name}")
