"""Проверка почтового домена перед отправкой письма подтверждения.

Боты массово регистрируются с несуществующих/мусорных адресов; SMTP-сервер
принимает письмо, а возврат (bounce) приходит позже на наш ящик. Проверка
DNS до отправки отсекает такие домены: у рабочего почтового домена всегда
есть MX-запись (или, по RFC 5321, A/AAAA-запись при отсутствии MX).

Только стандартная библиотека: сырой UDP-запрос к DNS-серверам из
/etc/resolv.conf (или системного резолвера), без новых зависимостей.

Результат кешируется в памяти: успешный домен — на 1 час, несуществующий —
на 24 часа (чтобы DNS не долбили повторными попытками регистрации).
"""

import random
import socket
import struct
import time

from .config import get_settings

CACHE_TTL_OK = 3600
CACHE_TTL_FAIL = 24 * 3600

# (нормализованный домен -> (есть_ли_почта, timestamp))
_cache: dict[str, tuple[bool, float]] = {}

QTYPE_MX = 15

FALLBACK_RESOLVERS = ["8.8.8.8", "1.1.1.1"]


def _nameservers() -> list[str]:
    servers: list[str] = []
    try:
        with open("/etc/resolv.conf", encoding="utf-8") as fh:
            for line in fh:
                parts = line.split()
                if len(parts) >= 2 and parts[0] == "nameserver":
                    servers.append(parts[1])
    except OSError:
        pass
    return servers or FALLBACK_RESOLVERS


def _encode_query(name: str, qtype: int) -> bytes:
    qname = b"".join(
        bytes([len(label)]) + label.encode("idna") if label else b""
        for label in name.split(".")
    ) + b"\x00"
    # RD=1: разрешить рекурсию на сервере
    header = struct.pack(">HHHHHH", 0, 0x0100, 1, 0, 0, 0)
    return header + qname + struct.pack(">HH", qtype, 1)


def _decode_names(payload: bytes, offset: int) -> tuple[str, int]:
    """Имя из ответа DNS с распаковкой указателей (RFC 1035, п. 4.1.4)."""
    labels: list[str] = []
    end = None
    for _ in range(64):  # защита от зацикленных указателей
        if offset >= len(payload):
            return "".join(labels), offset
        length = payload[offset]
        if length == 0:
            offset += 1
            return ".".join(labels), end if end is not None else offset
        if length & 0xC0 == 0xC0:
            if offset + 1 >= len(payload):
                return "".join(labels), offset
            pointer = ((length & 0x3F) << 8) | payload[offset + 1]
            if end is None:
                end = offset + 2
            offset = pointer
            continue
        labels.append(payload[offset + 1: offset + 1 + length].decode("ascii", "replace"))
        offset += 1 + length
    return "".join(labels), offset


def _dns_query(name: str, qtype: int, timeout: float = 4.0) -> list[bytes] | None:
    """Один UDP-запрос; возвращает список RDATA записей или None при сбое."""
    server = random.choice(_nameservers())
    query = _encode_query(name, qtype)
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.settimeout(timeout)
            sock.sendto(query, (server, 53))
            response, _ = sock.recvfrom(4096)
    except OSError:
        return None

    try:
        (_txid, flags, qdcount, ancount, _, _), = [struct.unpack(">HHHHHH", response[:12])]
    except (struct.error, ValueError):
        return None
    if not flags & 0x8000 or not ancount:  # не ответ или ответ без записей
        return []
    rcode = flags & 0x0F
    if rcode != 0:
        return []

    offset = 12
    for _ in range(qdcount):
        _, offset = _decode_names(response, offset)
        offset += 4
    records = []
    for _ in range(ancount):
        _, offset = _decode_names(response, offset)
        if offset + 10 > len(response):
            break
        rtype, _class, _ttl, rdlength = struct.unpack(">HHIH", response[offset: offset + 10])
        offset += 10
        if rtype == qtype:
            records.append(response[offset: offset + rdlength])
        offset += rdlength
    return records


def domain_accepts_mail(domain: str) -> bool:
    """Пускает ли домен почту: требуется настоящая MX-запись.

    Фолбэка на A/AAAA (RFC 5321) нет намеренно: припаркованные домены без
    почты (ex.com и подобные) часто имеют только A-запись — SMTP-сервер
    примет письмо, а оно потом вернётся bounce'ом. Все реальные почтовые
    домены MX имеют, поэтому ложных отказов у нормальных пользователей
    правило практически не даёт.

    None при полном сбое DNS (все серверы недоступны) — в этом случае
    проверку лучше пропустить, а не блокировать реальных пользователей.
    """
    domain = domain.strip().lower().rstrip(".")
    if not domain or "." not in domain:
        return False

    if domain in _domain_blocklist():
        return False

    cached = _cache.get(domain)
    if cached is not None:
        ok, ts = cached
        if time.time() - ts < (CACHE_TTL_OK if ok else CACHE_TTL_FAIL):
            return ok

    mx = _dns_query(domain, QTYPE_MX)
    if mx is None:  # DNS недоступен целиком
        return True  # осторожно: не блокируем, письмо может уйти в bounce
    ok = bool(mx)
    _cache[domain] = (ok, time.time())
    return ok


def _domain_blocklist() -> set[str]:
    """Чёрный список доменов из AIOS_EMAIL_DOMAIN_BLOCKLIST (через запятую)."""
    raw = get_settings().email_domain_blocklist
    return {item.strip().lower().lstrip(".") for item in raw.split(",") if item.strip()}


def email_domain_accepts_mail(email: str) -> bool:
    """Проверка по полному адресу; домен берётся после последнего «@»."""
    _, _, domain = (email or "").rpartition("@")
    return domain_accepts_mail(domain)