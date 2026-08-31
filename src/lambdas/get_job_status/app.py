"""
GET /jobs/{jobId}

El frontend hace polling hasta status=DONE. Cuando termina, devuelve:
- opportunities: solo los mercados donde SI hay arbitraje (<=30% ganancia)
- scannedFixtures: TODOS los partidos escaneados, con el mercado mas
  cercano a arbitraje de cada uno, para mostrar el razonamiento completo
  aunque no haya arbitraje real.
"""

import json
import os

import boto3
from boto3.dynamodb.conditions import Key

dynamodb = boto3.resource("dynamodb")
jobs_table = dynamodb.Table(os.environ["JOBS_TABLE_NAME"])
results_table = dynamodb.Table(os.environ["RESULTS_TABLE_NAME"])


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

        opportunities = []
        scanned = []
        for item in items:
            fixture_label = (item.get("participant1Name") or "?") + " vs " + (item.get("participant2Name") or "?")
            scanned.append({
                "fixtureId": item["fixtureId"],
                "label": fixture_label,
                "tournamentName": item.get("tournamentName"),
                "sportName": item.get("sportName"),
                "startTime": item.get("startTime"),
                "marketsAnalyzed": item.get("marketsAnalyzed"),
                "bestMarket": item.get("bestMarket"),
            })
            for op in (item.get("arbitrageOpportunities") or []):
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
