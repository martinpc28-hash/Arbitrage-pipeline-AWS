"""
Último paso del Step Function, después del Map. Recibe la lista de
resultados de cada rama (uno por fixture) y marca el job como DONE.
"""

import os
from datetime import datetime, timezone

import boto3

dynamodb = boto3.resource("dynamodb")
jobs_table = dynamodb.Table(os.environ["JOBS_TABLE_NAME"])


def handler(event, context):
    job_id = event["jobId"]
    map_results = event["mapResults"]  # lista de outputs de fetch_odds_detect_arb

    total_fixtures = len(map_results)
    fixtures_con_arbitraje = sum(1 for r in map_results if r.get("opportunitiesFound", 0) > 0)
    fixtures_con_error = sum(1 for r in map_results if r.get("error"))
    desde_cache = sum(1 for r in map_results if r.get("fromCache"))

    jobs_table.update_item(
        Key={"jobId": job_id},
        UpdateExpression=(
            "SET #s = :done, finishedAt = :now, "
            "fixturesProcessed = :total, fixturesWithArbitrage = :arb, "
            "fixturesFromCache = :cache, fixturesWithError = :err"
        ),
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={
            ":done": "DONE",
            ":now": datetime.now(timezone.utc).isoformat(),
            ":total": total_fixtures,
            ":arb": fixtures_con_arbitraje,
            ":cache": desde_cache,
            ":err": fixtures_con_error,
        },
    )

    return {
        "jobId": job_id,
        "status": "DONE",
        "fixturesProcessed": total_fixtures,
        "fixturesWithArbitrage": fixtures_con_arbitraje,
    }
