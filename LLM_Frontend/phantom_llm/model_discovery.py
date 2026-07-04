"""Dynamic model discovery for Anthropic-compatible LLM endpoints."""
from __future__ import annotations
import asyncio, time, logging
import aiohttp

log = logging.getLogger("tgclaude.modeldisc")


def _parse_models_response(payload) -> list[str]:
    """Extract model id strings from various response shapes."""
    raw = None
    if isinstance(payload, dict):
        raw = payload.get("data") or payload.get("models")
    if raw is None:
        raw = payload if isinstance(payload, list) else []
    ids: list[str] = []
    for item in raw:
        if isinstance(item, dict):
            mid = item.get("id") or item.get("name")
        elif isinstance(item, str):
            mid = item
        else:
            mid = None
        if mid:
            ids.append(mid)
    # dedupe preserving discovery order, then sort: claude-* first (alpha), rest alpha
    seen: set[str] = set()
    unique: list[str] = []
    for m in ids:
        if m not in seen:
            seen.add(m)
            unique.append(m)
    claude = sorted(m for m in unique if m.startswith("claude-"))
    others = sorted(m for m in unique if not m.startswith("claude-"))
    return claude + others


async def probe_models(provider, timeout: float = 12.0) -> tuple[list[str], str | None]:
    """Fetch model list from provider's /v1/models endpoint.

    Returns (sorted_model_ids, error_or_None).
    """
    url = provider.base_url.rstrip("/") + "/v1/models"
    headers = {
        "x-api-key": provider.auth_token,
        "Authorization": f"Bearer {provider.auth_token}",
    }
    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=timeout)
        ) as session:
            async with session.get(url, headers=headers) as resp:
                if resp.status != 200:
                    text = (await resp.text())[:200]
                    return [], f"HTTP {resp.status}: {text}"
                payload = await resp.json(content_type=None)
                models = _parse_models_response(payload)
                return models, None
    except asyncio.TimeoutError:
        return [], "timeout"
    except Exception as exc:
        return [], f"{type(exc).__name__}: {exc}"


class ModelCache:
    """In-memory model list cache keyed by provider name. TTL-based expiry."""

    TTL = 300.0
    REFRESH_TIMEOUT = 12

    def __init__(self):
        self._cache: dict[str, dict] = {}
        self._inflight: dict[str, asyncio.Task] = {}

    def get(self, provider_name: str) -> dict | None:
        entry = self._cache.get(provider_name)
        if entry is None:
            return None
        fresh = (time.time() - entry["fetched"]) < self.TTL
        return {**entry, "fresh": fresh}

    def should_refresh(self, provider_name: str) -> bool:
        entry = self._cache.get(provider_name)
        if entry is None:
            return True
        return (time.time() - entry["fetched"]) >= self.TTL

    async def refresh(self, provider) -> dict:
        name = getattr(provider, "name", provider.base_url)
        if name in self._inflight:
            await self._inflight[name]
            return self.get(name)  # type: ignore

        async def _do():
            try:
                models, error = await probe_models(provider, timeout=self.REFRESH_TIMEOUT)
                self._cache[name] = {
                    "models": models,
                    "fetched": time.time(),
                    "error": error,
                }
            finally:
                self._inflight.pop(name, None)

        task = asyncio.ensure_future(_do())
        self._inflight[name] = task
        await task
        return self.get(name)  # type: ignore

    def fallback_models(self, provider) -> list[str]:
        models_dict = getattr(provider, "models", None) or {}
        seen: set[str] = set()
        result: list[str] = []
        for v in models_dict.values():
            if v and v not in seen:
                seen.add(v)
                result.append(v)
        return result

    def list_for_menu(self, provider) -> list[str]:
        name = getattr(provider, "name", provider.base_url)
        entry = self.get(name)
        if entry and entry["fresh"] and entry["models"]:
            return entry["models"]
        fb = self.fallback_models(provider)
        if entry and entry["models"] and not entry.get("error"):
            return entry["models"]
        return fb if fb else (entry["models"] if entry and entry["models"] else ["claude-sonnet-4-20250514"])


if __name__ == "__main__":
    # Smoke test: verify parsing logic with mock payloads
    # Shape 1: OpenAI-style {"data": [...]}
    p1 = {"object": "list", "data": [
        {"id": "claude-opus-4-20250514", "object": "model"},
        {"id": "claude-sonnet-4-20250514", "object": "model"},
        {"id": "gpt-4o", "object": "model"},
    ]}
    r1 = _parse_models_response(p1)
    assert r1 == ["claude-opus-4-20250514", "claude-sonnet-4-20250514", "gpt-4o"], f"FAIL shape1: {r1}"

    # Shape 2: {"models": [...]} with plain strings
    p2 = {"models": ["claude-haiku-3", "mistral-large", "claude-opus-4-20250514"]}
    r2 = _parse_models_response(p2)
    assert r2 == ["claude-haiku-3", "claude-opus-4-20250514", "mistral-large"], f"FAIL shape2: {r2}"

    # Shape 3: bare list with dicts having "name" key + duplicates
    p3 = [{"name": "claude-sonnet-4-20250514"}, {"name": "deepseek-r1"}, {"name": "claude-sonnet-4-20250514"}]
    r3 = _parse_models_response(p3)
    assert r3 == ["claude-sonnet-4-20250514", "deepseek-r1"], f"FAIL shape3: {r3}"

    # ModelCache fallback test
    class FakeProvider:
        base_url = "http://localhost"
        auth_token = "test"
        name = "fake"
        models = {"opus": "claude-opus-4-20250514", "sonnet": "claude-sonnet-4-20250514"}

    mc = ModelCache()
    assert mc.should_refresh("fake") is True
    fb = mc.fallback_models(FakeProvider())
    assert "claude-opus-4-20250514" in fb and "claude-sonnet-4-20250514" in fb
    menu = mc.list_for_menu(FakeProvider())
    assert len(menu) >= 1  # fallback used since no cache

    print("ALL SMOKE TESTS PASSED")
    print(f"  shape1 -> {r1}")
    print(f"  shape2 -> {r2}")
    print(f"  shape3 -> {r3}")
    print(f"  fallback -> {fb}")
    print(f"  menu (no cache) -> {menu}")
