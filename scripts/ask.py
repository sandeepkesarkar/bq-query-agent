#!/usr/bin/env python3
"""Ask the agent one question from the command line.

The question is routed and planned by a model (--model stub, the default, is a keyword stub
that needs no cloud access; --model gemini calls Gemini on Vertex AI as set in
config/model.yaml and bills a few small requests to your project), checked by code, and
dry-run against BigQuery. If the estimate is within the cap, the function, parameters
and estimated bytes are shown, and the query runs only if you type y. Every request,
including refused and declined ones, appends one line to audit.jsonl.

Usage, from the repo root:
    uv run python scripts/ask.py "top 5 products by profit in Jeans from 2026-01-01 to 2026-09-30"
    uv run python scripts/ask.py "revenue by quarter" --audit /tmp/audit.jsonl
    uv run python scripts/ask.py "revenue by month for last year" --model gemini

A dry run is free. An approved query is billed to your Google Cloud project (each of the
six functions bills 10 to 40 MiB).
"""
import argparse
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from langgraph.types import Command  # noqa: E402

from agent import GeminiModel, KeywordModel, build_graph  # noqa: E402
from agent.audit import DEFAULT_AUDIT_PATH  # noqa: E402
from functions import load  # noqa: E402


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("question")
    parser.add_argument("--audit", type=Path, default=DEFAULT_AUDIT_PATH,
                        help=f"audit log to append to (default {DEFAULT_AUDIT_PATH.name})")
    parser.add_argument("--model", choices=["stub", "gemini"], default="stub",
                        help="which model routes, plans and summarizes (default stub)")
    args = parser.parse_args()

    catalog = load()
    model = GeminiModel() if args.model == "gemini" else KeywordModel(catalog)
    graph = build_graph(catalog, model, audit_path=args.audit)
    config = {"configurable": {"thread_id": uuid.uuid4().hex}}

    state = graph.invoke({"question": args.question}, config)
    if "__interrupt__" in state:
        ask = state["__interrupt__"][0].value
        print(f"Function:  {ask['function']}")
        print(f"Params:    {ask['params'] or '(defaults)'}")
        print(f"Estimate:  {ask['estimated_bytes']:,} bytes (cap {ask['cap_bytes']:,})")
        try:
            answer = input("Run this query? [y/N] ").strip().lower()
        except EOFError:  # no input to read: decline, so the request still ends and is audited
            answer = ""
        state = graph.invoke(Command(resume=answer == "y"), config)

    print()
    print(state.get("message", "(no message)"))
    print(f"\nStatus: {state.get('status')}. Audit id {state.get('audit_id')} in {args.audit}.")


if __name__ == "__main__":
    main()
