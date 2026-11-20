"""TODO 1D2c Done-when (part 1): from inside the agent container, the internet is blocked and
the gateway is reachable. Uses the same AgentContainer + RemoteWorkspace as the engine.

    .venv-oh/Scripts/python spikes/egress_check.py      # needs Docker + the gateway up
"""

from __future__ import annotations

import os
import subprocess
import sys

os.environ.setdefault("OPENHANDS_SUPPRESS_BANNER", "1")

from grafter.config import OpenHands  # noqa: E402
from grafter.engines.openhands_sdk import sandbox_factory  # noqa: E402

PROBES = {
    "internet (https://pypi.org)": "curl -sS -m 8 -o /dev/null -w '%{http_code}' https://pypi.org/simple/",
    "internet by IP (1.1.1.1:443)": "curl -sS -m 8 -o /dev/null -w '%{http_code}' https://1.1.1.1/",
    "gateway (http://litellm:4000)": "curl -sS -m 8 http://litellm:4000/health/liveliness",
    "test tools": "python3 -m pytest --version",
    "identity": "id -u; grep CapEff /proc/self/status | awk '{print $2}'; "
    "sudo -n true 2>/dev/null && echo SUDO_OK || echo NO_SUDO",
}


def main() -> int:
    results = {}
    with sandbox_factory(OpenHands())() as box:
        for name, cmd in PROBES.items():
            r = box.execute(cmd, 30)
            results[name] = (r.exit_code, r.stdout.strip().replace("\n", " | ")[-200:])
            print(f"{name}: exit {r.exit_code}: {results[name][1]}")
    left = subprocess.run(
        ["docker", "ps", "-a", "--filter", "name=grafter-", "--format", "{{.Names}}"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    left = [n for n in left if n.startswith(("grafter-agent-", "grafter-relay-"))]
    print("leftover containers:", left or "none")
    blocked = all(results[k][0] != 0 for k in list(PROBES)[:2])
    gateway = results["gateway (http://litellm:4000)"] == (0, '"I\'m alive!"')
    no_sudo = "NO_SUDO" in results["identity"][1]
    tools = results["test tools"][0] == 0
    ok = blocked and gateway and no_sudo and tools and not left
    print(
        f"internet blocked={blocked} gateway reachable={gateway} sudo blocked={no_sudo} "
        f"test tools={tools}"
    )
    print("DONE-WHEN (egress):", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
