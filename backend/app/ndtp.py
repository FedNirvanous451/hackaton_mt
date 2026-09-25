"""Minimal NDTP TCP receiver for handshake and G6CellNav00 telemetry."""

import asyncio
import logging
import struct
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable

from .models import Telemetry

log = logging.getLogger(__name__)
MSK = timezone(timedelta(hours=3))
NAV = struct.Struct("<IIIBBHHHHHBB")
NPL = struct.Struct("<HHHHBIH")
NPH = struct.Struct("<HHHI")


def crc16_modbus(data: bytes) -> int:
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ (0xA001 if crc & 1 else 0)
    return crc


def decode_frame(frame: bytes, unit_to_tr: dict[int, int]) -> Telemetry | None:
    """Validate one complete frame and extract its first navigation cell.

    Other cell types are deliberately ignored: the emulator always sends Nav00 first.
    """
    if len(frame) < NPL.size + NPH.size:
        raise ValueError("short frame")
    signature, size, flags, crc_wire, packet_type, unit_id, _ = NPL.unpack_from(frame)
    if signature != 0x7E7E or packet_type != 2 or size != len(frame) - NPL.size:
        raise ValueError("invalid NPL header")
    payload = frame[NPL.size:]
    expected_crc = int.from_bytes(crc16_modbus(payload).to_bytes(2, "little"), "big")
    if crc_wire != expected_crc:
        raise ValueError("CRC mismatch")
    service_id, message_type, _, request_id = NPH.unpack_from(payload)
    if service_id == 0 and message_type == 100:
        return None
    if service_id != 1 or message_type != 101:
        raise ValueError("unsupported NDTP message")
    cells = payload[NPH.size:]
    if len(cells) < 2 + NAV.size or cells[0] != 0:
        raise ValueError("missing G6CellNav00")
    timestamp, lon, lat, bits, _, speed, _, course, _, altitude, _, _ = NAV.unpack_from(cells, 2)
    valid = bool(bits & 0x80)
    event_time = datetime.fromtimestamp(timestamp, MSK).replace(tzinfo=None)
    return Telemetry(tr_id=unit_to_tr.get(unit_id), unit_id=unit_id,
        event_time=event_time, gps_time=event_time, alt=float(altitude),
        received_at=datetime.now(MSK).replace(tzinfo=None),
        packet_id=f"{unit_id}:{request_id}", source="ndtp",
        lon=(lon / 1e7) * (1 if bits & 0x40 else -1) if valid else None,
        lat=(lat / 1e7) * (1 if bits & 0x20 else -1) if valid else None,
        speed_kmh=float(speed), heading_deg=float(course), location_valid=valid)


async def receive_ndtp(reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
                       mapping: dict[int, int], on_telemetry: Callable[[Telemetry], Awaitable[None]]) -> None:
    """Read multiple frames per connection; a new emulator connection can reconnect freely."""
    try:
        while True:
            head = await reader.readexactly(NPL.size)
            signature, size, *_ = NPL.unpack(head)
            if signature != 0x7E7E or size < NPH.size or size > 65535:
                raise ValueError("invalid NDTP frame size")
            frame = head + await reader.readexactly(size)
            try:
                item = decode_frame(frame, mapping)
                if item is not None:
                    await on_telemetry(item)
            except ValueError as exc:
                log.warning("Rejected NDTP frame: %s", exc)
    except (asyncio.IncompleteReadError, ConnectionResetError):
        pass
    except ValueError as exc:
        log.warning("NDTP connection closed: %s", exc)
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except ConnectionError:
            pass
