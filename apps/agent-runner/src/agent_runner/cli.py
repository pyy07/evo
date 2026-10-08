from __future__ import annotations

import argparse
import json
import sys

from agent_runner.loop import run_once
from agent_runner.settings import RunnerSettings


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Evo agent runner")
    parser.add_argument("command", choices=["once"], help="Run one agent cycle")
    parser.add_argument("--api-base", default=None)
    args = parser.parse_args(argv)
    settings = RunnerSettings()
    if args.api_base:
        settings.api_base = args.api_base
    if args.command == "once":
        result = run_once(settings)
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        sys.exit(0)


if __name__ == "__main__":
    main()