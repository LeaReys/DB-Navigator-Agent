from __future__ import annotations

import argparse
import json

from app.graph import build_graph


def main() -> None:
    parser = argparse.ArgumentParser(description="DB Navigator Agent demo")
    parser.add_argument("question", nargs="?", default="Где найти информацию по статусу должника?")
    parser.add_argument("--json", action="store_true", help="Print full state as JSON")
    args = parser.parse_args()

    graph = build_graph()
    result = graph.invoke({"question": args.question, "tool_results": []})

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(result["final_answer"])
        print("\n---")
        print("Intent:", result.get("intent"))
        print("Tool calls:", [event["tool"] for event in result.get("tool_results", [])])


if __name__ == "__main__":
    main()
