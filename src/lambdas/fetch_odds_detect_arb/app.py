"""
Invocada por el estado Map de Step Functions, una vez por cada fixtureId.

1. Cache de 10 min para no re-gastar cuota si se repite la busqueda.
2. Agrupa cuotas por mercado, se queda SOLO con mercados de exactamente 2
   resultados (evita falsos positivos de mercados con 3+ resultados donde
   solo se detectaron cuotas de 2 casas para 2 de esos resultados).
3. Enriquece los nombres de mercado/resultado con el catalogo GLOBAL de OddsPapi
   (GET /markets no filtra por deporte), cacheado 24h en varios items de DynamoDB
   (el catalogo completo supera los 400KB por item).
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


CATALOG_CACHE_KEY = "catalog:all"
CATALOG_CHUNK_MAX_CHARS = 300000  # margen bajo el limite de 400KB por item de DynamoDB


def _catalogo_nombres():
    """Catalogo GLOBAL de mercados de OddsPapi (marketId -> marketName, outcomeId ->
    outcomeName). GET /markets no filtra por deporte (ver oddspapi_client.get_markets),
    asi que siempre trae el catalogo entero de todos los deportes, y por eso hay un
    solo cache global (no uno por deporte).

    El catalogo completo supera los 400KB (limite por item de DynamoDB), asi que se
    guarda partido en varios items ("chunks") mas un item "meta" con la cuenta de
    chunks. Antes esto se guardaba en un solo item y el put_item fallaba siempre
    (ValidationException: Item size has exceeded the maximum allowed size), lo que
    hacia que el catalogo se re-descargara de OddsPapi en CADA fixture sin nunca
    quedar cacheado.
    """
    try:
        meta = cache_table.get_item(Key={"fixtureId": CATALOG_CACHE_KEY + ":meta"}).get("Item")
    except Exception:
        meta = None

    if meta and (time.time() - float(meta["fetchedAt"])) < CATALOG_TTL_SECONDS:
        try:
            chunks = []
            for i in range(int(meta["chunkCount"])):
                item = cache_table.get_item(Key={"fixtureId": _catalog_chunk_key(i)}).get("Item")
                if not item:
                    raise ValueError("falta el chunk " + str(i))
                chunks.append(item["data"])
            data = json.loads("".join(chunks))
            return data["marketNames"], data["outcomeNames"]
        except Exception as read_err:
            print("Cache de catalogo incompleto, se vuelve a descargar: " + str(read_err))

    try:
        markets = get_markets()
    except OddsPapiError as e:
        print("No se pudo obtener el catalogo de OddsPapi: " + str(e))
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
        payload = json.dumps({"marketNames": market_names, "outcomeNames": outcome_names}, ensure_ascii=False)
        chunk_texts = [payload[i:i + CATALOG_CHUNK_MAX_CHARS] for i in range(0, len(payload), CATALOG_CHUNK_MAX_CHARS)] or [""]
        now = int(time.time())
        expires_at = now + CATALOG_TTL_SECONDS * 2
        for i, chunk in enumerate(chunk_texts):
            cache_table.put_item(Item={
                "fixtureId": _catalog_chunk_key(i), "fetchedAt": now,
                "data": chunk, "expiresAt": expires_at,
            })
        cache_table.put_item(Item={
            "fixtureId": CATALOG_CACHE_KEY + ":meta", "fetchedAt": now,
            "chunkCount": len(chunk_texts), "expiresAt": expires_at,
        })
    except Exception as cache_err:
        print("No se pudo cachear el catalogo: " + str(cache_err))

    return market_names, outcome_names


def _catalog_chunk_key(i):
    return CATALOG_CACHE_KEY + ":chunk:" + str(i)


def _enriquecer_nombres(evaluados, market_names, outcome_names, participant1_name, participant2_name):
    # El catalogo de OddsPapi usa la convencion "1X2" para mercados de 2/3 resultados
    # (ganador, handicap, etc.): el outcomeName es literalmente "1", "2" o "X", no el
    # nombre del equipo. CONFIRMADO CONTRA DATOS REALES: el outcomeId mas bajo del par
    # siempre corresponde a "1" (= participant1Name) y el mas alto a "2" (=
    # participant2Name). Se traduce aqui para que el frontend muestre el equipo real
    # en vez de un "1"/"2" ambiguo.
    equipo_por_posicion = {"1": participant1_name, "2": participant2_name, "X": "Empate"}
    for e in evaluados:
        base_market_id = e["marketKey"].split("::")[0]
        if not e.get("marketLabel"):
            e["marketLabel"] = market_names.get(base_market_id, "Mercado " + base_market_id)
        for leg in e["legs"]:
            if not leg.get("outcomeLabel"):
                base_outcome_id = str(leg["outcomeId"]).split(":")[0]
                leg["outcomeLabel"] = outcome_names.get(base_outcome_id, "Resultado " + str(leg["outcomeId"]))
            equipo = equipo_por_posicion.get(leg["outcomeLabel"])
            if equipo:
                leg["outcomeLabel"] = equipo
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

    if evaluados:
        market_names, outcome_names = _catalogo_nombres()
        evaluados = _enriquecer_nombres(
            evaluados, market_names, outcome_names,
            odds_response.get("participant1Name"), odds_response.get("participant2Name"),
        )

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
