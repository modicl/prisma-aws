"""Colas SQS: contadores, DLQ a la que apuntan y cuáles son DLQ. Solo lectura."""
import json
from unittest.mock import MagicMock

import pytest

from app import queues

BASE = "http://sqs.us-east-1.localhost.localstack.cloud:4566/000000000000"


def cliente(colas: dict) -> MagicMock:
    """colas: {nombre: atributos}"""
    c = MagicMock()
    c.list_queues.return_value = {"QueueUrls": [f"{BASE}/{n}" for n in colas]}
    c.get_queue_attributes.side_effect = lambda QueueUrl, AttributeNames: {"Attributes": colas[QueueUrl.rsplit("/", 1)[-1]]}
    return c


@pytest.fixture
def con(monkeypatch):
    def _usar(colas):
        c = cliente(colas)
        monkeypatch.setattr(queues, "_client", lambda: c)
        return c
    return _usar


async def test_lista_las_colas_con_sus_contadores(con):
    con({
        "prisma-flujo": {"ApproximateNumberOfMessages": "3", "ApproximateNumberOfMessagesNotVisible": "1",
                         "ApproximateNumberOfMessagesDelayed": "0"},
    })
    res = await queues.list_queues()
    assert res["queues"] == [{"name": "prisma-flujo", "visible": 3, "in_flight": 1, "delayed": 0, "dlq": None, "is_dlq": False}]


async def test_detecta_la_dlq_por_la_redrive_policy(con):
    redrive = json.dumps({"deadLetterTargetArn": "arn:aws:sqs:us-east-1:000000000000:prisma-flujo-dlq", "maxReceiveCount": 3})
    con({
        "prisma-flujo": {"ApproximateNumberOfMessages": "0", "ApproximateNumberOfMessagesNotVisible": "0",
                         "ApproximateNumberOfMessagesDelayed": "0", "RedrivePolicy": redrive},
        "prisma-flujo-dlq": {"ApproximateNumberOfMessages": "2", "ApproximateNumberOfMessagesNotVisible": "0",
                             "ApproximateNumberOfMessagesDelayed": "0"},
    })
    por_nombre = {q["name"]: q for q in (await queues.list_queues())["queues"]}
    assert por_nombre["prisma-flujo"]["dlq"] == "prisma-flujo-dlq" and por_nombre["prisma-flujo"]["is_dlq"] is False
    assert por_nombre["prisma-flujo-dlq"]["is_dlq"] is True and por_nombre["prisma-flujo-dlq"]["visible"] == 2


async def test_sin_colas_devuelve_lista_vacia(monkeypatch):
    c = MagicMock()
    c.list_queues.return_value = {}            # boto3 omite QueueUrls cuando no hay colas
    monkeypatch.setattr(queues, "_client", lambda: c)
    assert await queues.list_queues() == {"queues": []}


async def test_es_solo_lectura(con):
    c = con({"q": {"ApproximateNumberOfMessages": "1", "ApproximateNumberOfMessagesNotVisible": "0",
                   "ApproximateNumberOfMessagesDelayed": "0"}})
    await queues.list_queues()
    for prohibido in ("receive_message", "purge_queue", "delete_message", "send_message", "delete_queue"):
        assert not getattr(c, prohibido).called, prohibido


def test_el_cliente_de_sqs_tiene_tiempos_cortos(monkeypatch):
    """I3: sin esto boto3 espera 60 s y reintenta, y la página se queda colgada."""
    visto = {}
    monkeypatch.setattr(queues.boto3, "client", lambda nombre, **kw: visto.update(nombre=nombre, **kw))
    queues._client()
    assert visto["nombre"] == "sqs" and visto["config"].read_timeout <= 5 and visto["config"].connect_timeout <= 3
