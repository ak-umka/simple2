"""Тонкий клиент для Keitaro Admin API v1."""
import time
from typing import Any, Dict, List, Optional

import httpx

from app.config import settings


class KeitaroError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


class KeitaroClient:
    OFFERS_TTL = 300  # секунд кэша для списка офферов

    def __init__(self, base_url: str, api_key: str, transport: Optional[httpx.BaseTransport] = None):
        self._http = httpx.Client(
            base_url=f"{base_url}/admin_api/v1",
            headers={"Api-Key": api_key},
            timeout=30,
            transport=transport,
        )
        self._offers_cache: Optional[List[Dict[str, Any]]] = None
        self._offers_cached_at = 0.0

    def _request(self, method: str, path: str, **kwargs) -> Any:
        try:
            resp = self._http.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise KeitaroError(502, f"Keitaro недоступен: {exc}") from exc
        if resp.status_code >= 400:
            raise KeitaroError(resp.status_code, f"Keitaro {method} {path}: {resp.text[:500]}")
        if not resp.content:
            return None
        return resp.json()

    # --- справочники
    def domains(self) -> List[Dict[str, Any]]:
        return self._request("GET", "/domains")

    def campaign_groups(self) -> List[Dict[str, Any]]:
        return self._request("GET", "/groups", params={"type": "campaigns"})

    def traffic_sources(self) -> List[Dict[str, Any]]:
        return self._request("GET", "/traffic_sources")

    def offers(self, force: bool = False) -> List[Dict[str, Any]]:
        if force or self._offers_cache is None or time.time() - self._offers_cached_at > self.OFFERS_TTL:
            self._offers_cache = self._request("GET", "/offers")
            self._offers_cached_at = time.time()
        return self._offers_cache

    def offer_names(self) -> Dict[int, str]:
        return {o["id"]: o.get("name", "") for o in self.offers()}

    # --- кампании и потоки
    def campaigns(self) -> List[Dict[str, Any]]:
        return self._request("GET", "/campaigns")

    def campaign(self, campaign_id: int) -> Dict[str, Any]:
        return self._request("GET", f"/campaigns/{campaign_id}")

    def create_campaign(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return self._request("POST", "/campaigns", json=payload)

    def streams(self, campaign_id: int) -> List[Dict[str, Any]]:
        return self._request("GET", f"/campaigns/{campaign_id}/streams")

    def stream(self, stream_id: int) -> Dict[str, Any]:
        return self._request("GET", f"/streams/{stream_id}")

    def create_stream(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return self._request("POST", "/streams", json=payload)

    def update_stream(self, stream_id: int, payload: Dict[str, Any]) -> Dict[str, Any]:
        return self._request("PUT", f"/streams/{stream_id}", json=payload)


_client: Optional[KeitaroClient] = None


def get_keitaro() -> KeitaroClient:
    global _client
    if _client is None:
        _client = KeitaroClient(settings.keitaro_url, settings.keitaro_api_key)
    return _client
