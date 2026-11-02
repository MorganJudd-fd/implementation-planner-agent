#!/usr/bin/env python3
"""implementation-planner-agent CLI.

  planner.py prepare  <KEY|brief.json> [--repo <path|folder name>]
  planner.py finalize <KEY> [--no-engine]
  planner.py render   <KEY>
  planner.py probe

prepare binds KEY to one repository; finalize, render and the hooks use that binding.

Exit codes: 0 ok · 1 fixable validation errors · 2 environment/IO · 3 needs the user (which repo?, no brief,
attempts exhausted, risky repo git config).
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from plannerlib import decide, finalize, prepare, render  # noqa: E402
from plannerlib.common import EXIT_ENV, EXIT_INVALID, EXIT_OK, PlannerError, load_policy  # noqa: E402


def main(argv=None):
    ap = argparse.ArgumentParser(prog="planner.py")
    repo = argparse.ArgumentParser(add_help=False)
    repo.add_argument("--repo", help="target repo: absolute path, or a folder name under policy repoSearch.roots")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("prepare", parents=[repo])
    p.add_argument("source", help="ticket key or path to a brief JSON file")
    f = sub.add_parser("finalize", parents=[repo])
    f.add_argument("key")
    f.add_argument("--no-engine", action="store_true")
    sub.add_parser("render", parents=[repo]).add_argument("key")
    sub.add_parser("probe")
    args = ap.parse_args(argv)

    try:
        if args.cmd == "prepare":
            print(json.dumps(prepare.summary(prepare.run(args.source, args.repo)), indent=2))
            return EXIT_OK
        if args.cmd == "finalize":
            result = finalize.run(args.key, args.repo, use_engine=not args.no_engine)
            print(json.dumps(result, indent=2))
            return EXIT_OK if result["ok"] else EXIT_INVALID
        if args.cmd == "render":
            print(render.run(args.key, args.repo))
            return EXIT_OK
        if args.cmd == "probe":
            print(json.dumps(decide.probe(load_policy()), indent=2))
            return EXIT_OK
    except (PlannerError, ValueError) as e:
        # a bare ValueError (corrupt context.json, bad schema) is not fixable by editing the draft
        print(json.dumps({"ok": False, "error": str(e)}, indent=2))
        return getattr(e, "code", EXIT_ENV)


if __name__ == "__main__":
    sys.exit(main())
