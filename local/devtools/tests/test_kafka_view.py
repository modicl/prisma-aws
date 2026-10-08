"""Kafka: mensajes por tópico y lag por grupo, calculados con una función pura. Los clientes reales se prueban contra el compose."""
from app.kafka_view import summarize


def test_cuenta_los_mensajes_sumando_las_particiones():
    res = summarize(
        partitions={"prisma.agent-events": [0, 1, 2]},
        beginning={("prisma.agent-events", p): 0 for p in range(3)},
        end={("prisma.agent-events", 0): 92, ("prisma.agent-events", 1): 214, ("prisma.agent-events", 2): 157},
        committed={},
    )
    assert res["topics"] == [{"name": "prisma.agent-events", "partitions": 3, "messages": 463}]
    assert res["groups"] == []


def test_los_offsets_iniciales_descuentan_lo_ya_expirado():
    res = summarize({"t": [0]}, {("t", 0): 100}, {("t", 0): 130}, {})
    assert res["topics"][0]["messages"] == 30


def test_el_lag_es_fin_menos_offset_confirmado_sumado_por_particion():
    res = summarize(
        partitions={"t": [0, 1]},
        beginning={("t", 0): 0, ("t", 1): 0},
        end={("t", 0): 10, ("t", 1): 20},
        committed={"archiver": {("t", 0): 10, ("t", 1): 15}},
    )
    assert res["groups"] == [{"group": "archiver", "topic": "t", "lag": 5}]


def test_un_grupo_con_offsets_en_dos_topicos_da_una_fila_por_topico():
    res = summarize(
        partitions={"a": [0], "b": [0]},
        beginning={("a", 0): 0, ("b", 0): 0},
        end={("a", 0): 4, ("b", 0): 9},
        committed={"g": {("a", 0): 4, ("b", 0): 5}},
    )
    assert sorted((g["topic"], g["lag"]) for g in res["groups"]) == [("a", 0), ("b", 4)]


def test_un_grupo_sin_offsets_confirmados_se_muestra_sin_lag():
    res = summarize({"t": [0]}, {("t", 0): 0}, {("t", 0): 7}, {"nuevo": {}})
    assert res["groups"] == [{"group": "nuevo", "topic": None, "lag": None}]


def test_sin_topicos_devuelve_listas_vacias():
    """Foco de revisión 4: Kafka recién creado."""
    assert summarize({}, {}, {}, {}) == {"topics": [], "groups": []}


def test_un_topico_vacio_tiene_cero_mensajes():
    res = summarize({"t": [0, 1]}, {("t", 0): 0, ("t", 1): 0}, {("t", 0): 0, ("t", 1): 0}, {})
    assert res["topics"] == [{"name": "t", "partitions": 2, "messages": 0}]


def test_el_lag_nunca_es_negativo():
    """Si el offset confirmado supera al fin (retención o truncado), no se muestra lag negativo."""
    res = summarize({"t": [0]}, {("t", 0): 0}, {("t", 0): 3}, {"g": {("t", 0): 9}})
    assert res["groups"][0]["lag"] == 0
