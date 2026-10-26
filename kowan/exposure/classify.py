"""Classify published ports: public vs local-only, and risky well-known services.

A port bound on ``0.0.0.0`` / ``::`` (or with no IP) is reachable from every
interface of the host: *public*. ``127.0.0.1`` / ``::1`` is *local* only. Any
other concrete address is bound to one *interface* (often a LAN or VPN IP).

Databases, caches, brokers and admin UIs should never be public: they are
flagged by their container port (and, as a fallback, by the image name), so the
UI can draw the row with a warning.
"""

from __future__ import annotations

import ipaddress
from typing import Dict, Optional, Tuple

SCOPE_PUBLIC = "public"
SCOPE_LOCAL = "local"
SCOPE_INTERFACE = "interface"

# container port -> (kind, label)
WELL_KNOWN_PORTS: Dict[int, Tuple[str, str]] = {
    3306: ("database", "MySQL / MariaDB"),
    5432: ("database", "PostgreSQL"),
    1433: ("database", "SQL Server"),
    1521: ("database", "Oracle"),
    27017: ("database", "MongoDB"),
    27018: ("database", "MongoDB"),
    6379: ("database", "Redis"),
    11211: ("database", "Memcached"),
    9200: ("database", "Elasticsearch"),
    9300: ("database", "Elasticsearch transport"),
    5984: ("database", "CouchDB"),
    8086: ("database", "InfluxDB"),
    7474: ("database", "Neo4j"),
    7687: ("database", "Neo4j Bolt"),
    9042: ("database", "Cassandra"),
    26257: ("database", "CockroachDB"),
    8123: ("database", "ClickHouse HTTP"),
    9000: ("admin", "Portainer / MinIO / ClickHouse"),
    9443: ("admin", "Portainer"),
    5672: ("database", "AMQP broker"),
    15672: ("admin", "RabbitMQ management"),
    9092: ("database", "Kafka"),
    2181: ("database", "ZooKeeper"),
    2379: ("database", "etcd"),
    8500: ("admin", "Consul"),
    8200: ("admin", "Vault"),
    2375: ("docker", "Docker API (unencrypted)"),
    2376: ("docker", "Docker API"),
    5601: ("admin", "Kibana"),
    9090: ("admin", "Prometheus"),
    9093: ("admin", "Alertmanager"),
    10250: ("admin", "Kubelet"),
    6443: ("admin", "Kubernetes API"),
    8081: ("admin", "Admin UI (8081)"),
}

# image name fragment -> (kind, label), used when the container port is unusual.
WELL_KNOWN_IMAGES: Dict[str, Tuple[str, str]] = {
    "postgres": ("database", "PostgreSQL"),
    "postgis": ("database", "PostgreSQL"),
    "mysql": ("database", "MySQL"),
    "mariadb": ("database", "MariaDB"),
    "mongo": ("database", "MongoDB"),
    "redis": ("database", "Redis"),
    "valkey": ("database", "Valkey"),
    "memcached": ("database", "Memcached"),
    "elasticsearch": ("database", "Elasticsearch"),
    "opensearch": ("database", "OpenSearch"),
    "influxdb": ("database", "InfluxDB"),
    "rabbitmq": ("database", "RabbitMQ"),
    "adminer": ("admin", "Adminer"),
    "phpmyadmin": ("admin", "phpMyAdmin"),
    "pgadmin": ("admin", "pgAdmin"),
    "portainer": ("admin", "Portainer"),
    "mongo-express": ("admin", "mongo-express"),
    "redis-commander": ("admin", "Redis Commander"),
    "redisinsight": ("admin", "RedisInsight"),
}


def bind_scope(ip: str) -> str:
    """Return ``public`` / ``local`` / ``interface`` for a published port's bind IP."""
    s = (ip or "").strip().strip("[]")
    if s in ("", "0.0.0.0", "::", "*"):
        return SCOPE_PUBLIC
    try:
        addr = ipaddress.ip_address(s)
    except ValueError:
        return SCOPE_INTERFACE
    if addr.is_loopback:
        return SCOPE_LOCAL
    if addr.is_unspecified:
        return SCOPE_PUBLIC
    return SCOPE_INTERFACE


def _image_base(image: str) -> str:
    ref = (image or "").lower().split("@", 1)[0]
    last = ref.rsplit("/", 1)[-1]
    return last.split(":", 1)[0]


def risk_for(private_port: int, image: str = "") -> Optional[Dict[str, str]]:
    """``{"kind", "label"}`` when the port/image is a database or admin surface."""
    hit = WELL_KNOWN_PORTS.get(int(private_port or 0))
    if hit is None:
        base = _image_base(image)
        for frag, val in WELL_KNOWN_IMAGES.items():
            if base == frag or base.startswith(frag + "-") or base.endswith("-" + frag) or base == frag + "s":
                hit = val
                break
    if hit is None:
        return None
    return {"kind": hit[0], "label": hit[1]}


def classify_port(ip: str, private_port: int, image: str = "") -> Dict[str, object]:
    """Scope plus risk; ``warn`` is true only for a risky port that is public."""
    scope = bind_scope(ip)
    risk = risk_for(private_port, image)
    return {"scope": scope, "risk": risk, "warn": bool(risk) and scope == SCOPE_PUBLIC}
