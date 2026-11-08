"""Minimal OCPP 2.1 central system (CSMS) simulator."""
import asyncio
import logging
from datetime import datetime, timezone

import websockets
from ocpp.routing import on
from ocpp.v21 import ChargePoint as cp
from ocpp.v21 import call, call_result
from ocpp.v21.datatypes import IdTokenInfoType, IdTokenType
from ocpp.v21.enums import Action, AuthorizationStatusEnumType, RegistrationStatusEnumType, TransactionEventEnumType

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("csms")

STOP_AFTER_UPDATES = 3  # remotely stop a transaction after this many Updated events


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ChargePoint(cp):
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.updates = {}

    @on(Action.boot_notification)
    async def on_boot(self, charging_station, reason, **kw):
        log.info("%s Boot: %s (%s)", self.id, charging_station, reason)
        return call_result.BootNotification(
            current_time=now(), interval=10, status=RegistrationStatusEnumType.accepted
        )

    @on(Action.heartbeat)
    async def on_heartbeat(self, **kw):
        log.info("%s Heartbeat", self.id)
        return call_result.Heartbeat(current_time=now())

    @on(Action.status_notification)
    async def on_status(self, connector_id, connector_status, evse_id, **kw):
        log.info("%s Status evse=%s connector=%s -> %s", self.id, evse_id, connector_id, connector_status)
        return call_result.StatusNotification()

    @on(Action.authorize)
    async def on_authorize(self, id_token, **kw):
        log.info("%s Authorize %s", self.id, id_token)
        status = (AuthorizationStatusEnumType.accepted
                  if id_token["id_token"] != "BAD" else AuthorizationStatusEnumType.blocked)
        return call_result.Authorize(id_token_info=IdTokenInfoType(status=status))

    @on(Action.meter_values)
    async def on_meter_values(self, **kw):
        return call_result.MeterValues()

    @on(Action.transaction_event)
    async def on_transaction_event(self, event_type, transaction_info, trigger_reason, seq_no, **kw):
        tx = transaction_info["transaction_id"]
        log.info("%s TransactionEvent %s tx=%s seq=%s trigger=%s", self.id, event_type, tx, seq_no, trigger_reason)
        if event_type == TransactionEventEnumType.updated:
            self.updates[tx] = self.updates.get(tx, 0) + 1
            if self.updates[tx] == STOP_AFTER_UPDATES:
                asyncio.create_task(self.stop(tx))
        return call_result.TransactionEvent()

    async def stop(self, tx):
        res = await self.call(call.RequestStopTransaction(transaction_id=tx))
        log.info("%s RequestStopTransaction -> %s", self.id, res.status)

    async def remote_start(self, token="REMOTE1", remote_start_id=1, evse_id=1):
        res = await self.call(call.RequestStartTransaction(
            id_token=IdTokenType(id_token=token, type="Central"),
            remote_start_id=remote_start_id, evse_id=evse_id))
        log.info("%s RequestStartTransaction -> %s", self.id, res.status)


connected: dict[str, ChargePoint] = {}


async def on_connect(ws):
    cp_id = ws.request.path.strip("/").split("/")[-1]
    if ws.subprotocol != "ocpp2.1":
        log.warning("%s rejected: subprotocol %s", cp_id, ws.subprotocol)
        return await ws.close(1002, "ocpp2.1 required")
    log.info("%s connected", cp_id)
    charge_point = ChargePoint(cp_id, ws)
    connected[cp_id] = charge_point
    try:
        await charge_point.start()
    except websockets.exceptions.ConnectionClosed:
        pass
    finally:
        connected.pop(cp_id, None)
        log.info("%s disconnected", cp_id)


async def serve(host="0.0.0.0", port=9000):
    return await websockets.serve(on_connect, host, port, subprotocols=["ocpp2.1"])


async def main():
    server = await serve()
    log.info("CSMS listening on ws://0.0.0.0:9000/<charge_point_id>")
    await server.wait_closed()


if __name__ == "__main__":
    asyncio.run(main())
