"""Colas SQS de LocalStack: contadores y DLQ. SOLO lectura: jamás se recibe ni se borra un mensaje (alteraría lo observado)."""
import asyncio
import json

import boto3
from botocore.config import Config

# Tiempos cortos: sin esto boto3 espera 60 s y reintenta, y una dependencia colgada deja la página sin responder.
_CONFIG = Config(connect_timeout=2, read_timeout=4, retries={"max_attempts": 1})


def _client():
    return boto3.client("sqs", config=_CONFIG)          # respeta AWS_ENDPOINT_URL / AWS_DEFAULT_REGION


def _listar() -> dict:
    sqs = _client()
    urls = sqs.list_queues().get("QueueUrls", [])
    colas, destinos = [], set()
    for url in urls:
        attrs = sqs.get_queue_attributes(QueueUrl=url, AttributeNames=["All"])["Attributes"]
        dlq = None
        if attrs.get("RedrivePolicy"):
            dlq = json.loads(attrs["RedrivePolicy"])["deadLetterTargetArn"].rsplit(":", 1)[-1]
            destinos.add(dlq)
        colas.append({
            "name": url.rsplit("/", 1)[-1],
            "visible": int(attrs.get("ApproximateNumberOfMessages", 0)),
            "in_flight": int(attrs.get("ApproximateNumberOfMessagesNotVisible", 0)),
            "delayed": int(attrs.get("ApproximateNumberOfMessagesDelayed", 0)),
            "dlq": dlq,
            "is_dlq": False,
        })
    for cola in colas:
        cola["is_dlq"] = cola["name"] in destinos
    return {"queues": colas}


async def list_queues() -> dict:
    return await asyncio.to_thread(_listar)
