# Spike 1C2: OpenHands SDK + DockerWorkspace

**Date:** 2026-11-20. **Script:** [`spikes/openhands_hello.py`](../../spikes/openhands_hello.py). **Feeds:** 1C3 (`OpenHandsEngine`), 1D2b (Docker runner).

**Question:** can the OpenHands Software Agent SDK (D4) drive an agent that edits files inside a Docker container, while the model is reached only through our gateway with a capped per-run key? And is everything it installs licensed in a way we can use?

## Version and image

| What | Pinned | Why |
|---|---|---|
| `openhands-sdk`, `-tools`, `-workspace`, `-agent-server` | `==1.60.1`, all four together (`openhands` extra) | The newest release at least 14 days old with a published server image. The docs say mismatched SDK versions fail at import. |
| Agent-server image | `ghcr.io/openhands/agent-server@sha256:b43ee580…6d55` (the `1.60-python` index) | No `1.60.2-*` image exists. `1.60-python` was built 2026-11-05 16:57Z, right after the 1.60.1 release, so client and server match. |

- The image is large: 3.4 GB, Python 3.13, a non-root user `openhands` (uid 10001).
- That user has **passwordless sudo** (group 27), so inside the container the agent is effectively root. The isolation comes from the container boundary, not from the user account.

## Licences

- The upstream `LICENSE` at tag `v1.60.1` is MIT, and the repo has no `enterprise/` directory. The wheels carry no licence metadata.
- `pip-licenses` across the roughly 200 packages in the tree found everything permissive, with these exceptions, all installed unmodified and never copied:
  - `func_timeout`: LGPL, pulled in by `openhands-tools`;
  - `certifi`, `orjson`, `tqdm`: contain MPL-2.0 parts.
- `litellm` is the open-source package only: no `litellm-enterprise`, and no `enterprise/` folder.
- Recorded in [THIRD_PARTY.md](../../THIRD_PARTY.md).

## API used

```python
llm = LLM(
    usage_id="coder",
    model="litellm_proxy/claude-sonnet-5-5",
    base_url="http://litellm:4000",
    api_key=SecretStr(run_key),
)
with DockerWorkspace(server_image=IMAGE, network="grafter-gw") as ws:
    ws.file_upload(local_tgz, "/tmp/repo.tgz")  # one file at a time
    ws.execute_command("tar -xzf /tmp/repo.tgz -C /workspace/project", timeout=120)
    conv = Conversation(
        agent=get_default_agent(llm=llm, cli_mode=True),
        workspace=ws,
        max_iteration_per_run=40,
        visualizer=None,
    )
    conv.send_message(prompt)
    conv.run()
    conv.close()
    m = conv.conversation_stats.get_combined_metrics()  # accumulated_cost, accumulated_token_usage
    ws.file_download("/workspace/project/tests/test_grading.py", local_path)
```

- **`DockerWorkspace`** runs `docker run -d --rm` on the image, publishes the agent-server's port 8000 on `127.0.0.1:<free port>`, and waits for `/health`.
  - Options it takes: `network=`, `volumes=`, `forward_env=` and `platform=`.
  - Leaving the `with` block stops the container, and `--rm` removes it. No container was left behind.
- **`Conversation`** with a remote workspace becomes a `RemoteConversation`. The agent loop, and with it every LLM call, runs **inside the container**. That is why the container must get only a scoped run key, never the Anthropic or master key.
- **Moving files in and out:** `file_upload` and `file_download` handle one file each. A tarball is the easy way to move a whole repo. A bind mount (`volumes=`) would also work, but it gives the container write access to a host directory. 1C3 should prefer upload/download, or a throwaway copy.
- **Cost and token counts:** `get_combined_metrics()` returns `accumulated_cost` and `accumulated_token_usage` (`prompt_tokens`, `completion_tokens`, cache read/write tokens, reasoning tokens).
  - These are LiteLLM's estimates on the client side.
  - The gateway's own spend for the run key (`/key/info`) is the authoritative figure, because D16's cap is enforced there.
- **Spend limits:** `Conversation(max_budget_per_run=…)` and `max_iteration_per_run` are SDK-side limits on top of the gateway cap.

## Checked without the model

Checked with `network="grafter-gw"`, Docker Desktop 29.9.3 on Windows:

- The container starts in about 5 s once the image is pulled. Upload, tar extraction and commands all work.
- `python` is 3.13.15 and `git` is present.
- **No secrets are in the container's environment.** Only the image's own variables are there: `PATH`, `LANG`, `OH_*`, Chromium paths and similar.
- It reaches the gateway at `http://litellm:4000`.
- **It also reaches the internet**, because `grafter-gw` is a normal bridge network.
- Making the network `internal: true` blocks the internet, but it also blocks the host from reaching the published port. Tested directly: host to container failed, and container to the internet failed. So the SDK cannot talk to the agent server on an internal-only network.

## Model run

Run 2026-11-20 against the local gateway (`grafter gateway check` printed ok first). Model `claude-sonnet-5-5` (`models.coder`). The run key allowed only that model, was capped at $0.50 and valid for 1 h, and was deleted afterwards.

| Check | Result |
|---|---|
| Container started on `grafter-gw`, repo uploaded | yes, ready in about 10 s |
| Secrets in the container env | none (only image variables) |
| Baseline suite inside the container | 4 passed |
| Agent edited a file inside the container | **yes**: `tests/test_grading.py` only (`git status`: `M tests/test_grading.py`) |
| Suite after the agent | 5 passed |
| Changed file downloaded to the host | yes; differs from the host copy, and the host copy was never touched |
| Tokens (SDK) | 58,951 prompt, 1,494 completion |
| Cost | SDK estimate $0.0537; **gateway spend $0.0481** |
| Wall time | 40 s total, 16 s agent run |
| Leftovers | no container; master and Anthropic keys absent from the log |

The agent added one test function, `test_grade_boundaries_and_validation`. It checks the grade boundaries, the `min_brix` override, the `REJECT` price, and 5 `ValueError` cases. The test is correct and passes. It is also one large test with 17 asserts. 1C3's prompt should ask for small, focused tests, one behaviour each, so reviewers can judge them. The guard and gates don't check for that.

## Recommendations for 1C3 and 1D2b

1. **Restricting network access is still open, and must be solved before real runs (1D2b).**
   - Candidate: put the agent-server on an `internal` network together with LiteLLM, and add a tiny relay container on both that network and a normal one. The relay publishes only the agent-server port to `127.0.0.1`. The agent then reaches only the gateway, and the host reaches only the agent server.
   - Alternative: an egress proxy that only allows the gateway.
   - Either way, build it in 1D2b and test it there.
2. **Image choice:** use the OpenHands agent-server image, pinned by digest, as the engine's sandbox.
   - Our `sandboxes/python` image stays the **gates** runner (1D2b): tests and coverage run in a fresh container that the agent never touched.
   - `DockerDevWorkspace` can build the agent server from a different base image, but it needs the SDK source tree. That isn't worth it for phase 1.
3. **Pin the SDK and the image together.** An upgrade means bumping both and re-running this spike. Renovate (1E1) must treat them as one group.
4. **Keys:** one run key per task, limited to `[models.planner, models.coder]` and capped at that task's share of the nightly budget. Delete it in `finally`. Report the gateway spend, not the SDK's estimate.
5. **Model names:** `litellm_proxy/<name>` with `base_url` pointing at the gateway. `<name>` is the `model_name` in `deploy/litellm/config.yaml`.
6. **On Windows:** nothing special was needed with Docker Desktop on Linux containers. The SDK prints a banner; set `OPENHANDS_SUPPRESS_BANNER=1` to turn it off.
