#!/usr/bin/env python3
"""Check that a model answers from this machine, before building on it.

Two real (tiny) requests per model, billed to your account:
  1. a plain message
  2. a structured-output request (the JSON shape the agent's plan will use)
No BigQuery and no agent code is involved.

Usage, from the repo root:
    uv add google-genai                       # once, for the default provider
    uv run python scripts/vertex_smoke_test.py
    uv run python scripts/vertex_smoke_test.py --model gemini-3.5-flash-lite

Providers (--provider):
  gemini        Gemini on Vertex AI, with your Google login. Default.
  vertex        Claude on Vertex AI (needs: uv add "anthropic[vertex]").
  anthropic     Anthropic's API; set ANTHROPIC_API_KEY, never commit it.
  google-cloud  Claude on Google Cloud, run by Anthropic; set
                ANTHROPIC_GOOGLE_CLOUD_WORKSPACE_ID. Beta client.

The project comes from GOOGLE_CLOUD_PROJECT, or from `gcloud config get-value project`.
Each failure prints what it most likely means. Nothing is retried.
"""
import argparse
import json
import os
import subprocess
import sys

DEFAULT_MODELS = {
    "gemini": ["gemini-3.8-flash"],
    "vertex": ["claude-sonnet-5-5", "claude-haiku-5-5"],
    "anthropic": ["claude-sonnet-5-5", "claude-haiku-5-5"],
    "google-cloud": ["claude-sonnet-5-5", "claude-haiku-5-5"],
}
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
    if status == 401:
        return "401: the API key is missing, wrong or revoked."
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
    if status == 400 and "credit balance" in text.lower():
        return "400: the account has no credit. Add credit in the provider's billing page."
    if status == 400:
        return "400: the request was rejected. Read the message above; for check 2 it names the schema problem."
    if "default credentials" in text.lower() or "DefaultCredentialsError" in type(exc).__name__:
        return "No Google login found. Run `gcloud auth application-default login`."
    if isinstance(exc, (TypeError, AttributeError)):
        return "The installed SDK may be too old for this call. Run `uv add -U` on the SDK package."
    return "Unexpected error. Paste the whole output."


def make_client(provider, region):
    """Return (plain, structured): two functions that take a model ID and return a result string."""
    if provider == "gemini":
        try:
            from google import genai
            from google.genai import types
        except ImportError:
            sys.exit("The google-genai package is missing. Run: uv add google-genai")
        pid = project_id()
        if not pid:
            sys.exit("No project. Set GOOGLE_CLOUD_PROJECT or run `gcloud config set project <id>`.")
        print(f"Provider gemini (Vertex AI). Project {pid}, location {region}")
        client = genai.Client(vertexai=True, project=pid, location=region)

        def plain(model):
            r = client.models.generate_content(
                model=model, contents=PLAIN_PROMPT,
                config=types.GenerateContentConfig(max_output_tokens=50, temperature=0))
            u = r.usage_metadata
            detail = (f"finish_reason {r.candidates[0].finish_reason if r.candidates else None}, "
                      f"{u.prompt_token_count} in, {u.candidates_token_count} out, "
                      f"{getattr(u, 'thoughts_token_count', None)} thinking")
            text = (r.text or "").strip()
            if not text:
                raise AssertionError(f"empty reply ({detail}). The output cap may be spent on thinking; raise max_output_tokens")
            return f"{text!r} ({detail})"

        def structured(model):
            r = client.models.generate_content(
                model=model, contents=STRUCTURED_PROMPT,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json", response_json_schema=SCHEMA,
                    max_output_tokens=200, temperature=0))
            parsed = json.loads(r.text)
            assert parsed["function"] in SCHEMA["properties"]["function"]["enum"], parsed
            u = r.usage_metadata
            return (f"{parsed} ({u.prompt_token_count} in, {u.candidates_token_count} out, "
                    f"{getattr(u, 'thoughts_token_count', None)} thinking)")

        return plain, structured

    # The three Claude providers share one request shape.
    if provider == "anthropic":
        if not os.environ.get("ANTHROPIC_API_KEY"):
            sys.exit("Set ANTHROPIC_API_KEY. Do not put it in a file in the repo.")
        from anthropic import Anthropic
        print("Provider anthropic (Anthropic API)")
        client = Anthropic(max_retries=0, timeout=60)
    elif provider == "vertex":
        try:
            from anthropic import AnthropicVertex
        except ImportError:
            sys.exit('Run: uv add "anthropic[vertex]"')
        pid = project_id()
        print(f"Provider vertex (Claude on Vertex AI). Project {pid}, region {region}")
        client = AnthropicVertex(project_id=pid, region=region, max_retries=0, timeout=60)
    else:
        try:
            from anthropic import AnthropicGoogleCloud
        except ImportError:
            sys.exit('Run: uv add "anthropic[google_cloud]"')
        workspace = os.environ.get("ANTHROPIC_GOOGLE_CLOUD_WORKSPACE_ID")
        if not workspace:
            sys.exit("Set ANTHROPIC_GOOGLE_CLOUD_WORKSPACE_ID to the wrkspc_... id from the Claude Console.")
        pid = project_id()
        print(f"Provider google-cloud (Claude, Anthropic operated). Project {pid}, workspace {workspace}")
        client = AnthropicGoogleCloud(project=pid, workspace_id=workspace, max_retries=0, timeout=60)

    def plain(model):
        r = client.messages.create(model=model, max_tokens=50,
                                   messages=[{"role": "user", "content": PLAIN_PROMPT}])
        text = next(b.text for b in r.content if b.type == "text").strip()
        return f"{text!r} (stop_reason {r.stop_reason}, {r.usage.input_tokens} in, {r.usage.output_tokens} out)"

    def structured(model):
        r = client.messages.create(
            model=model, max_tokens=100,
            messages=[{"role": "user", "content": STRUCTURED_PROMPT}],
            output_config={"format": {"type": "json_schema", "schema": SCHEMA}})
        parsed = json.loads(next(b.text for b in r.content if b.type == "text"))
        assert parsed["function"] in SCHEMA["properties"]["function"]["enum"], parsed
        return f"{parsed} (stop_reason {r.stop_reason})"

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
    parser.add_argument("--provider", choices=list(DEFAULT_MODELS), default="gemini")
    parser.add_argument("--model", help="test one model instead of the provider's defaults")
    parser.add_argument("--region", default="global", help="Vertex location (default: global)")
    args = parser.parse_args()

    plain, structured = make_client(args.provider, args.region)
    all_ok = True
    for model in [args.model] if args.model else DEFAULT_MODELS[args.provider]:
        print(f"\n{model}")
        ok = check("1. plain message", plain, model)
        if ok:
            ok = check("2. structured output", structured, model)
        all_ok = all_ok and ok

    print("\nAll checks passed." if all_ok else "\nSome checks failed. Paste this output back.")
    sys.exit(0 if all_ok else 1)


if __name__ == "__main__":
    main()
