from __future__ import annotations

import copy
import datetime as _dt
import json
import logging
import os
import re
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable
from jsonschema import Draft202012Validator

# Credentials come only from server environment variables.
DEFAULT_GOOGLE_API_KEY = ""
DEFAULT_CLOUDFLARE_ACCOUNT_ID = ""
DEFAULT_CLOUDFLARE_API_TOKEN = ""
DEFAULT_OPENROUTER_API_KEY = ""

logger = logging.getLogger("tarot_oracle.cascade")

STREAM_TIMEOUT_SECONDS = 60
JSON_TIMEOUT_SECONDS = 45
TELEMETRY_LIMIT = 500
TRANSIENT_COOLDOWN_SECONDS = 60.0
JSON_ROUTE_TTL_SECONDS = 300.0
AUTH_COOLDOWN_SECONDS = 3600.0
CIRCUIT_BREAKER_FAILURES = 3

QUOTA_NOTICE = (
    "Today's free Gemma 26B quota is used up on every provider. "
    "Please try again tomorrow."
)
OUTAGE_NOTICE = (
    "The Gemma 26B providers are not responding right now. "
    "Please try again in a little while."
)

_DAILY_PATTERN = re.compile(r"per[\s_-]?day|perday|daily|\brpd\b|neurons", re.IGNORECASE)


class ModelQuotaExhaustedError(RuntimeError):
    """A provider (or the whole cascade) is out of quota or unusable for a while."""

    def __init__(self, message: str, *, cooldown: float | None = None, kind: str = "quota"):
        super().__init__(message)
        self.cooldown = cooldown
        self.kind = kind


class ReadingCancelled(Exception):
    """The visitor stopped the reading; abort without blaming the provider."""


class ModelProviderError(RuntimeError):
    """Raised when an individual provider fails transiently or returns unusable output."""


class _BadRequest(ModelProviderError):
    """HTTP 400/404/422: the request shape is not accepted by this provider."""


@dataclass
class CallTelemetry:
    provider: str
    model: str
    call_type: str  # "stream" or "json"
    prompt_tokens: int = 0
    completion_tokens: int = 0
    ttft_seconds: float = 0.0
    total_seconds: float = 0.0
    tokens_per_second: float = 0.0
    success: bool = True
    error_message: str | None = None
    timestamp: float = field(default_factory=time.time)


@dataclass
class ProviderStatus:
    name: str
    display_name: str
    enabled: bool = True
    quota_exhausted: bool = False
    exhaustion_timestamp: float = 0.0
    cooldown_duration: float = 3600.0
    consecutive_failures: int = 0
    last_error: str | None = None
    total_calls: int = 0
    total_tokens: int = 0


def _seconds_until_utc_hour(hour: int, now: float | None = None) -> float:
    current = _dt.datetime.fromtimestamp(now if now is not None else time.time(), tz=_dt.timezone.utc)
    target = current.replace(hour=hour, minute=0, second=0, microsecond=0)
    if target <= current:
        target += _dt.timedelta(days=1)
    return max(300.0, (target - current).total_seconds())


def _required_keys_missing(schema: dict[str, Any], parsed: dict[str, Any]) -> list[str]:
    required = schema.get("required") if isinstance(schema, dict) else None
    if not isinstance(required, list):
        return []
    return [str(key) for key in required if key not in parsed]


def _strip_optional_nulls(schema: dict[str, Any], parsed: Any) -> Any:
    """Strip null values on optional properties so Gemma's null placeholders pass schema validation."""
    if not isinstance(schema, dict) or not isinstance(parsed, dict):
        return parsed
    req_list = schema.get("required")
    required = {str(k) for k in req_list} if isinstance(req_list, list) else set()
    properties = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
    for key in list(parsed.keys()):
        if schema.get("additionalProperties") is False and properties and key not in properties:
            del parsed[key]
            continue
        val = parsed[key]
        prop_schema = properties.get(key) if isinstance(properties.get(key), dict) else None
        if val is None:
            prop_type = prop_schema.get("type") if prop_schema else None
            allows_null = (prop_type == "null") or (isinstance(prop_type, list) and "null" in prop_type)
            if key not in required and not allows_null:
                del parsed[key]
            elif (
                key in required
                and not allows_null
                and prop_schema
                and prop_schema.get("type") == "object"
                and not prop_schema.get("required")
            ):
                parsed[key] = {}
        elif isinstance(val, int) and not isinstance(val, bool) and prop_schema:
            minimum = prop_schema.get("minimum")
            if isinstance(minimum, int) and val < minimum and parsed.get("kind") == "NONE":
                parsed[key] = minimum
        elif isinstance(val, str) and prop_schema and "enum" not in prop_schema:
            max_len = prop_schema.get("maxLength")
            if isinstance(max_len, int) and max_len > 0 and len(val) > max_len:
                parsed[key] = val[:max_len]
        elif isinstance(val, dict) and prop_schema:
            _strip_optional_nulls(prop_schema, val)
        elif isinstance(val, list) and prop_schema and isinstance(prop_schema.get("items"), dict):
            item_schema = prop_schema["items"]
            for item in val:
                if isinstance(item, dict):
                    _strip_optional_nulls(item_schema, item)
    return parsed


def _schema_mode_key(provider: str, schema: dict[str, Any]) -> str:
    try:
        serialized = json.dumps(schema, sort_keys=True, separators=(",", ":"))
    except TypeError:
        serialized = str(id(schema))
    return f"{provider}:{serialized}"


def _strip_reasoning(raw: str) -> str:
    raw = raw.strip()
    for open_tag, close_tag in (("<thought>", "</thought>"), ("<think>", "</think>")):
        if open_tag in raw and close_tag in raw:
            raw = re.sub(re.escape(open_tag) + r".*?" + re.escape(close_tag), "", raw, flags=re.DOTALL).strip()
        elif close_tag in raw:
            raw = raw.split(close_tag, 1)[1].strip()
    if raw.startswith("```"):
        raw = raw.strip("`").strip()
        if raw.casefold().startswith("json"):
            raw = raw[4:].strip()
    return raw


def _parse_json_object(raw: str) -> dict[str, Any]:
    raw = _strip_reasoning(raw)
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        start, end = raw.find("{"), raw.rfind("}")
        if start < 0 or end <= start:
            raise
        parsed = json.loads(raw[start:end + 1])
    if not isinstance(parsed, dict):
        raise ModelProviderError("Provider returned a non-object JSON value")
    return parsed


def _schema_instruction(schema: dict[str, Any]) -> str:
    return (
        "Return exactly one JSON object and nothing else. It must validate against this JSON Schema "
        "(all required keys present, no prose, no markdown fences):\n"
        + json.dumps(schema, ensure_ascii=False, separators=(",", ":"))
    )


def _with_schema_instruction(messages: list[dict[str, str]], schema: dict[str, Any]) -> list[dict[str, str]]:
    result = copy.deepcopy(messages)
    instruction = _schema_instruction(schema)
    for message in result:
        if message.get("role") == "system":
            message["content"] = f"{message.get('content', '')}\n\n{instruction}"
            return result
    return [{"role": "system", "content": instruction}, *result]


class CascadingModelClient:
    """
    Multi-tier cascading LLM client (shared with Aether Harbor) (order comes from settings):
    Cloudflare Workers AI -> Google AI Studio -> OpenRouter -> Local LM Studio.

    - Remote providers run in parallel for different players; only the local
      LM Studio tier is serialized through the shared inference lock.
    - HTTP 429 is classified into per-minute spikes (short cooldown, honours
      Retry-After) and daily quota exhaustion (cooldown until the daily reset).
    - Empty or error-carrying streams are failures, never successes.
    - A stream that fails after emitting text triggers ``on_reset`` before the
      next provider starts, so a player never sees two mixed narrations.
    - Structured JSON calls send the real JSON Schema (json_schema), degrading
      per provider to json_object / prompt-embedded schema only when rejected,
      and results missing required keys fall through to the next provider.
    - When every provider fails, an exception is raised; the caller decides how
      to present it. Outage notices are never returned as narration.
    """

    def __init__(
        self,
        *,
        config: dict[str, Any] | None = None,
        request_lock: threading.Lock | None = None,
    ):
        self.config = config or {}
        self._local_lock = request_lock if request_lock is not None else threading.Lock()
        self._status_lock = threading.Lock()
        self.telemetry_history: list[CallTelemetry] = []
        self._json_mode: dict[str, str] = {}
        self._json_routes: dict[str, tuple[str, float]] = {}
        self._google_mode = "native_system"
        self._local_resolved_model: str | None = None
        self._load_provider_configs()

    # ------------------------------------------------------------------ config
    def _load_provider_configs(self) -> None:
        cfg = self.config
        cascade_order = cfg.get("active_provider_cascade") or ["cloudflare", "google", "openrouter", "local"]
        self.cascade_order: list[str] = list(cascade_order)

        google_cfg = cfg.get("google", {})
        self.google_key = str(
            os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY")
            or google_cfg.get("api_key") or DEFAULT_GOOGLE_API_KEY or ""
        ).strip()
        self.google_model = str(google_cfg.get("model_id") or "gemma-4-26b-a4b-it").strip()
        self.google_url = str(
            google_cfg.get("base_url") or "https://generativelanguage.googleapis.com/v1beta/openai"
        ).rstrip("/")
        native = self.google_url[: -len("/openai")] if self.google_url.endswith("/openai") else self.google_url
        self.google_native_url = str(google_cfg.get("native_base_url") or native).rstrip("/")
        self.google_thinking_level = str(google_cfg.get("thinking_level") or "MINIMAL").upper()

        cf_cfg = cfg.get("cloudflare", {})
        self.cf_token = str(
            os.environ.get("CLOUDFLARE_API_TOKEN") or cf_cfg.get("api_token") or DEFAULT_CLOUDFLARE_API_TOKEN or ""
        ).strip()
        self.cf_account_id = str(
            os.environ.get("CLOUDFLARE_ACCOUNT_ID") or cf_cfg.get("account_id") or DEFAULT_CLOUDFLARE_ACCOUNT_ID or ""
        ).strip()
        self.cf_model = str(cf_cfg.get("model_id") or "@cf/google/gemma-4-26b-a4b-it").strip()
        if self.cf_account_id:
            self.cf_url = f"https://api.cloudflare.com/client/v4/accounts/{self.cf_account_id}/ai/v1"
        else:
            self.cf_url = str(cf_cfg.get("base_url") or "").rstrip("/")

        or_cfg = cfg.get("openrouter", {})
        self.or_key = str(
            os.environ.get("OPENROUTER_API_KEY") or or_cfg.get("api_key") or DEFAULT_OPENROUTER_API_KEY or ""
        ).strip()
        self.or_model = str(or_cfg.get("model_id") or "google/gemma-4-26b-a4b-it:free").strip()
        self.or_url = str(or_cfg.get("base_url") or "https://openrouter.ai/api/v1").rstrip("/")

        local_cfg = cfg.get("local", {})
        local_url = str(local_cfg.get("base_url") or "http://127.0.0.1:1234").rstrip("/")
        # LM Studio's OpenAI-compatible routes live under /v1.
        self.local_url = local_url if local_url.endswith("/v1") else f"{local_url}/v1"
        self.local_model = str(local_cfg.get("model_id") or "auto").strip()
        env_local = os.environ.get("AETHER_LOCAL_FALLBACK")
        self.local_enabled = bool(local_cfg.get("enabled", True)) if env_local is None else env_local.strip() not in {"0", "false", "no"}

        self.provider_statuses: dict[str, ProviderStatus] = {
            "google": ProviderStatus("google", "Google AI Studio", enabled=bool(self.google_key)),
            "cloudflare": ProviderStatus("cloudflare", "Cloudflare Workers AI", enabled=bool(self.cf_token and self.cf_account_id)),
            "openrouter": ProviderStatus("openrouter", "OpenRouter Free", enabled=bool(self.or_key)),
            "local": ProviderStatus("local", "Local LM Studio", enabled=self.local_enabled),
        }

        self.base_url = self._get_primary_active_endpoint()
        self.model_id = self._get_primary_active_model()
        self.display_model_id = "Gemma 26B"

    def _get_primary_active_endpoint(self) -> str:
        urls = {"google": self.google_url, "cloudflare": self.cf_url, "openrouter": self.or_url, "local": self.local_url}
        for p in self.cascade_order:
            status = self.provider_statuses.get(p)
            if status and status.enabled and not status.quota_exhausted:
                return urls.get(p, self.local_url)
        return self.local_url

    def _get_primary_active_model(self) -> str:
        models = {"google": self.google_model, "cloudflare": self.cf_model, "openrouter": self.or_model, "local": self.local_model}
        for p in self.cascade_order:
            status = self.provider_statuses.get(p)
            if status and status.enabled and not status.quota_exhausted:
                return models.get(p, "gemma-4-26b-a4b-it")
        return "gemma-4-26b-a4b-it"

    def ensure_ready(self, status: Callable[[str], None] | None = None) -> None:
        status = status or (lambda _message: None)
        status("Connected to Gemma 26B · Ready")

    # ------------------------------------------------------- quota bookkeeping
    def _available(self, provider: str) -> bool:
        status = self.provider_statuses.get(provider)
        return bool(status and status.enabled and not status.quota_exhausted)

    def _mark_exhausted(self, provider: str, error_msg: str, cooldown: float | None = None) -> None:
        with self._status_lock:
            status = self.provider_statuses.get(provider)
            if not status:
                return
            status.quota_exhausted = True
            status.exhaustion_timestamp = time.time()
            status.last_error = error_msg[:500]
            status.cooldown_duration = float(cooldown if cooldown is not None else TRANSIENT_COOLDOWN_SECONDS)
        logger.warning("Provider %s paused for %.0fs: %s", provider, status.cooldown_duration, error_msg[:300])

    def _check_cooldown_reset(self) -> None:
        now = time.time()
        with self._status_lock:
            for p, status in self.provider_statuses.items():
                if status.quota_exhausted and (now - status.exhaustion_timestamp > status.cooldown_duration):
                    status.quota_exhausted = False
                    status.consecutive_failures = 0
                    logger.info("Provider %s cooldown expired, re-enabling.", p)

    def _daily_cooldown(self, provider: str) -> float:
        # Google AI Studio resets at midnight Pacific (08:00 UTC in winter);
        # Cloudflare neurons and OpenRouter free limits reset at 00:00 UTC.
        return _seconds_until_utc_hour(8 if provider == "google" else 0)

    def _quota_error(self, provider: str, code: int, body: str, headers: Any) -> ModelQuotaExhaustedError:
        if code == 429:
            if _DAILY_PATTERN.search(body or ""):
                return ModelQuotaExhaustedError(
                    f"HTTP 429 daily quota exhausted on {provider}: {body[:300]}",
                    cooldown=self._daily_cooldown(provider), kind="daily",
                )
            retry_after = None
            try:
                value = headers.get("Retry-After") if headers is not None else None
                retry_after = float(value) if value not in (None, "") else None
            except (TypeError, ValueError):
                retry_after = None
            cooldown = max(5.0, min(retry_after, 3600.0)) if retry_after else TRANSIENT_COOLDOWN_SECONDS
            return ModelQuotaExhaustedError(
                f"HTTP 429 rate limit on {provider}: {body[:300]}", cooldown=cooldown, kind="minute"
            )
        if code == 402:
            return ModelQuotaExhaustedError(
                f"HTTP 402 credits exhausted on {provider}: {body[:300]}",
                cooldown=self._daily_cooldown(provider), kind="daily",
            )
        return ModelQuotaExhaustedError(
            f"HTTP {code} authentication/permission error on {provider}: {body[:300]}",
            cooldown=AUTH_COOLDOWN_SECONDS, kind="auth",
        )

    def _http_error(self, provider: str, exc: urllib.error.HTTPError) -> Exception:
        body = ""
        try:
            body = exc.read().decode("utf-8", errors="replace") if exc.fp is not None else ""
        except Exception:
            pass
        if exc.code in (401, 402, 403, 429):
            return self._quota_error(provider, exc.code, body, getattr(exc, "headers", None))
        if exc.code in (400, 404, 422):
            return _BadRequest(f"HTTP {exc.code} bad request on {provider}: {body[:300]}")
        return ModelProviderError(f"HTTP {exc.code} error on {provider}: {body[:300]}")

    def _record(self, telem: CallTelemetry) -> None:
        with self._status_lock:
            self.telemetry_history.append(telem)
            if len(self.telemetry_history) > TELEMETRY_LIMIT:
                del self.telemetry_history[: len(self.telemetry_history) - TELEMETRY_LIMIT]

    def _note_success(self, provider: str, telem: CallTelemetry) -> None:
        self._record(telem)
        with self._status_lock:
            status = self.provider_statuses[provider]
            status.total_calls += 1
            status.total_tokens += int(telem.completion_tokens or 0)
            status.consecutive_failures = 0

    def _note_failure(self, provider: str, call_type: str, exc: Exception) -> None:
        message = str(exc)
        self._record(CallTelemetry(provider=provider, model="", call_type=call_type, success=False, error_message=message[:500]))
        with self._status_lock:
            status = self.provider_statuses[provider]
            status.consecutive_failures += 1
            status.last_error = message[:500]
            failures = status.consecutive_failures
        if isinstance(exc, ModelQuotaExhaustedError):
            self._mark_exhausted(provider, message, exc.cooldown)
        elif failures >= CIRCUIT_BREAKER_FAILURES:
            self._mark_exhausted(provider, f"{failures} consecutive failures: {message}", TRANSIENT_COOLDOWN_SECONDS)

    def _final_error(self, errors: list[tuple[str, Exception]], call_type: str) -> ModelQuotaExhaustedError:
        def _daily(exc: Exception) -> bool:
            return isinstance(exc, ModelQuotaExhaustedError) and exc.kind == "daily"

        # An unreachable local fallback does not change the story: if every
        # remote provider is out of daily quota, tell the player so.
        quota_only = any(_daily(exc) for _p, exc in errors) and all(
            _daily(exc) or p == "local" for p, exc in errors
        )
        if not errors:
            # Everything is paused: report quota only if a daily pause is active.
            quota_only = any(
                s.enabled and s.quota_exhausted and s.cooldown_duration > TRANSIENT_COOLDOWN_SECONDS * 2
                for s in self.provider_statuses.values()
            ) and not any(self._available(p) for p in self.cascade_order)
        notice = QUOTA_NOTICE if quota_only else OUTAGE_NOTICE
        detail = "; ".join(f"{p}: {exc}" for p, exc in errors) or "No active providers available in cascade."
        error = ModelQuotaExhaustedError(f"{notice} ({call_type}: {detail})", kind="cascade")
        error.player_message = notice  # type: ignore[attr-defined]
        return error

    # ------------------------------------------------------------- streaming
    def stream_chat(
        self,
        messages: list[dict[str, str]],
        *,
        on_token: Callable[[str], None],
        on_reset: Callable[[], None] | None = None,
        temperature: float = 0.8,
    ) -> str:
        self._check_cooldown_reset()
        errors: list[tuple[str, Exception]] = []

        for provider in self.cascade_order:
            if not self._available(provider):
                continue
            emitted = {"any": False}

            def forward(token: str) -> None:
                emitted["any"] = True
                on_token(token)

            try:
                if provider == "local":
                    with self._local_lock:
                        result, telem = self._invoke_stream_provider(provider, messages, on_token=forward, temperature=temperature)
                else:
                    result, telem = self._invoke_stream_provider(provider, messages, on_token=forward, temperature=temperature)
                if not result.strip():
                    raise ModelProviderError(f"Empty narration stream from {provider}")
                self._note_success(provider, telem)
                return result
            except ReadingCancelled:
                raise
            except Exception as exc:
                errors.append((provider, exc))
                self._note_failure(provider, "stream", exc)
                logger.warning("Provider %s failed during stream_chat: %s", provider, exc)
                if emitted["any"]:
                    if on_reset is None:
                        # Without a reset channel the caller cannot retract the
                        # partial text; never splice a second provider onto it.
                        raise ModelProviderError(
                            f"Narration stream from {provider} was interrupted: {exc}"
                        ) from exc
                    on_reset()
                continue

        raise self._final_error(errors, "stream")

    def _invoke_stream_provider(
        self,
        provider: str,
        messages: list[dict[str, str]],
        *,
        on_token: Callable[[str], None],
        temperature: float = 0.8,
    ) -> tuple[str, CallTelemetry]:
        while provider == "google" and self._google_mode != "openai":
            try:
                return self._google_native_stream(messages, on_token=on_token, temperature=temperature)
            except _BadRequest as exc:
                if not self._downgrade_google(exc):
                    raise
        return self._openai_stream(provider, messages, on_token=on_token, temperature=temperature)

    def _openai_stream(
        self,
        provider: str,
        messages: list[dict[str, str]],
        *,
        on_token: Callable[[str], None],
        temperature: float = 0.8,
    ) -> tuple[str, CallTelemetry]:
        url, model_name, headers = self._prepare_provider_req(provider)
        payload: dict[str, Any] = {"model": model_name, "messages": messages, "temperature": float(temperature), "stream": True}
        if provider in ("cloudflare", "local"):
            payload["chat_template_kwargs"] = {"enable_thinking": False}
        req = urllib.request.Request(
            f"{url}/chat/completions", data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST"
        )

        t_start = time.time()
        t_first: float | None = None
        pieces: list[str] = []
        in_thought = False
        usage: dict[str, Any] = {}
        completed = False
        try:
            with urllib.request.urlopen(req, timeout=STREAM_TIMEOUT_SECONDS) as response:
                for line in response:
                    line_str = line.decode("utf-8", errors="replace").strip()
                    if not line_str.startswith("data:"):
                        continue
                    data = line_str[5:].strip()
                    if data == "[DONE]":
                        completed = True
                        break
                    try:
                        chunk = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(chunk, dict) and chunk.get("error"):
                        err = chunk["error"]
                        message = json.dumps(err, ensure_ascii=False) if not isinstance(err, str) else err
                        code = err.get("code") if isinstance(err, dict) else None
                        if code in (429, "429") or "rate" in message.lower() or "quota" in message.lower():
                            raise self._quota_error(provider, 429, message, None)
                        raise ModelProviderError(f"Stream error from {provider}: {message[:300]}")
                    if isinstance(chunk.get("usage"), dict):
                        usage = chunk["usage"]
                    choices = chunk.get("choices") or []
                    if not choices:
                        continue
                    finish = choices[0].get("finish_reason")
                    if finish in {"length", "content_filter"}:
                        raise ModelProviderError(f"Incomplete narration from {provider}: {finish}")
                    if finish in {"stop", "eos"}:
                        completed = True
                    delta = choices[0].get("delta") or {}
                    content = delta.get("content")
                    if not content:
                        continue
                    extra = delta.get("extra_content") if isinstance(delta.get("extra_content"), dict) else {}
                    if (extra.get("google") or {}).get("thought") is True:
                        continue
                    if "<thought>" in content:
                        in_thought = True
                    if in_thought:
                        if "</thought>" in content:
                            in_thought = False
                            content = content.split("</thought>", 1)[1]
                            if not content:
                                continue
                        else:
                            continue
                    if t_first is None:
                        t_first = time.time()
                    pieces.append(content)
                    on_token(content)
        except urllib.error.HTTPError as exc:
            raise self._http_error(provider, exc) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ModelProviderError(f"Network timeout / connection error on {provider}: {exc}") from exc

        if not completed:
            raise ModelProviderError(f"Unterminated narration stream from {provider}")
        return "".join(pieces).strip(), self._stream_telemetry(provider, model_name, pieces, t_start, t_first, usage)

    @staticmethod
    def _stream_telemetry(provider, model_name, pieces, t_start, t_first, usage) -> CallTelemetry:
        total = max(0.001, time.time() - t_start)
        text = "".join(pieces)
        completion = int(usage.get("completion_tokens") or usage.get("candidatesTokenCount") or 0) or max(
            len(pieces), len(text.split()) * 4 // 3
        )
        return CallTelemetry(
            provider=provider,
            model=model_name,
            call_type="stream",
            prompt_tokens=int(usage.get("prompt_tokens") or usage.get("promptTokenCount") or 0),
            completion_tokens=completion,
            ttft_seconds=(t_first - t_start) if t_first else total,
            total_seconds=total,
            tokens_per_second=completion / total if pieces else 0.0,
            success=True,
        )

    # ------------------------------------------------------ Google native REST
    def _downgrade_google(self, exc: Exception) -> bool:
        """Step down Google request shapes after a 400: system field -> merged -> OpenAI proxy."""
        previous = self._google_mode
        self._google_mode = {"native_system": "native_merged", "native_merged": "openai"}.get(previous, "openai")
        logger.warning("Google native request rejected (%s); switching %s -> %s", exc, previous, self._google_mode)
        return previous != self._google_mode

    def _google_payload(self, messages: list[dict[str, str]], generation: dict[str, Any]) -> dict[str, Any]:
        system_parts = [str(m.get("content") or "") for m in messages if m.get("role") == "system"]
        contents: list[dict[str, Any]] = []
        for message in messages:
            role = message.get("role")
            if role == "system":
                continue
            g_role = "model" if role == "assistant" else "user"
            text = str(message.get("content") or "")
            if contents and contents[-1]["role"] == g_role:
                contents[-1]["parts"][0]["text"] += "\n\n" + text
            else:
                contents.append({"role": g_role, "parts": [{"text": text}]})
        system_text = "\n\n".join(part for part in system_parts if part)
        payload: dict[str, Any] = {"generationConfig": generation}
        if system_text and self._google_mode == "native_system":
            payload["systemInstruction"] = {"parts": [{"text": system_text}]}
        elif system_text:
            if contents and contents[0]["role"] == "user":
                contents[0]["parts"][0]["text"] = system_text + "\n\n" + contents[0]["parts"][0]["text"]
            else:
                contents.insert(0, {"role": "user", "parts": [{"text": system_text}]})
        if not contents:
            contents = [{"role": "user", "parts": [{"text": "Begin."}]}]
        payload["contents"] = contents
        return payload

    def _google_generation(self, temperature: float) -> dict[str, Any]:
        generation: dict[str, Any] = {"temperature": float(temperature)}
        if self.google_thinking_level and self.google_thinking_level != "NONE":
            generation["thinkingConfig"] = {"thinkingLevel": self.google_thinking_level}
        return generation

    def _google_request(self, method: str, payload: dict[str, Any]) -> urllib.request.Request:
        suffix = "?alt=sse" if method == "streamGenerateContent" else ""
        return urllib.request.Request(
            f"{self.google_native_url}/models/{self.google_model}:{method}{suffix}",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", "x-goog-api-key": self.google_key},
            method="POST",
        )

    @staticmethod
    def _google_text(chunk: dict[str, Any]) -> str:
        feedback = chunk.get("promptFeedback") if isinstance(chunk.get("promptFeedback"), dict) else {}
        if feedback.get("blockReason"):
            raise ModelProviderError(f"Google blocked the prompt: {feedback.get('blockReason')}")
        pieces: list[str] = []
        for candidate in chunk.get("candidates") or []:
            content = candidate.get("content") if isinstance(candidate, dict) else None
            for part in (content or {}).get("parts") or []:
                if isinstance(part, dict) and part.get("text") and part.get("thought") is not True:
                    pieces.append(str(part["text"]))
            break
        return "".join(pieces)

    def _google_native_stream(
        self, messages: list[dict[str, str]], *, on_token: Callable[[str], None], temperature: float = 0.8
    ) -> tuple[str, CallTelemetry]:
        payload = self._google_payload(messages, self._google_generation(temperature))
        req = self._google_request("streamGenerateContent", payload)
        t_start = time.time()
        t_first: float | None = None
        pieces: list[str] = []
        usage: dict[str, Any] = {}
        completed = False
        try:
            with urllib.request.urlopen(req, timeout=STREAM_TIMEOUT_SECONDS) as response:
                for line in response:
                    line_str = line.decode("utf-8", errors="replace").strip()
                    if not line_str.startswith("data:"):
                        continue
                    try:
                        chunk = json.loads(line_str[5:].strip())
                    except json.JSONDecodeError:
                        continue
                    if isinstance(chunk, dict) and chunk.get("error"):
                        raise ModelProviderError(f"Stream error from google: {json.dumps(chunk['error'])[:300]}")
                    if isinstance(chunk.get("usageMetadata"), dict):
                        usage = chunk["usageMetadata"]
                    for candidate in chunk.get("candidates") or []:
                        reason = candidate.get("finishReason")
                        if reason == "STOP":
                            completed = True
                        elif reason:
                            raise ModelProviderError(f"Incomplete narration from google: {reason}")
                    text = self._google_text(chunk)
                    if not text:
                        continue
                    if t_first is None:
                        t_first = time.time()
                    pieces.append(text)
                    on_token(text)
        except urllib.error.HTTPError as exc:
            raise self._http_error("google", exc) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ModelProviderError(f"Network timeout / connection error on google: {exc}") from exc
        if not completed:
            raise ModelProviderError("Unterminated narration stream from google")
        return "".join(pieces).strip(), self._stream_telemetry("google", self.google_model, pieces, t_start, t_first, usage)

    def _default_json_mode(self, provider: str, schema: dict[str, Any]) -> str:
        schema_key = _schema_mode_key(provider, schema)
        if schema_key in self._json_mode:
            return self._json_mode[schema_key]
        if provider in self._json_mode:
            return self._json_mode[provider]
        if provider == "google" and "gemma" in self.google_model.casefold():
            return "json_object"
        props = schema.get("properties") if isinstance(schema, dict) else None
        if provider == "cloudflare" and isinstance(props, dict) and len(props) >= 12:
            return "json_object"
        return "json_schema"

    def _google_native_json(
        self, messages: list[dict[str, str]], *, schema: dict[str, Any], temperature: float
    ) -> tuple[dict[str, Any], CallTelemetry]:
        mode = self._default_json_mode("google", schema)
        generation = self._google_generation(temperature)
        generation["responseMimeType"] = "application/json"
        send_messages = messages
        if mode == "json_schema":
            generation["responseJsonSchema"] = schema
        else:
            send_messages = _with_schema_instruction(messages, schema)
        req = self._google_request("generateContent", self._google_payload(send_messages, generation))
        t_start = time.time()
        try:
            with urllib.request.urlopen(req, timeout=JSON_TIMEOUT_SECONDS) as response:
                body = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise self._http_error("google", exc) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ModelProviderError(f"Timeout/Network error on google: {exc}") from exc
        raw = self._google_text(body)
        if not raw.strip():
            raise ModelProviderError("Empty JSON response from google")
        parsed = _parse_json_object(raw)
        usage = body.get("usageMetadata") if isinstance(body.get("usageMetadata"), dict) else {}
        total = max(0.001, time.time() - t_start)
        completion = int(usage.get("candidatesTokenCount") or len(raw.split()))
        return parsed, CallTelemetry(
            provider="google", model=self.google_model, call_type="json",
            prompt_tokens=int(usage.get("promptTokenCount") or 0), completion_tokens=completion,
            total_seconds=total, tokens_per_second=completion / total, success=True,
        )

    # ------------------------------------------------------------- JSON calls
    def _json_provider_order(self, schema_key: str) -> list[str]:
        order = self.cascade_order.copy()
        with self._status_lock:
            route = self._json_routes.get(schema_key)
        if route and time.monotonic() < route[1] and route[0] in order:
            order.remove(route[0])
            order.insert(0, route[0])
            return order
        if (
            '"movement_dependency"' not in schema_key
            and '"selected_site"' not in schema_key
            and "cloudflare" in order
            and "google" in order
            and order[0] == "cloudflare"
            and self._available("google")
        ):
            order.remove("google")
            order.insert(0, "google")
        return order

    def _remember_json_fallback(self, schema_key: str, provider: str) -> None:
        # A validated remote fallback is useful evidence for this schema only.
        # Expiry restores normal priority and never sleeps or skips a tier.
        now = time.monotonic()
        with self._status_lock:
            self._json_routes = {k: v for k, v in self._json_routes.items() if v[1] > now}
            if len(self._json_routes) >= 64:
                del self._json_routes[min(self._json_routes, key=lambda k: self._json_routes[k][1])]
            self._json_routes[schema_key] = (provider, now + JSON_ROUTE_TTL_SECONDS)

    def chat_json(
        self,
        messages: list[dict[str, str]],
        *,
        schema: dict[str, Any],
        temperature: float = 0.1,
        **_ignored: Any,
    ) -> dict[str, Any]:
        self._check_cooldown_reset()
        errors: list[tuple[str, Exception]] = []
        schema_key = _schema_mode_key("route", schema)
        fields = schema.get("properties", {})
        label = "action risk" if "movement_dependency" in fields else "review" if "approved" in fields else "helper"

        for provider in self._json_provider_order(schema_key):
            if not self._available(provider):
                continue
            started = time.monotonic()
            try:
                if provider == "local":
                    with self._local_lock:
                        result, telem = self._invoke_json_provider(provider, messages, schema=schema, temperature=temperature)
                else:
                    result, telem = self._invoke_json_provider(provider, messages, schema=schema, temperature=temperature)
                _strip_optional_nulls(schema, result)
                missing = _required_keys_missing(schema, result)
                if missing:
                    raise ModelProviderError(f"{provider} JSON is missing required keys: {', '.join(missing[:8])}")
                validation_error = next(Draft202012Validator(schema).iter_errors(result), None)
                if validation_error is not None:
                    path = ".".join(str(p) for p in validation_error.absolute_path) or "$"
                    raise ModelProviderError(f"{provider} JSON violates schema at {path} ({validation_error.validator})")
                self._note_success(provider, telem)
                if errors and provider != "local":
                    self._remember_json_fallback(schema_key, provider)
                if label != "helper":
                    logger.info("%s via %s in %.1fs", label, provider, time.monotonic() - started)
                return result
            except Exception as exc:
                errors.append((provider, exc))
                self._note_failure(provider, "json", exc)
                logger.warning("Provider %s failed during %s JSON after %.1fs: %s",
                               provider, label, time.monotonic() - started, exc)
                continue

        raise self._final_error(errors, "json")

    def _invoke_json_provider(
        self,
        provider: str,
        messages: list[dict[str, str]],
        *,
        schema: dict[str, Any],
        temperature: float,
    ) -> tuple[dict[str, Any], CallTelemetry]:
        ladder = ["json_schema", "json_object", "none"]
        schema_key = _schema_mode_key(provider, schema)
        while True:
            try:
                if provider == "google" and self._google_mode != "openai":
                    return self._google_native_json(messages, schema=schema, temperature=temperature)
                return self._openai_json(provider, messages, schema=schema, temperature=temperature)
            except _BadRequest as exc:
                if provider == "google" and self._google_mode != "openai":
                    mode = self._default_json_mode("google", schema)
                    if mode == "json_schema":
                        self._json_mode["google"] = "json_object"
                        self._json_mode[schema_key] = "json_object"
                        continue
                    self._json_mode.pop(schema_key, None)
                    self._json_mode.pop("google", None)
                    if self._downgrade_google(exc):
                        continue
                    raise
                mode = self._default_json_mode(provider, schema)
                if mode == "none":
                    raise
                next_mode = ladder[ladder.index(mode) + 1]
                self._json_mode[schema_key] = next_mode
                logger.warning("%s rejected %s response format; retrying with %s", provider, mode, next_mode)

    def _openai_json(
        self,
        provider: str,
        messages: list[dict[str, str]],
        *,
        schema: dict[str, Any],
        temperature: float,
    ) -> tuple[dict[str, Any], CallTelemetry]:
        url, model_name, headers = self._prepare_provider_req(provider)
        schema_key = _schema_mode_key(provider, schema)
        mode = self._default_json_mode(provider, schema)
        send_messages = messages if mode == "json_schema" else _with_schema_instruction(messages, schema)
        payload: dict[str, Any] = {
            "model": model_name,
            "messages": send_messages,
            "temperature": float(temperature),
            "stream": False,
        }
        if provider in ("cloudflare", "local"):
            payload["chat_template_kwargs"] = {"enable_thinking": False}
        if mode == "json_schema":
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "aether_helper", "strict": True, "schema": schema},
            }
        elif mode == "json_object":
            payload["response_format"] = {"type": "json_object"}

        req = urllib.request.Request(
            f"{url}/chat/completions", data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST"
        )
        t_start = time.time()
        try:
            with urllib.request.urlopen(req, timeout=JSON_TIMEOUT_SECONDS) as response:
                body = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise self._http_error(provider, exc) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ModelProviderError(f"Timeout/Network error on {provider}: {exc}") from exc

        if isinstance(body, dict) and body.get("error"):
            raise ModelProviderError(f"{provider} returned an error body: {json.dumps(body['error'])[:300]}")
        choices = body.get("choices") or []
        if not choices:
            raise ModelProviderError(f"No choices returned by {provider}")
        message = choices[0].get("message") or {}
        raw = message.get("content")
        if isinstance(raw, (dict, list)):
            raw = json.dumps(raw)
        raw = str(raw or "").strip()
        if not raw:
            raise ModelProviderError(f"Empty JSON response from {provider}")
        try:
            parsed = _parse_json_object(raw)
        except json.JSONDecodeError as exc:
            raise ModelProviderError(f"{provider} returned invalid JSON: {exc}") from exc

        usage = body.get("usage") if isinstance(body.get("usage"), dict) else {}
        total = max(0.001, time.time() - t_start)
        completion = int(usage.get("completion_tokens") or len(raw.split()))
        return parsed, CallTelemetry(
            provider=provider, model=model_name, call_type="json",
            prompt_tokens=int(usage.get("prompt_tokens") or 0), completion_tokens=completion,
            total_seconds=total, tokens_per_second=completion / total, success=True,
        )

    # ---------------------------------------------------------------- helpers
    def _resolve_local_model(self) -> str:
        if self.local_model and self.local_model.casefold() != "auto":
            return self.local_model
        if self._local_resolved_model:
            return self._local_resolved_model
        try:
            with urllib.request.urlopen(f"{self.local_url}/models", timeout=3) as response:
                data = json.loads(response.read().decode("utf-8"))
            models = [str(m.get("id")) for m in data.get("data", []) if isinstance(m, dict) and m.get("id")]
            gemma = [m for m in models if "gemma" in m.casefold()]
            if gemma or models:
                self._local_resolved_model = (gemma or models)[0]
                return self._local_resolved_model
        except Exception as exc:
            raise ModelProviderError(f"Local LM Studio is not reachable: {exc}") from exc
        raise ModelProviderError("Local LM Studio has no loaded model")

    def _prepare_provider_req(self, provider: str) -> tuple[str, str, dict[str, str]]:
        headers = {"Content-Type": "application/json"}
        if provider == "google":
            headers["Authorization"] = f"Bearer {self.google_key}"
            return self.google_url, self.google_model, headers
        if provider == "cloudflare":
            headers["Authorization"] = f"Bearer {self.cf_token}"
            return self.cf_url, self.cf_model, headers
        if provider == "openrouter":
            headers["Authorization"] = f"Bearer {self.or_key}"
            headers["HTTP-Referer"] = "https://tarot-oracle.onrender.com"
            headers["X-Title"] = "Tarot LLM Oracle"
            return self.or_url, self.or_model, headers
        if provider == "local":
            return self.local_url, self._resolve_local_model(), headers
        raise ValueError(f"Unknown provider {provider}")

    def get_telemetry_summary(self) -> dict[str, Any]:
        """Return speed, token and quota statistics across all providers."""
        with self._status_lock:
            history = list(self.telemetry_history)
            statuses = {p: copy.copy(s) for p, s in self.provider_statuses.items()}
        now = time.time()
        summary_by_provider: dict[str, dict[str, Any]] = {}
        for p, status in statuses.items():
            calls = [t for t in history if t.provider == p and t.success]
            streams = [c for c in calls if c.call_type == "stream"]
            summary_by_provider[p] = {
                "display_name": status.display_name,
                "enabled": status.enabled,
                "quota_exhausted": status.quota_exhausted,
                "cooldown_remaining_seconds": (
                    max(0, int(status.exhaustion_timestamp + status.cooldown_duration - now))
                    if status.quota_exhausted else 0
                ),
                "total_calls": status.total_calls,
                "total_tokens": status.total_tokens,
                "failed_calls_recent": len([t for t in history if t.provider == p and not t.success]),
                "avg_tokens_per_second": round(sum(c.tokens_per_second for c in calls) / len(calls), 2) if calls else 0.0,
                "avg_ttft_seconds": round(sum(c.ttft_seconds for c in streams) / len(streams), 3) if streams else 0.0,
                "last_error": status.last_error,
            }
        return {
            "cascade_order": self.cascade_order,
            "providers": summary_by_provider,
            "total_telemetry_events": len(history),
        }

    def close(self) -> None:
        pass
