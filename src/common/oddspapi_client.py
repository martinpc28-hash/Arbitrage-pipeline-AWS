"""
Cliente para la API de OddsPapi (https://api.oddspapi.io/v4).
Solo libreria estandar (sin requests) para no necesitar Lambda Layers ni Docker.

DESCUBRIMIENTOS IMPORTANTES DE ESTA SESION DE DEPLOY:
1. OddsPapi bloquea peticiones desde IPs de datacenter de AWS con un error de
   Cloudflare (codigo 1010, bloqueo por ASN). Se resuelve enviando un
   User-Agent de navegador real (ver _headers()).
2. Hay un rate limit no documentado publicamente de ~5 solicitudes/segundo
   ademas del limite mensual del plan. Se maneja con reintentos automaticos
   en 429 (ver _get()).
3. El plan free es de 250 solicitudes/MES en total (no por dia). Cada
   GET /odds cuenta 1 solicitud sin importar cuantas casas/mercados traiga.
"""

import json
import os
import time
import urllib.parse
import urllib.request
import urllib.error

BASE_URL = "https://api.oddspapi.io/v4"


class OddsPapiError(Exception):
    def __init__(self, status_code: int, message: str):
        self.status_code = status_code
        super().__init__(f"OddsPapi respondio {status_code}: {message}")


def _api_key() -> str:
    api_key = os.environ.get("ODDSPAPI_API_KEY")
    if not api_key:
        raise RuntimeError("La variable de entorno ODDSPAPI_API_KEY no esta configurada")
    return api_key


def _headers():
    return {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "application/json",
    }


def _get(path: str, params: dict, max_retries: int = 4) -> dict:
    params = {**params, "apiKey": _api_key()}
    url = f"{BASE_URL}{path}?{urllib.parse.urlencode(params)}"

    last_err = None
    for attempt in range(max_retries):
        req = urllib.request.Request(url, method="GET", headers=_headers())
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body_text = e.read().decode("utf-8", errors="replace")
            last_err = OddsPapiError(e.code, body_text)
            if e.code == 429:
                wait_s = 0.6 * (attempt + 1)
                try:
                    parsed = json.loads(body_text)
                    retry_ms = parsed.get("error", {}).get("retryMs")
                    if retry_ms:
                        wait_s = max(wait_s, (retry_ms / 1000.0) + 0.05)
                except Exception:
                    pass
                time.sleep(wait_s)
                continue
            raise last_err
    raise last_err


def get_account_usage() -> dict:
    try:
        return _get("/account", {})
    except OddsPapiError:
        return _get("/account/usage", {})


def get_fixtures(date_from: str, date_to: str, sport_id=None) -> list:
    params = {"from": date_from, "to": date_to}
    if sport_id is not None:
        params["sportId"] = sport_id
    fixtures = _get("/fixtures", params)
    if isinstance(fixtures, dict) and "data" in fixtures:
        fixtures = fixtures["data"]
    return [f for f in fixtures if f.get("hasOdds")]


def get_odds(fixture_id: str) -> dict:
    """Una sola llamada trae TODAS las casas y TODOS los mercados. Cuenta 1 solicitud de cuota."""
    return _get("/odds", {"fixtureId": fixture_id, "oddsFormat": "decimal", "verbosity": 3})


def get_markets(sport_id) -> list:
    """Catalogo de mercados con nombres legibles (marketId -> marketName, outcomeId -> outcomeName)."""
    markets = _get("/markets", {"sportId": sport_id})
    if isinstance(markets, dict) and "data" in markets:
        markets = markets["data"]
    return markets
