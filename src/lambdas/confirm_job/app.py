"""
POST /jobs/{jobId}/confirm

El usuario ya vio cuanto costaria (estimate_job) y confirma que quiere
seguir. Arranca la ejecucion de Step Functions que hace el fan-out real.
"""

import json
import os
from datetime import datetime, timezone

import boto3

dynamodb = boto3.resource("dynamodb")
jobs_table = dynamodb.Table(os.environ["JOBS_TABLE_NAME"])
sfn = boto3.client("stepfunctions")

STATE_MACHINE_ARN = os.environ["STATE_MACHINE_ARN"]


def handler(event, context):
    job_id = event["pathParameters"]["jobId"]

    item = jobs_table.get_item(Key={"jobId": job_id}).get("Item")
    if not item:
        return _r(404, {"error": "job no encontrado"})

    if item["status"] != "PENDING_CONFIRMATION":
        return _r(409, {"error": "el job ya esta en estado " + item["status"]})

    fixture_ids = item["fixtureIds"]

    sfn.start_execution(
        stateMachineArn=STATE_MACHINE_ARN,
        name="oddsarb-" + job_id,
        input=json.dumps({"jobId": job_id, "fixtureIds": fixture_ids}),
    )

    jobs_table.update_item(
        Key={"jobId": job_id},
        UpdateExpression="SET #s = :running, startedAt = :now",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":running": "RUNNING", ":now": datetime.now(timezone.utc).isoformat()},
    )

    return _r(200, {"jobId": job_id, "status": "RUNNING"})


def _r(status_code, body):
    return {"statusCode": status_code,
            "headers": {"Content-Type": "application/json", "Access-Control-Allow-Origin": "*"},
            "body": json.dumps(body, ensure_ascii=False)}
