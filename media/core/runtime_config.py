"""Validated, non-secret runtime catalog for media providers and capabilities."""
from __future__ import annotations

import json
import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Any


DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[1] / "config" / "runtime.json"


class RuntimeConfigError(ValueError):
    """Raised when runtime configuration or a requested capability is invalid."""


def load_runtime_config(path: Path | str = DEFAULT_CONFIG_PATH) -> dict[str, Any]:
    config_path = Path(path)
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeConfigError(f"Runtime config must be readable valid JSON: {config_path.name}") from exc
    _validate_runtime_config(config)
    return config


def _validate_runtime_config(config: Any) -> None:
    if not isinstance(config, dict) or config.get("version") != 1:
        raise RuntimeConfigError("Runtime config must be an object with supported version 1")
    providers = config.get("providers")
    models = config.get("models")
    defaults = config.get("defaults")
    if not isinstance(providers, dict) or not isinstance(models, dict) or not isinstance(defaults, dict):
        raise RuntimeConfigError("Runtime config requires providers, models, and defaults objects")
    option_labels = config.get("option_labels", {})
    if not isinstance(option_labels, dict):
        raise RuntimeConfigError("Runtime config option_labels must be an object")
    for group, labels in option_labels.items():
        if not isinstance(group, str) or not isinstance(labels, dict) or not all(
            isinstance(key, str) and isinstance(value, str) and key.strip() and value.strip()
            for key, value in labels.items()
        ):
            raise RuntimeConfigError("Runtime config option_labels must map option ids to non-empty labels")
    for provider_id, provider in providers.items():
        if not isinstance(provider, dict) or not isinstance(provider.get("api_key_env"), str):
            raise RuntimeConfigError(f"Provider {provider_id} requires an API key environment variable name")
        if not provider.get("base_url") and not (
            isinstance(provider.get("base_url_env"), str)
            and provider["base_url_env"].strip()
        ):
            raise RuntimeConfigError(f"Provider {provider_id} requires a base URL or base URL environment reference")
    for kind in ("chat", "image"):
        entries = models.get(kind)
        if not isinstance(entries, list) or not entries:
            raise RuntimeConfigError(f"Runtime config requires a non-empty {kind} model list")
        ids: set[str] = set()
        for item in entries:
            if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not item["id"].strip():
                raise RuntimeConfigError(f"Each {kind} model requires a non-empty id")
            if item["id"] in ids:
                raise RuntimeConfigError(f"Duplicate {kind} model id: {item['id']}")
            ids.add(item["id"])
            if item.get("provider") not in providers:
                raise RuntimeConfigError(f"Unknown provider for {kind} model {item['id']}")
            if not all(isinstance(item.get(flag), bool) for flag in ("enabled", "available", "verified")):
                raise RuntimeConfigError(f"Model {item['id']} requires enabled, available, and verified flags")
            for capability in ("qualities", "aspect_ratios", "thinking_modes"):
                if capability in item and not isinstance(item[capability], list):
                    raise RuntimeConfigError(f"Model {item['id']} field {capability} must be a list")
            if "request_parameters" in item and not isinstance(item["request_parameters"], dict):
                raise RuntimeConfigError(f"Model {item['id']} request_parameters must be an object")
            request_parameters = item.get("request_parameters", {})
            reserved = {"model", "messages", "prompt", "stream", "partial_images", "size", "quality", "n", "thinking", "reasoning_effort"}
            parameter_sets = [request_parameters]
            if kind == "image":
                parameter_sets = list(request_parameters.values())
                if not all(isinstance(parameters, dict) for parameters in parameter_sets):
                    raise RuntimeConfigError(f"Model {item['id']} image request parameters must be objects")
            if any(reserved.intersection(parameters) for parameters in parameter_sets):
                raise RuntimeConfigError(f"Model {item['id']} request parameters may not override reserved fields")
            if kind == "chat" and item["enabled"]:
                modes = item.get("thinking_modes", [])
                mode_parameters = item.get("thinking_mode_parameters")
                if not isinstance(mode_parameters, dict) or set(mode_parameters) != set(modes):
                    raise RuntimeConfigError(f"Enabled chat model {item['id']} requires parameters for each thinking mode")
                mode_reserved = {"model", "messages", "prompt", "stream", "size", "quality", "n"}
                for mode, parameters in mode_parameters.items():
                    if not isinstance(parameters, dict):
                        raise RuntimeConfigError(f"Chat model {item['id']} has invalid parameters for thinking mode {mode}")
                    if mode_reserved.intersection(parameters):
                        raise RuntimeConfigError(f"Chat model {item['id']} thinking parameters may not override reserved fields")
            if kind == "image" and item["enabled"]:
                size_map = item.get("size_by_aspect_ratio")
                if not isinstance(size_map, dict) or set(size_map) != set(item.get("aspect_ratios", [])):
                    raise RuntimeConfigError(f"Enabled image model {item['id']} requires a size for each aspect ratio")
                if any(
                    not isinstance(size, str)
                    or not re.fullmatch(r"[1-9][0-9]*x[1-9][0-9]*", size)
                    for size in size_map.values()
                ):
                    raise RuntimeConfigError(f"Enabled image model {item['id']} has an invalid size mapping")
                constraints = item.get("size_constraints")
                if not isinstance(constraints, dict):
                    raise RuntimeConfigError(f"Enabled image model {item['id']} requires size_constraints")
                for field in ("multiple_of", "min_pixels", "max_pixels", "max_edge"):
                    if not isinstance(constraints.get(field), int) or constraints[field] <= 0:
                        raise RuntimeConfigError(f"Enabled image model {item['id']} size constraint {field} must be positive")
                if constraints["min_pixels"] > constraints["max_pixels"]:
                    raise RuntimeConfigError(f"Enabled image model {item['id']} has inverted pixel limits")
                if not isinstance(constraints.get("max_edge_inclusive"), bool):
                    raise RuntimeConfigError(f"Enabled image model {item['id']} max_edge_inclusive must be boolean")
                max_ratio = constraints.get("max_aspect_ratio")
                if not isinstance(max_ratio, str) or not re.fullmatch(r"[1-9][0-9]*:[1-9][0-9]*", max_ratio):
                    raise RuntimeConfigError(f"Enabled image model {item['id']} has invalid max_aspect_ratio")
                max_ratio_width, max_ratio_height = (int(part) for part in max_ratio.split(":"))
                if max_ratio_width < max_ratio_height:
                    raise RuntimeConfigError(f"Enabled image model {item['id']} max_aspect_ratio must be at least 1:1")
                for ratio, size in size_map.items():
                    if not isinstance(ratio, str) or not re.fullmatch(r"[1-9][0-9]*:[1-9][0-9]*", ratio):
                        raise RuntimeConfigError(f"Enabled image model {item['id']} has an invalid aspect ratio")
                    width, height = (int(part) for part in size.split("x"))
                    ratio_width, ratio_height = (int(part) for part in ratio.split(":"))
                    edge = max(width, height)
                    edge_allowed = edge <= constraints["max_edge"] if constraints["max_edge_inclusive"] else edge < constraints["max_edge"]
                    if (
                        width % constraints["multiple_of"] or height % constraints["multiple_of"]
                        or not edge_allowed
                        or not constraints["min_pixels"] <= width * height <= constraints["max_pixels"]
                        or max(width, height) * max_ratio_height > min(width, height) * max_ratio_width
                    ):
                        raise RuntimeConfigError(f"Enabled image model {item['id']} has unsupported dimensions for {ratio}")
                    if width * ratio_height != height * ratio_width:
                        raise RuntimeConfigError(f"Enabled image model {item['id']} size does not match aspect ratio {ratio}")
            if "partial_streaming" in item:
                stream = item["partial_streaming"]
                if (kind != "image" or item["id"] != "gpt-image-2.5-flare"
                    or item.get("api_protocol") != "openai_images_v1"
                    or not isinstance(stream, dict)
                    or set(stream) != {"enabled", "partial_images"}
                    or type(stream.get("enabled")) is not bool
                    or type(stream.get("partial_images")) is not int
                    or not 0 <= stream["partial_images"] <= 3):
                    raise RuntimeConfigError("Invalid Flare partial_streaming capability")
                if stream["enabled"] and not all(item[flag] for flag in ("enabled", "available", "verified")):
                    raise RuntimeConfigError("Partial streaming requires enabled, available, verified Flare")
            if kind == "image":
                protocol = item.get("api_protocol")
                if protocol not in ("transit_images_legacy", "openai_images_v1", "ohmygpt_images_polling"):
                    raise RuntimeConfigError(f"Image model {item['id']} requires a supported api_protocol")
                if protocol == "openai_images_v1" and any(
                    {"response_format", "input_fidelity"}.intersection(parameters)
                    for parameters in request_parameters.values()
                ):
                    raise RuntimeConfigError(
                        f"Image model {item['id']} uses parameters unsupported by the OpenAI Images API"
                    )
    for kind, key in (("chat", "chat_model"), ("image", "image_model")):
        if not isinstance(defaults.get(key), str):
            raise RuntimeConfigError(f"Runtime config requires defaults.{key}")
        if not any(item["id"] == defaults[key] for item in models[kind]):
            raise RuntimeConfigError(f"Default {kind} model is not declared")
        default_model = next(item for item in models[kind] if item["id"] == defaults[key])
        if not default_model["enabled"] or not default_model["available"]:
            raise RuntimeConfigError(f"Default {kind} model must be enabled and available")
    default_image = next(item for item in models["image"] if item["id"] == defaults["image_model"])
    if defaults.get("quality") not in default_image.get("qualities", []):
        raise RuntimeConfigError("Default quality is not supported by the default image model")
    if defaults.get("aspect_ratio") not in default_image.get("aspect_ratios", []):
        raise RuntimeConfigError("Default aspect ratio is not supported by the default image model")
    default_chat = next(item for item in models["chat"] if item["id"] == defaults["chat_model"])
    if defaults.get("thinking_mode") not in default_chat.get("thinking_modes", []):
        raise RuntimeConfigError("Default thinking mode is not supported by the default chat model")
    if not isinstance(config.get("request"), dict) or not isinstance(config.get("storage_policies"), dict):
        raise RuntimeConfigError("Runtime config requires request and storage_policies objects")
    jobs = config.get("image_jobs")
    synchronous_policy = defaults.get("synchronous_storage_policy")
    if synchronous_policy != "cos" or not config["storage_policies"].get(synchronous_policy, {}).get("enabled"):
        raise RuntimeConfigError("Synchronous generation requires an enabled COS storage policy")
    if not isinstance(jobs, dict):
        raise RuntimeConfigError("Runtime config requires image_jobs object")
    for key in ("workers", "queue_capacity", "active_jobs_limit", "max_prompt_chars", "poll_timeout_seconds", "storage_workers", "storage_queue_capacity", "result_max_bytes", "result_retention_seconds", "storage_timeout_seconds"):
        if not isinstance(jobs.get(key), int) or jobs[key] <= 0:
            raise RuntimeConfigError(f"Runtime config image_jobs.{key} must be a positive integer")
    request = config["request"]
    for key in ("timeout_seconds", "total_budget_seconds", "reference_image_timeout_seconds", "chat_timeout_seconds", "chat_total_budget_seconds"):
        if not isinstance(request.get(key), (int, float)) or request[key] <= 0:
            raise RuntimeConfigError(f"Runtime config request.{key} must be positive")
    for key in ("reference_image_max_bytes", "reference_image_max_pixels", "reference_image_max_edge", "task_poll_interval_ms", "chat_max_input_chars", "chat_max_output_chars", "chat_max_sse_event_bytes"):
        if not isinstance(request.get(key), int) or request[key] <= 0:
            raise RuntimeConfigError(f"Runtime config request.{key} must be a positive integer")
    if not isinstance(request.get("max_retries"), int) or request["max_retries"] < 0:
        raise RuntimeConfigError("Runtime config request.max_retries must be a non-negative integer")
    if not isinstance(request.get("retry_transport_errors"), bool):
        raise RuntimeConfigError("Runtime config request.retry_transport_errors must be boolean")
    backoff = request.get("retry_backoff_seconds")
    if not isinstance(backoff, list) or len(backoff) < request["max_retries"] or not all(
        isinstance(delay, (int, float)) and delay >= 0 for delay in backoff
    ):
        raise RuntimeConfigError("Runtime config request.retry_backoff_seconds must cover all retries")
    if not isinstance(request.get("retry_http_statuses"), list) or not all(
        isinstance(code, int) for code in request["retry_http_statuses"]
    ):
        raise RuntimeConfigError("Runtime config request.retry_http_statuses must be an integer list")
    for policy_name, policy in config["storage_policies"].items():
        if not isinstance(policy, dict) or not isinstance(policy.get("enabled"), bool):
            raise RuntimeConfigError(f"Storage policy {policy_name} requires an enabled flag")
    default_storage = defaults.get("storage_policy")
    if default_storage not in config["storage_policies"] or not config["storage_policies"][default_storage]["enabled"]:
        raise RuntimeConfigError("Default storage policy must be declared and enabled")
    reference_image = config.get("reference_image")
    if not isinstance(reference_image, dict) or reference_image.get("preserve_frame") is not True:
        raise RuntimeConfigError("Runtime config reference_image.preserve_frame must be true")
    if not isinstance(reference_image.get("max_edge"), int) or reference_image["max_edge"] <= 0:
        raise RuntimeConfigError("Runtime config reference_image.max_edge must be a positive integer")


@lru_cache(maxsize=1)
def get_runtime_config() -> dict[str, Any]:
    return load_runtime_config(os.environ.get("MEDIA_RUNTIME_CONFIG", str(DEFAULT_CONFIG_PATH)))


def get_model(model_id: str | None, kind: str, *, require_available: bool = True) -> dict[str, Any]:
    if kind not in ("chat", "image"):
        raise RuntimeConfigError(f"Unknown model kind: {kind}")
    config = get_runtime_config()
    selected_id = model_id or config["defaults"][f"{kind}_model"]
    model = next((entry for entry in config["models"][kind] if entry["id"] == selected_id), None)
    if model is None:
        raise RuntimeConfigError(f"Unknown {kind} model: {selected_id}")
    if not model["enabled"] or (require_available and not model["available"]):
        raise RuntimeConfigError(f"{kind.capitalize()} model is not available: {selected_id}")
    return model


def get_provider(provider_id: str) -> dict[str, Any]:
    config = get_runtime_config()
    provider = config["providers"].get(provider_id)
    if provider is None:
        raise RuntimeConfigError(f"Unknown provider: {provider_id}")
    return provider


def public_capabilities() -> dict[str, Any]:
    config = get_runtime_config()
    safe_fields = {
        "id", "label", "enabled", "available", "verified", "availability_reason",
        "qualities", "aspect_ratios", "size_by_aspect_ratio", "size_constraints",
        "thinking_modes", "supports_vision", "capabilities_documented",
    }
    return {
        "chat_models": [{key: value for key, value in model.items() if key in safe_fields}
                         for model in config["models"]["chat"]],
        "image_models": [{key: value for key, value in model.items() if key in safe_fields}
                          for model in config["models"]["image"]],
        "defaults": dict(config["defaults"]),
        "option_labels": {
            group: dict(labels)
            for group, labels in config.get("option_labels", {}).items()
        },
        "storage_policies": {
            name: {"enabled": policy["enabled"]}
            for name, policy in config["storage_policies"].items()
        },
        "task_poll_interval_ms": config["request"]["task_poll_interval_ms"],
    }
