"""
POST /jobs/estimate

1. Lista fixtures de hoy/manana (hasOdds=true) desde OddsPapi.
2. Consulta cuota restante de la cuenta (GET /account, no descuenta cuota).
3. Crea un job en PENDING_CONFIRMATION con la lista de fixtureIds.
4. Devuelve al frontend cuantas solicitudes consumiria confirmar, para que
   el usuario decida antes de gastar cuota real.
"""

import json
import os
import uuid
from datetime import datetime, timedelta, timezone

import boto3

from common.oddspapi_client import get_fixtures, get_account_usage, OddsPapiError

dynamodb = boto3.resource("dynamodb")
jobs_table = dynamodb.Table(os.environ["JOBS_TABLE_NAME"])


def handler(event, context):
    body = json.loads(event.get("body") or "{}")
    sport_id = body.get("sportId")

    hoy = datetime.now(timezone.utc).date()
    manana = hoy + timedelta(days=1)

    try:
        fixtures = get_fixtures(hoy.isoformat(), manana.isoformat(), sport_id=sport_id)
    except OddsPapiError as e:
        return _r(502, {"error": str(e)})

    try:
        usage = get_account_usage()
    except OddsPapiError as e:
        usage = {"error": str(e)}

    fixture_ids = [f["fixtureId"] for f in fixtures]
    job_id = str(uuid.uuid4())

    jobs_table.put_item(Item={
        "jobId": job_id,
        "status": "PENDING_CONFIRMATION",
        "createdAt": datetime.now(timezone.utc).isoformat(),
        "sportIdFilter": sport_id,
        "fixtureIds": fixture_ids,
        "estimatedRequests": len(fixture_ids),
    })

    return _r(200, {
        "jobId": job_id,
        "estimatedRequests": len(fixture_ids),
        "fixturesPreview": [
            {"fixtureId": f["fixtureId"], "tournamentId": f.get("tournamentId"), "startTime": f.get("startTime")}
            for f in fixtures[:20]
        ],
        "accountUsage": usage,
    })


def _r(status_code, body):
    return {"statusCode": status_code,
            "headers": {"Content-Type": "application/json", "Access-Control-Allow-Origin": "*"},
            "body": json.dumps(body, ensure_ascii=False)}
