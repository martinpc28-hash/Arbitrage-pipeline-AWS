"""
Invocada por el estado Map de Step Functions, una vez por cada fixtureId.

1. Cache de 10 min para no re-gastar cuota si se repite la busqueda.
2. Agrupa cuotas por mercado, se queda SOLO con mercados de exactamente 2
   resultados (evita falsos positivos de mercados con 3+ resultados donde
   solo se detectaron cuotas de 2 casas para 2 de esos resultados).
3. Enriquece los nombres de mercado/resultado con el catalogo de OddsPapi
   (GET /markets), cacheado 24h por deporte.
4. Descarta como "arbitraje" cualquier oportunidad con mas de 30% de
   ganancia: en la practica casi nunca ocurre, y cuando aparece suele venir
   de cuotas de prueba/erroneas de alguna casa (visto en produccion con
   balkanbet.rs y gamdom devolviendo precios en secuencia sospechosamente
   redonda: 10.0, 9.8, 9.6...).
5. Guarda SIEMPRE un resumen por fixture (haya o no arbitraje), para poder
   mostrar en el frontend todos los partidos escaneados con su razonamiento
   matematico, no solo los que tuvieron arbitraje.
"""

import json
import os
import time
from decimal import Decimal

import boto3

from common.oddspapi_client import get_odds, get_markets, OddsPapiError
from common.oddspapi_parser import agrupar_mejores_cuotas_por_mercado
from common.arbitrage import evaluar_mercado_siempre

dynamodb = boto3.resource("dynamodb")
cache_table = dynamodb.Table(os.environ["CACHE_TABLE_NAME"])
results_table = dynamodb.Table(os.environ["RESULTS_TABLE_NAME"])

CACHE_TTL_SECONDS = int(os.environ.get("CACHE_TTL_SECONDS", "600"))
CATALOG_TTL_SECONDS = 86400
PROFIT_PCT_MAX = 30.0


def _to_decimal(obj):
    """DynamoDB (boto3 resource) no acepta float nativo de Python, solo Decimal."""
    if isinstance(obj, float):
        return Decimal(str(obj))
    if isinstance(obj, dict):
        return {k: _to_decimal(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_to_decimal(v) for v in obj]
    return obj


def _cached_odds(fixture_id):
    try:
        cached = cache_table.get_item(Key={"fixtureId": fixture_id}).get("Item")
    except Exception:
        cached = None

    if cached:
        # DynamoDB devuelve numeros como Decimal: no se puede restar directo con float.
        fetched_at = float(cached["fetchedAt"])
        if (time.time() - fetched_at) < CACHE_TTL_SECONDS:
            return json.loads(cached["oddsData"]), True

    odds_response = get_odds(fixture_id)

    try:
        # Guardar en cache es una optimizacion, no critico: si la respuesta supera
        # el limite de 400KB por item de DynamoDB, no debe tumbar la busqueda.
        cache_table.put_item(Item={
            "fixtureId": fixture_id, "fetchedAt": int(time.time()),
            "oddsData": json.dumps(odds_response, ensure_ascii=False),
            "expiresAt": int(time.time()) + CACHE_TTL_SECONDS * 3,
        })
    except Exception as cache_err:
        print("No se pudo cachear fixture " + fixture_id + ": " + str(cache_err))

    return odds_response, False


def _catalogo_nombres(sport_id):
    cache_key = "catalog:" + str(sport_id)
    try:
        cached = cache_table.get_item(Key={"fixtureId": cache_key}).get("Item")
    except Exception:
        cached = None

    if cached and (time.time() - float(cached["fetchedAt"])) < CATALOG_TTL_SECONDS:
        data = json.loads(cached["oddsData"])
        return data["marketNames"], data["outcomeNames"]

    try:
        markets = get_markets(sport_id)
    except OddsPapiError:
        return {}, {}

    market_names = {}
    outcome_names = {}
    for m in markets:
        if m.get("marketName"):
            market_names[str(m["marketId"])] = m["marketName"]
        for o in m.get("outcomes", []):
            if o.get("outcomeName"):
                outcome_names[str(o["outcomeId"])] = o["outcomeName"]

    try:
        cache_table.put_item(Item={
            "fixtureId": cache_key, "fetchedAt": int(time.time()),
            "oddsData": json.dumps({"marketNames": market_names, "outcomeNames": outcome_names}, ensure_ascii=False),
            "expiresAt": int(time.time()) + CATALOG_TTL_SECONDS * 2,
        })
    except Exception as cache_err:
        print("No se pudo cachear catalogo de deporte " + str(sport_id) + ": " + str(cache_err))

    return market_names, outcome_names


def _enriquecer_nombres(evaluados, market_names, outcome_names):
    for e in evaluados:
        base_market_id = e["marketKey"].split("::")[0]
        if not e.get("marketLabel"):
            e["marketLabel"] = market_names.get(base_market_id, "Mercado " + base_market_id)
        for leg in e["legs"]:
            if not leg.get("outcomeLabel"):
                base_outcome_id = str(leg["outcomeId"]).split(":")[0]
                leg["outcomeLabel"] = outcome_names.get(base_outcome_id, "Resultado " + str(leg["outcomeId"]))
    return evaluados


def handler(event, context):
    job_id = event["jobId"]
    fixture_id = event["fixtureId"]

    try:
        odds_response, from_cache = _cached_odds(fixture_id)
    except OddsPapiError as e:
        return {"jobId": job_id, "fixtureId": fixture_id, "error": str(e), "opportunitiesFound": 0}

    mercados, etiquetas = agrupar_mejores_cuotas_por_mercado(odds_response)

    evaluados = []
    for market_key, outcomes in mercados.items():
        if len(outcomes) != 2:
            continue
        op = evaluar_mercado_siempre(market_key, etiquetas.get(market_key), outcomes)
        if op:
            evaluados.append(op.to_dict())

    sport_id = odds_response.get("sportId")
    if sport_id is not None and evaluados:
        market_names, outcome_names = _catalogo_nombres(sport_id)
        evaluados = _enriquecer_nombres(evaluados, market_names, outcome_names)

    arbitrajes = [e for e in evaluados if e["isArbitrage"] and e["profitPct"] <= PROFIT_PCT_MAX]
    mejor_mercado = min(evaluados, key=lambda e: e["impliedProbabilitySum"]) if evaluados else None

    try:
        results_table.put_item(Item=_to_decimal({
            "jobId": job_id,
            "fixtureId": fixture_id,
            "participant1Name": odds_response.get("participant1Name"),
            "participant2Name": odds_response.get("participant2Name"),
            "tournamentName": odds_response.get("tournamentName"),
            "sportName": odds_response.get("sportName"),
            "startTime": odds_response.get("startTime"),
            "marketsAnalyzed": len(evaluados),
            "bestMarket": mejor_mercado,
            "arbitrageOpportunities": arbitrajes,
        }))
    except Exception as results_err:
        return {"jobId": job_id, "fixtureId": fixture_id,
                "error": "No se pudo guardar resultados: " + str(results_err), "opportunitiesFound": 0}

    return {"jobId": job_id, "fixtureId": fixture_id, "fromCache": from_cache, "opportunitiesFound": len(arbitrajes)}
