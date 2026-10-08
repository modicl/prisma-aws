"""Kafka: tópicos, mensajes y lag por grupo. SOLO lectura (metadatos y offsets); jamás se consumen ni se leen mensajes."""
import os

from aiokafka import AIOKafkaConsumer, TopicPartition
from aiokafka.admin import AIOKafkaAdminClient


def summarize(partitions: dict, beginning: dict, end: dict, committed: dict) -> dict:
    """Arma la respuesta a partir de offsets ya leídos (función pura: es lo que se prueba con tests)."""
    topics = [
        {
            "name": topic,
            "partitions": len(parts),
            "messages": sum(end[(topic, p)] - beginning[(topic, p)] for p in parts),
        }
        for topic, parts in partitions.items()
    ]
    groups = []
    for group, offsets in committed.items():
        por_topico: dict = {}
        for (topic, partition), offset in offsets.items():
            if (topic, partition) in end:
                por_topico[topic] = por_topico.get(topic, 0) + max(0, end[(topic, partition)] - offset)
        if not por_topico:
            groups.append({"group": group, "topic": None, "lag": None})
        for topic, lag in por_topico.items():
            groups.append({"group": group, "topic": topic, "lag": lag})
    return {"topics": topics, "groups": groups}


def _bootstrap() -> str:
    return os.environ.get("KAFKA_BOOTSTRAP_SERVERS", "kafka:9092")


async def kafka_overview() -> dict:
    admin = AIOKafkaAdminClient(bootstrap_servers=_bootstrap())
    await admin.start()
    try:
        nombres = [t for t in await admin.list_topics() if not t.startswith("__")]       # sin los tópicos internos
        descritos = await admin.describe_topics(nombres) if nombres else []
        partitions = {d["topic"]: sorted(p["partition"] for p in d["partitions"]) for d in descritos}
        tps = [TopicPartition(t, p) for t, ps in partitions.items() for p in ps]

        beginning, end = {}, {}
        if tps:
            consumidor = AIOKafkaConsumer(bootstrap_servers=_bootstrap(), group_id=None)     # sin grupo: no altera ninguno
            await consumidor.start()
            try:
                beginning = {(tp.topic, tp.partition): o for tp, o in (await consumidor.beginning_offsets(tps)).items()}
                end = {(tp.topic, tp.partition): o for tp, o in (await consumidor.end_offsets(tps)).items()}
            finally:
                await consumidor.stop()

        committed = {}
        for group, _tipo in await admin.list_consumer_groups():
            offsets = await admin.list_consumer_group_offsets(group)
            committed[group] = {(tp.topic, tp.partition): meta.offset for tp, meta in offsets.items()}
        return summarize(partitions, beginning, end, committed)
    finally:
        await admin.close()
