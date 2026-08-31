"""
GET /jobs/{jobId}

El frontend hace polling hasta status=DONE. Cuando termina, devuelve:
- opportunities: solo los mercados donde SI hay arbitraje (<=30% ganancia)
- scannedFixtures: TODOS los partidos escaneados, con el mercado mas
  cercano a arbitraje de cada uno, para mostrar el razonamiento completo
  aunque no haya arbitraje real.

Tambien sirve para ver jobs VIEJOS (pestana Historial del frontend, via
GET /history + este endpoint por cada jobId). Algunos jobs viejos se
guardaron con una version anterior del codigo que no traducia
marketLabel/outcomeLabel ni tenia el tope de 30% de ganancia — por eso
aqui se re-enriquecen y se filtran al leer, sin tocar lo ya guardado en
DynamoDB (ver _catalogo_desde_cache y _enriquecer_opportunity).
"""

import json
import os

import boto3
from boto3.dynamodb.conditions import Key

dynamodb = boto3.resource("dynamodb")
jobs_table = dynamodb.Table(os.environ["JOBS_TABLE_NAME"])
results_table = dynamodb.Table(os.environ["RESULTS_TABLE_NAME"])
cache_table = dynamodb.Table(os.environ["CACHE_TABLE_NAME"])

CATALOG_CACHE_KEY = "catalog:all"
PROFIT_PCT_MAX = 30.0


def _catalogo_desde_cache():
    """Lee el catalogo global ya cacheado por fetch_odds_detect_arb. NO dispara
    una descarga nueva a OddsPapi: si todavia no esta cacheado, devuelve vacio
    y se cae de vuelta al marketKey/outcomeId crudo (igual que antes)."""
    try:
        meta = cache_table.get_item(Key={"fixtureId": CATALOG_CACHE_KEY + ":meta"}).get("Item")
        if not meta:
            return {}, {}
        chunks = []
        for i in range(int(meta["chunkCount"])):
            item = cache_table.get_item(Key={"fixtureId": CATALOG_CACHE_KEY + ":chunk:" + str(i)}).get("Item")
            if not item:
                return {}, {}
            chunks.append(item["data"])
        data = json.loads("".join(chunks))
        return data.get("marketNames", {}), data.get("outcomeNames", {})
    except Exception:
        return {}, {}


def _enriquecer_opportunity(op, market_names, outcome_names, p1_name, p2_name):
    equipo_por_posicion = {"1": p1_name, "2": p2_name, "X": "Empate"}
    if not op.get("marketLabel"):
        base_market_id = str(op.get("marketKey", "")).split("::")[0]
        op["marketLabel"] = market_names.get(base_market_id, op.get("marketKey"))
    for leg in op.get("legs", []):
        if not leg.get("outcomeLabel"):
            base_outcome_id = str(leg.get("outcomeId", "")).split(":")[0]
            leg["outcomeLabel"] = outcome_names.get(base_outcome_id)
        equipo = equipo_por_posicion.get(leg.get("outcomeLabel"))
        if equipo:
            leg["outcomeLabel"] = equipo
    return op


def handler(event, context):
    job_id = event["pathParameters"]["jobId"]

    job = jobs_table.get_item(Key={"jobId": job_id}).get("Item")
    if not job:
        return _r(404, {"error": "job no encontrado"})

    body = {
        "jobId": job_id,
        "status": job["status"],
        "estimatedRequests": job.get("estimatedRequests"),
        "fixturesProcessed": job.get("fixturesProcessed"),
        "fixturesWithArbitrage": job.get("fixturesWithArbitrage"),
        "fixturesFromCache": job.get("fixturesFromCache"),
    }

    if job["status"] == "DONE":
        items = results_table.query(KeyConditionExpression=Key("jobId").eq(job_id)).get("Items", [])
        market_names, outcome_names = _catalogo_desde_cache()

        opportunities = []
        scanned = []
        for item in items:
            p1_name = item.get("participant1Name")
            p2_name = item.get("participant2Name")
            fixture_label = (p1_name or "?") + " vs " + (p2_name or "?")

            best_market = item.get("bestMarket")
            if best_market:
                best_market = _enriquecer_opportunity(dict(best_market), market_names, outcome_names, p1_name, p2_name)

            scanned.append({
                "fixtureId": item["fixtureId"],
                "label": fixture_label,
                "tournamentName": item.get("tournamentName"),
                "sportName": item.get("sportName"),
                "startTime": item.get("startTime"),
                "marketsAnalyzed": item.get("marketsAnalyzed"),
                "bestMarket": best_market,
            })
            for op in (item.get("arbitrageOpportunities") or []):
                # Filtro de seguridad al leer: jobs viejos se guardaron con una
                # version del codigo sin el tope de 30% (ver PROFIT_PCT_MAX en
                # fetch_odds_detect_arb/app.py). Se aplica aqui tambien para
                # que no reaparezcan como "oportunidades" en el Historial.
                if float(op.get("profitPct", 0)) > PROFIT_PCT_MAX:
                    continue
                op = _enriquecer_opportunity(dict(op), market_names, outcome_names, p1_name, p2_name)
                opportunities.append({"fixtureId": item["fixtureId"], "label": fixture_label,
                                       "startTime": item.get("startTime"), **op})

        opportunities.sort(key=lambda o: o["profitPct"], reverse=True)
        scanned.sort(key=lambda s: (s["bestMarket"]["impliedProbabilitySum"] if s.get("bestMarket") else 999))
        body["opportunities"] = opportunities
        body["scannedFixtures"] = scanned

    return _r(200, body)


def _r(status_code, body):
    return {"statusCode": status_code,
            "headers": {"Content-Type": "application/json", "Access-Control-Allow-Origin": "*"},
            "body": json.dumps(body, ensure_ascii=False, default=str)}
