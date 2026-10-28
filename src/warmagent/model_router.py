"""Explicit gateway routing with opt-in ProviderError-only compatibility fallback."""

from __future__ import annotations

from pathlib import Path
import tomllib
import re
import json

from myatri.providers import BudgetExceeded, ProviderError


def _has_image(value):
    if isinstance(value, dict):
        if any(key in value for key in ("image_url", "image_manifest", "input_image")):
            return True
        if value.get("role") == "tool" and isinstance(value.get("content"), str):
            try:
                receipt = json.loads(value["content"])
            except (TypeError, ValueError):
                receipt = None
            if isinstance(receipt, dict) and _has_image(receipt):
                return True
        return any(_has_image(child) for key, child in value.items()
                   if not (key == "content" and isinstance(child, str)))
    if isinstance(value, list):
        return any(_has_image(child) for child in value)
    return False


_SECRET_KEYS = {"key", "apikey", "token", "accesstoken", "secret", "clientsecret",
                "credential", "credentials", "password", "authorization", "bearertoken"}


def _factory_spec(value):
    if isinstance(value, dict):
        safe = {}
        for key, child in value.items():
            token = re.sub(r"[_-]", "", str(key)).casefold()
            if token in _SECRET_KEYS or token.endswith("apikey"):
                continue
            safe[key] = _factory_spec(child)
        return safe
    if isinstance(value, list):
        return [_factory_spec(child) for child in value]
    return value


class ModelRouter:
    """Route through existing Gateway instances, preserving their ledgers and response IDs."""
    def __init__(self, base, gateways, config):
        self.base = base
        self.gateways = {"base": base, **dict(gateways or {})}
        raw_routes = config.get("routes", {})
        if not isinstance(raw_routes, dict):
            raise ValueError("model router routes must be a table")
        self.routes, self.failovers = {}, {}
        for name, spec in raw_routes.items():
            if isinstance(spec, str):
                target, fallback = spec, []
            elif isinstance(spec, dict):
                target, fallback = spec.get("gateway", "base"), spec.get("failover", [])
            else:
                raise ValueError(f"invalid route configuration: {name}")
            if target not in self.gateways or any(item not in self.gateways for item in fallback):
                raise ValueError(f"unknown gateway in route: {name}")
            self.routes[name], self.failovers[name] = self.gateways[target], list(fallback)
        self.purpose_routes = config.get("purpose_routes", {})
        if not isinstance(self.purpose_routes, dict):
            raise ValueError("purpose_routes must be a table")
        for route in self.purpose_routes.values():
            if route not in self.routes:
                raise ValueError(f"unknown purpose route: {route}")

    def __getattr__(self, name):
        # Preserve Gateway-facing budget/provider attributes for callers that inspect them.
        base = object.__getattribute__(self, "base")
        return getattr(base, name)

    def _route(self, request):
        if request.purpose in self.purpose_routes:
            return self.purpose_routes[request.purpose]
        if _has_image(request.messages) and "vision" in self.routes:
            return "vision"
        if request.thinking and "reasoning" in self.routes:
            return "reasoning"
        return "default"

    async def generate(self, request):
        route = self._route(request)
        if route not in self.routes:
            return await self.base.generate(request)
        candidates = [self.routes[route], *(self.gateways[name] for name in self.failovers[route])]
        for index, gateway in enumerate(candidates):
            try:
                # The same request object and the actual ModelResponse pass through unchanged.
                return await gateway.generate(request)
            except BudgetExceeded:
                raise
            except ProviderError:
                if index == len(candidates) - 1:
                    raise


def load_model_router(base, path, gateways=None, gateway_factory=None):
    """Missing config is a strict no-op and returns the original Gateway object."""
    config_path = Path(path)
    if not config_path.is_file():
        return base
    with config_path.open("rb") as stream:
        config = tomllib.load(stream)
    section = config.get("model_router", config)
    if not isinstance(section, dict):
        raise ValueError("model_router config must be a table")
    available = dict(gateways or {})
    specs = section.get("gateways", {})
    if not isinstance(specs, dict):
        raise ValueError("model_router gateways must be a table")
    for name, spec in specs.items():
        if name in available:
            continue
        if gateway_factory is None:
            raise ValueError(f"gateway_not_configured:{name}")
        if not isinstance(spec, dict):
            raise ValueError(f"invalid_gateway_spec:{name}")
        try:
            available[name] = gateway_factory(name, _factory_spec(spec))
        except Exception:
            raise ValueError(f"gateway_factory_failed:{name}") from None
    referenced = set()
    for spec in section.get("routes", {}).values():
        if isinstance(spec, str):
            referenced.add(spec)
        elif isinstance(spec, dict):
            referenced.add(spec.get("gateway", "base"))
            referenced.update(spec.get("failover", []))
    missing = sorted(name for name in referenced if name != "base" and name not in available)
    if missing:
        raise ValueError("gateway_not_configured:" + ",".join(missing))
    return ModelRouter(base, available, section)

