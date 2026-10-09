from __future__ import annotations

import argparse
import json
import sys

from agent_runner.loop import run_once, run_serve
from agent_runner.settings import RunnerSettings


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Evo agent runner")
    parser.add_argument(
        "command",
        choices=["once", "serve"],
        help="once: 强制单轮；serve: 按交易时段常驻调度",
    )
    parser.add_argument("--api-base", default=None)
    parser.add_argument(
        "--mode",
        choices=["intraday", "postclose"],
        default=None,
        help="仅 once：强制盘中或盘后轮（仍受 API 时段硬约束）",
    )
    args = parser.parse_args(argv)
    settings = RunnerSettings()
    if args.api_base:
        settings.api_base = args.api_base
    if args.command == "serve":
        run_serve(settings)
        return
    result = run_once(settings, mode=args.mode)
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    sys.exit(0)


if __name__ == "__main__":
    main()
