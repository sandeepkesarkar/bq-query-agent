"""A model that answers with Gemini on Vertex AI, through the google-genai SDK.

    model = GeminiModel()                 # reads config/model.yaml; uses your Google login
    model.complete_structured(prompt, schema)   # -> dict, or raises ModelError

The graph already treats an exception as a refusal at route, a rejected plan at plan and a
plain fallback at summarize, so this class raises on anything it cannot trust: empty text, a
finish reason other than STOP (a cut-off or a safety block), JSON that does not parse, or a
result that is not an object. Retrying a 429 or 5xx is left to the SDK's retry options; a task
can set its own request_timeout_seconds and retry_attempts in model.yaml.

A call that ends with MAX_TOKENS, and only that, is retried with the output cap doubled, up to
the task's ceiling and at most `max_token_retries` times. The two retries are independent: the
SDK's retry happens inside generate_content and never counts against the cap retries.
"""
import json
import os
import subprocess
import threading
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
        for task, spec in self.tasks.items():
            if "thinking_level" in spec and "thinking_budget" in spec:
                raise ValueError(f"{task}: give thinking_level or thinking_budget, not both")
            if spec.get("max_output_tokens_ceiling", spec["max_output_tokens"]) < spec["max_output_tokens"]:
                raise ValueError(f"{task}: max_output_tokens_ceiling is below max_output_tokens")
        self.max_token_retries = config.get("max_token_retries", 0)
        ids = {task: spec["model"] for task, spec in self.tasks.items()}
        # The audit line records this, so it names every model in use.
        self.name = (next(iter(ids.values())) if len(set(ids.values())) == 1
                     else ",".join(f"{task}={model}" for task, model in ids.items()))
        self._client = client
        self._lock = threading.Lock()
        self._local = threading.local()
        self.prompt_tokens = 0
        self.output_tokens = 0
        self.thinking_tokens = 0

    def http_options(self, **extra):
        """The timeout and the 429/5xx retry, as the SDK takes them."""
        return types.HttpOptions(
            timeout=int(self.config["request_timeout_seconds"] * 1000),
            retry_options=types.HttpRetryOptions(
                attempts=self.config["retry_attempts"],
                http_status_codes=RETRY_STATUS_CODES,
            ),
            **extra,
        )

    def _task_http_options(self, spec):
        """Per-request options for a task that sets its own request_timeout_seconds or
        retry_attempts in model.yaml; None for a task that uses the client's."""
        if "request_timeout_seconds" not in spec and "retry_attempts" not in spec:
            return None
        return types.HttpOptions(
            timeout=int(spec.get("request_timeout_seconds", self.config["request_timeout_seconds"]) * 1000),
            retry_options=types.HttpRetryOptions(
                attempts=spec.get("retry_attempts", self.config["retry_attempts"]),
                http_status_codes=RETRY_STATUS_CODES,
            ),
        )

    def _get_client(self):
        # Under a lock: two threads that each built a client would leave one to be garbage
        # collected, which closes it while another thread is still sending on it.
        with self._lock:
            if self._client is None:
                self._client = genai.Client(
                    vertexai=True, project=project_id(), location=self.config["location"],
                    http_options=self.http_options(),
                )
            return self._client

    @property
    def last_call(self):
        """What the calling thread's most recent complete_structured did: the task, how many
        attempts it took, the cap it finished with, and the tokens of every attempt. None
        before the first call."""
        return getattr(self._local, "last", None)

    def _thinking(self, spec):
        if "thinking_level" in spec:
            return types.ThinkingConfig(thinking_level=spec["thinking_level"].upper())
        if "thinking_budget" in spec:
            return types.ThinkingConfig(thinking_budget=spec["thinking_budget"])
        return None

    def complete_structured(self, prompt, schema):
        title = schema.get("title")
        if title not in TASKS:
            raise ValueError(f"no model task for schema {title!r}")
        spec = self.tasks[TASKS[title]]
        ceiling = spec.get("max_output_tokens_ceiling", spec["max_output_tokens"])
        cap = spec["max_output_tokens"]
        call = {"task": TASKS[title], "attempts": 0, "cap": cap,
                "tokens": {"prompt": 0, "output": 0, "thinking": 0}}
        self._local.last = call
        while True:
            call["attempts"] += 1
            call["cap"] = cap
            response = self._get_client().models.generate_content(
                model=spec["model"],
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_json_schema=schema,
                    temperature=self.config["temperature"],
                    max_output_tokens=cap,
                    thinking_config=self._thinking(spec),
                    http_options=self._task_http_options(spec),
                ),
            )
            self._count(response, call)
            reason = response.candidates[0].finish_reason if response.candidates else None
            if reason != types.FinishReason.MAX_TOKENS:
                break
            # Only a cut-off is retried. At the ceiling, or out of retries, it is an error.
            if cap >= ceiling or call["attempts"] > self.max_token_retries:
                raise ModelError(f"{title}: the answer ended with finish reason MAX_TOKENS at "
                                 f"{cap} tokens after {call['attempts']} attempt(s)",
                                 finish_reason="MAX_TOKENS")
            cap = min(cap * 2, ceiling)
        return self._parse(title, response)

    def _count(self, response, call):
        usage = response.usage_metadata
        if usage is None:
            return
        counts = {"prompt": usage.prompt_token_count or 0, "output": usage.candidates_token_count or 0,
                  "thinking": usage.thoughts_token_count or 0}
        for key, n in counts.items():
            call["tokens"][key] += n
        with self._lock:
            self.prompt_tokens += counts["prompt"]
            self.output_tokens += counts["output"]
            self.thinking_tokens += counts["thinking"]

    @staticmethod
    def _parse(title, response):
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
