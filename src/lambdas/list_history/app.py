"""
GET /history

Lista los jobs de busqueda anteriores (los mas recientes primero), con un
resumen de cada uno, para la pestana "Historial" del frontend. Para ver el
detalle completo de un job (oportunidades + partidos escaneados), el
frontend usa GET /jobs/{jobId} (ya existente) con el jobId devuelto aqui.

Solo se listan jobs con status DONE: los que quedaron a medias
(PENDING_CONFIRMATION, IN_PROGRESS, ERROR) no tienen resultados que ver.
"""

import json
import os

import boto3

dynamodb = boto3.resource("dynamodb")
jobs_table = dynamodb.Table(os.environ["JOBS_TABLE_NAME"])

MAX_JOBS = 50


def handler(event, context):
    items = []
    resp = jobs_table.scan()
    items.extend(resp.get("Items", []))
    while "LastEvaluatedKey" in resp:
        resp = jobs_table.scan(ExclusiveStartKey=resp["LastEvaluatedKey"])
        items.extend(resp.get("Items", []))

    done = [it for it in items if it.get("status") == "DONE"]
    done.sort(key=lambda it: it.get("createdAt") or "", reverse=True)

    jobs = []
    for it in done[:MAX_JOBS]:
        jobs.append({
            "jobId": it["jobId"],
            "createdAt": it.get("createdAt"),
            "finishedAt": it.get("finishedAt"),
            "sportIdFilter": it.get("sportIdFilter"),
            "fixturesProcessed": it.get("fixturesProcessed"),
            "fixturesWithArbitrage": it.get("fixturesWithArbitrage"),
            "fixturesFromCache": it.get("fixturesFromCache"),
        })

    return _r(200, {"jobs": jobs})


def _r(status_code, body):
    return {"statusCode": status_code,
            "headers": {"Content-Type": "application/json", "Access-Control-Allow-Origin": "*"},
            "body": json.dumps(body, ensure_ascii=False, default=str)}
