"""Entry point for the bundled server, kept separate from the CLI installer."""

from __future__ import annotations

import json
import multiprocessing
import sys


def main() -> None:
    multiprocessing.freeze_support()
    if sys.argv[1:] == ["--self-test"]:
        from pepsicode.agents.loader import AgentLoader

        agents = AgentLoader(".")
        names = [name for name in ("explore", "plan", "general", "verification") if agents.get(name)]
        if len(names) != 4:
            raise RuntimeError("The bundled agent definitions are incomplete")
        from pepsicode.prompts import load_harness_instructions

        load_harness_instructions()
        from pepsicode.build_info import build_identity

        print(json.dumps({"frozen": bool(getattr(sys, "frozen", False)), "agents": names, "build": build_identity()}))
    else:
        from pepsicode.server.app import main as server_main

        server_main()


if __name__ == "__main__":
    main()
