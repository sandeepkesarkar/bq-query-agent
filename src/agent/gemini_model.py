"""A model that answers with Gemini on Vertex AI, through the google-genai SDK.

    model = GeminiModel()                 # reads config/model.yaml; uses your Google login
    model.complete_structured(prompt, schema)   # -> dict, or raises ModelError

The graph already treats an exception as a refusal at route, a rejected plan at plan and a
plain fallback at summarize, so this class raises on anything it cannot trust: empty text, a
finish reason other than STOP (a cut-off or a safety block), JSON that does not parse, or a
result that is not an object. Retrying a 429 or 5xx once is left to the SDK's retry options.
"""
import json
import os
import subprocess
from pathlib import Path

import yaml
from google import genai
from google.genai import types

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = ROOT / "config" / "model.yaml"

# The schema titles used by the graph, and the task name each one has in model.yaml.
TASKS = {"route": "route", "plan": "plan", "summary": "summarize"}
RETRY_STATUS_CODES = [429, 500, 502, 503, 504]


class ModelError(Exception):
    """The model's answer cannot be used. `finish_reason` is the name Gemini gave, if any."""

    def __init__(self, message, finish_reason=None):
        super().__init__(message)
        self.finish_reason = finish_reason


def project_id():
    """GOOGLE_CLOUD_PROJECT, or the gcloud default project."""
    pid = os.environ.get("GOOGLE_CLOUD_PROJECT")
    if not pid:
        out = subprocess.run(["gcloud", "config", "get-value", "project"],
                             capture_output=True, text=True)
        pid = out.stdout.strip()
    if not pid:
        raise ModelError("no Google Cloud project: set GOOGLE_CLOUD_PROJECT "
                         "or run `gcloud config set project <id>`")
    return pid


class GeminiModel:
    def __init__(self, config=None, client=None):
        """`config` is the parsed model.yaml (default: read the file). `client` is for tests."""
        if config is None:
            config = yaml.safe_load(DEFAULT_CONFIG.read_text())
        if config.get("provider") != "gemini":
            raise ValueError(f"model.yaml provider is {config.get('provider')!r}, not 'gemini'")
        self.config = config
        self.tasks = config["tasks"]
        ids = {task: spec["model"] for task, spec in self.tasks.items()}
        # The audit line records this, so it names every model in use.
        self.name = (next(iter(ids.values())) if len(set(ids.values())) == 1
                     else ",".join(f"{task}={model}" for task, model in ids.items()))
        self._client = client
        self.prompt_tokens = 0
        self.output_tokens = 0
        self.thinking_tokens = 0

    def _get_client(self):
        if self._client is None:
            options = types.HttpOptions(
                timeout=int(self.config["request_timeout_seconds"] * 1000),
                retry_options=types.HttpRetryOptions(
                    attempts=self.config["retry_attempts"],
                    http_status_codes=RETRY_STATUS_CODES,
                ),
            )
            self._client = genai.Client(
                vertexai=True, project=project_id(), location=self.config["location"],
                http_options=options,
            )
        return self._client

    def _count(self, response):
        usage = response.usage_metadata
        if usage is not None:
            self.prompt_tokens += usage.prompt_token_count or 0
            self.output_tokens += usage.candidates_token_count or 0
            self.thinking_tokens += usage.thoughts_token_count or 0

    def complete_structured(self, prompt, schema):
        title = schema.get("title")
        if title not in TASKS:
            raise ValueError(f"no model task for schema {title!r}")
        task = self.tasks[TASKS[title]]
        response = self._get_client().models.generate_content(
            model=task["model"],
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_json_schema=schema,
                temperature=self.config["temperature"],
                max_output_tokens=task["max_output_tokens"],
            ),
        )
        self._count(response)

        if not response.candidates:
            feedback = response.prompt_feedback
            raise ModelError(f"{title}: no answer returned"
                             + (f" (prompt blocked: {feedback.block_reason})" if feedback else ""))
        reason = response.candidates[0].finish_reason
        if reason != types.FinishReason.STOP:
            name = getattr(reason, "name", str(reason))
            raise ModelError(f"{title}: the answer ended with finish reason {name}, not STOP",
                             finish_reason=name)
        text = (response.text or "").strip()
        if not text:
            raise ModelError(f"{title}: the answer was empty", finish_reason="EMPTY")
        try:
            out = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ModelError(f"{title}: the answer is not valid JSON ({exc})") from exc
        if not isinstance(out, dict):
            raise ModelError(f"{title}: the answer is a {type(out).__name__}, not an object")
        return out
