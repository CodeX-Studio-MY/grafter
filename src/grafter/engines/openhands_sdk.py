"""Real `Sandbox` on the OpenHands SDK (`pip install grafter[openhands]`).

Kept apart from `grafter.engines.openhands` so the core imports nothing from OpenHands.
Exercised by `spikes/engine_try.py` against Docker and the gateway, not by the offline
suite (excluded from coverage and mypy: the SDK is an optional dependency).
"""

from __future__ import annotations

import os
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from grafter.config import OpenHands
from grafter.engines.openhands import (
    AgentLimits,
    AgentOutcome,
    ExecResult,
    LlmSpec,
    Sandbox,
    SandboxFactory,
)

POLL_S = 2.0

os.environ.setdefault("OPENHANDS_SUPPRESS_BANNER", "1")


class SdkSandbox:
    def __init__(self, workspace: Any) -> None:
        self.ws = workspace

    def upload(self, local: Path, remote: str) -> None:
        res = self.ws.file_upload(local, remote)
        if not res.success:
            raise RuntimeError(f"upload to {remote} failed")

    def execute(self, command: str, timeout: float) -> ExecResult:
        res = self.ws.execute_command(command, timeout=timeout)
        return ExecResult(int(res.exit_code), (res.stdout or "") + (res.stderr or ""))

    def download(self, remote: str, local: Path) -> None:
        res = self.ws.file_download(remote, local)
        if not res.success:
            raise RuntimeError(f"download of {remote} failed")

    def run_agent(self, prompt: str, llm: LlmSpec, limits: AgentLimits) -> AgentOutcome:
        from openhands.sdk import LLM, Conversation
        from openhands.sdk.conversation.state import ConversationExecutionStatus as St
        from openhands.tools.preset.default import get_default_agent
        from pydantic import SecretStr

        model = LLM(
            usage_id="coder",
            model=llm.model,
            base_url=llm.base_url,
            api_key=SecretStr(llm.api_key),
            num_retries=2,
        )
        conv = Conversation(
            agent=get_default_agent(llm=model, cli_mode=True),
            workspace=self.ws,
            max_iteration_per_run=limits.max_iterations,
            max_budget_per_run=limits.max_usd,
            visualizer=None,
        )
        outcome = AgentOutcome(status="error")
        try:
            conv.send_message(prompt)
            conv.run(blocking=False)
            deadline = time.monotonic() + limits.timeout_s
            while True:
                time.sleep(POLL_S)
                conv.state.refresh_from_server()
                _metrics_into(conv, outcome)
                status = conv.state.execution_status
                if status == St.FINISHED:
                    outcome.status = "finished"
                    break
                if status in (St.ERROR, St.STUCK, St.PAUSED, St.WAITING_FOR_CONFIRMATION):
                    outcome.status = "error"
                    outcome.detail = f"status {status.value}"
                    break
                if outcome.tokens > limits.max_tokens:
                    conv.pause()
                    outcome.status = "token_cap"
                    break
                if time.monotonic() > deadline:
                    conv.pause()
                    outcome.status = "timeout"
                    break
            _metrics_into(conv, outcome)
        finally:
            conv.close()
        return outcome


def _metrics_into(conv: Any, outcome: AgentOutcome) -> None:
    m = conv.conversation_stats.get_combined_metrics()
    usage = m.accumulated_token_usage
    if usage is not None:
        outcome.prompt_tokens = int(usage.prompt_tokens or 0)
        outcome.completion_tokens = int(usage.completion_tokens or 0)
    outcome.cost_estimate = float(m.accumulated_cost or 0.0)


def sandbox_factory(settings: OpenHands) -> SandboxFactory:
    @contextmanager
    def open_sandbox() -> Iterator[Sandbox]:
        from openhands.workspace import DockerWorkspace

        with DockerWorkspace(
            server_image=settings.image, network=settings.network, detach_logs=False
        ) as ws:
            yield SdkSandbox(ws)

    return open_sandbox
