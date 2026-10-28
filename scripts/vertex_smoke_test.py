#!/usr/bin/env python3
"""Check that Gemini answers from this machine, before building on it.

Two real (tiny) requests, billed to your account:
  1. a plain message
  2. a structured-output request (the JSON shape the agent's plan will use)
No BigQuery and no agent code is involved. Each request fails if the reply is empty or the
finish reason is not STOP, because Gemini 3.x thinking tokens count against the output cap
and a cap that is too small returns an empty reply with no error (LESSONS.md, lesson 17).

Usage, from the repo root:
    uv run python scripts/vertex_smoke_test.py
    uv run python scripts/vertex_smoke_test.py --model gemini-3.5-flash-lite
    uv run python scripts/vertex_smoke_test.py --region us-central1

Gemini runs on Vertex AI with your Google login (no API key). The project comes from
GOOGLE_CLOUD_PROJECT, or from `gcloud config get-value project`. Each failure prints what
it most likely means. Nothing is retried.
"""
import argparse
import json
import os
import subprocess
import sys

from google import genai
from google.genai import types

DEFAULT_MODEL = "gemini-3.8-flash"
SCHEMA = {
    "type": "object",
    "properties": {
        "function": {"type": "string", "enum": ["revenue_by_period", "top_products"]},
        "n": {"type": "integer"},
    },
    "required": ["function"],
    "additionalProperties": False,
}
PLAIN_PROMPT = "Reply with the single word: ready"
STRUCTURED_PROMPT = "Top 5 products by revenue. Choose the function."


def project_id():
    pid = os.environ.get("GOOGLE_CLOUD_PROJECT")
    if pid:
        return pid
    out = subprocess.run(["gcloud", "config", "get-value", "project"],
                         capture_output=True, text=True)
    return out.stdout.strip() or None


def explain(exc):
    status = getattr(exc, "status_code", None) or getattr(exc, "code", None)
    text = str(exc)
    if status == 403 and "spend cap" in text.lower():
        return "403: the project's spend cap is reached. Raise the cap in Billing, then retry."
    if status == 403 or "PERMISSION_DENIED" in text:
        return ("403: your login lacks permission, or the Vertex AI API is off. Run "
                "`gcloud services enable aiplatform.googleapis.com` and check you hold "
                "roles/aiplatform.user on the project.")
    if status == 404 or "NOT_FOUND" in text:
        return ("404: the model ID is wrong or the model is not served here. Check the model "
                "ID against the Model Garden page, or try --model with another ID.")
    if status == 429 or "RESOURCE_EXHAUSTED" in text:
        return ("429: quota. If the message names a quota with a limit of 0, waiting will not "
                "help: that project has no quota for this model yet.")
    if status == 400:
        return "400: the request was rejected. Read the message above; for check 2 it names the schema problem."
    if "default credentials" in text.lower() or "DefaultCredentialsError" in type(exc).__name__:
        return "No Google login found. Run `gcloud auth application-default login`."
    if isinstance(exc, (TypeError, AttributeError)):
        return "The installed SDK may be too old for this call. Run `uv add -U google-genai`."
    if isinstance(exc, AssertionError):
        return "The model answered, but not usably. Read the message above."
    return "Unexpected error. Paste the whole output."


def usage_text(response):
    u = response.usage_metadata
    reason = response.candidates[0].finish_reason if response.candidates else None
    return (f"finish_reason {getattr(reason, 'name', reason)}, {u.prompt_token_count} in, "
            f"{u.candidates_token_count} out, {u.thoughts_token_count} thinking")


def reply_text(response):
    """The reply text, or an AssertionError saying why there is none."""
    reason = response.candidates[0].finish_reason if response.candidates else None
    text = (response.text or "").strip()
    if reason != types.FinishReason.STOP:
        raise AssertionError(f"finish reason {getattr(reason, 'name', reason)}, not STOP "
                             f"({usage_text(response)}). Raise max_output_tokens")
    if not text:
        raise AssertionError(f"empty reply ({usage_text(response)}). The output cap may be "
                             "spent on thinking; raise max_output_tokens")
    return text


def make_checks(client):
    def plain(model):
        response = client.models.generate_content(
            model=model, contents=PLAIN_PROMPT,
            config=types.GenerateContentConfig(max_output_tokens=256, temperature=0))
        return f"{reply_text(response)!r} ({usage_text(response)})"

    def structured(model):
        response = client.models.generate_content(
            model=model, contents=STRUCTURED_PROMPT,
            config=types.GenerateContentConfig(
                response_mime_type="application/json", response_json_schema=SCHEMA,
                max_output_tokens=1024, temperature=0))
        parsed = json.loads(reply_text(response))
        assert parsed.get("function") in SCHEMA["properties"]["function"]["enum"], parsed
        return f"{parsed} ({usage_text(response)})"

    return plain, structured


def check(label, call, model):
    try:
        print(f"  ok   {label}: {call(model)}")
        return True
    except Exception as exc:  # report and stop: no retries
        print(f"  FAIL {label}: {type(exc).__name__}: {exc}")
        print(f"       likely cause: {explain(exc)}")
        return False


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--model", default=DEFAULT_MODEL, help=f"model ID (default {DEFAULT_MODEL})")
    parser.add_argument("--region", default="global", help="Vertex location (default: global)")
    args = parser.parse_args()

    pid = project_id()
    if not pid:
        sys.exit("No project. Set GOOGLE_CLOUD_PROJECT or run `gcloud config set project <id>`.")
    print(f"Gemini on Vertex AI. Project {pid}, location {args.region}")
    plain, structured = make_checks(genai.Client(vertexai=True, project=pid, location=args.region))

    print(f"\n{args.model}")
    ok = check("1. plain message", plain, args.model)
    if ok:
        ok = check("2. structured output", structured, args.model)

    print("\nAll checks passed." if ok else "\nSome checks failed. Paste this output back.")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
