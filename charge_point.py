"""Minimal OCPP 2.1 charge station simulator."""
import asyncio
import logging
import sys
from datetime import datetime, timezone

import websockets
from ocpp.routing import on
from ocpp.v21 import ChargePoint as cp
from ocpp.v21 import call, call_result
from ocpp.v21.datatypes import (ChargingStationType, EVSEType, IdTokenType, MeterValueType,
                                SampledValueType, TransactionType)
from ocpp.v21.enums import (Action, BootReasonEnumType, ChargingStateEnumType, ConnectorStatusEnumType,
                            MeasurandEnumType, ReasonEnumType, RegistrationStatusEnumType,
                            RequestStartStopStatusEnumType, ResetStatusEnumType,
                            TransactionEventEnumType, TriggerReasonEnumType)

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("cp")


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ChargePoint(cp):
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.tx_id = None
        self.stop_event = asyncio.Event()
        self.session_task = None
        self.seq = 0
        self.energy_wh = 0.0

    # --- requests from the CSMS ---
    @on(Action.request_start_transaction)
    async def on_remote_start(self, id_token, remote_start_id, **kw):
        if self.tx_id:
            return call_result.RequestStartTransaction(status=RequestStartStopStatusEnumType.rejected)
        self.session_task = asyncio.create_task(
            self.session(id_token["id_token"], remote=True, remote_start_id=remote_start_id))
        return call_result.RequestStartTransaction(status=RequestStartStopStatusEnumType.accepted)

    @on(Action.request_stop_transaction)
    async def on_remote_stop(self, transaction_id, **kw):
        if transaction_id != self.tx_id:
            return call_result.RequestStopTransaction(status=RequestStartStopStatusEnumType.rejected)
        self.stop_event.set()
        return call_result.RequestStopTransaction(status=RequestStartStopStatusEnumType.accepted)

    @on(Action.reset)
    async def on_reset(self, **kw):
        return call_result.Reset(status=ResetStatusEnumType.accepted)

    # --- station behaviour ---
    async def boot(self):
        res = await self.call(call.BootNotification(
            charging_station=ChargingStationType(model="SimCharger", vendor_name="Sim Inc"),
            reason=BootReasonEnumType.power_up))
        log.info("Boot -> %s (interval %s)", res.status, res.interval)
        if res.status != RegistrationStatusEnumType.accepted:
            raise RuntimeError("boot not accepted")
        await self.status(ConnectorStatusEnumType.available)
        return res.interval

    async def status(self, st):
        await self.call(call.StatusNotification(
            timestamp=now(), connector_status=st, evse_id=1, connector_id=1))

    async def heartbeats(self, interval):
        while True:
            await asyncio.sleep(interval)
            res = await self.call(call.Heartbeat())
            log.info("Heartbeat -> %s", res.current_time)

    async def tx_event(self, event, trigger, state=None, **kw):
        self.seq += 1
        res = await self.call(call.TransactionEvent(
            event_type=event, timestamp=now(), trigger_reason=trigger, seq_no=self.seq,
            transaction_info=TransactionType(transaction_id=self.tx_id, charging_state=state, **kw),
            evse=EVSEType(id=1, connector_id=1),
            meter_value=[MeterValueType(timestamp=now(), sampled_value=[SampledValueType(
                value=self.energy_wh, measurand=MeasurandEnumType.energy_active_import_register)])]))
        return res

    async def session(self, token="RFID123", remote=False, remote_start_id=None, updates=None):
        """Plug in, authorize, charge until stopped (or `updates` meter reports), unplug."""
        if not remote:
            auth = await self.call(call.Authorize(id_token=IdTokenType(id_token=token, type="ISO14443")))
            log.info("Authorize -> %s", auth.id_token_info["status"])
            if auth.id_token_info["status"] != "Accepted":
                return
        self.tx_id = f"tx-{int(datetime.now().timestamp())}"
        self.seq, self.energy_wh = 0, 0.0
        self.stop_event.clear()
        await self.status(ConnectorStatusEnumType.occupied)
        await self.tx_event(TransactionEventEnumType.started,
                            TriggerReasonEnumType.remote_start if remote else TriggerReasonEnumType.authorized,
                            ChargingStateEnumType.charging, remote_start_id=remote_start_id)
        n = 0
        while not self.stop_event.is_set():
            try:
                await asyncio.wait_for(self.stop_event.wait(), timeout=2)
            except asyncio.TimeoutError:
                self.energy_wh += 500
                n += 1
                await self.tx_event(TransactionEventEnumType.updated,
                                    TriggerReasonEnumType.meter_value_periodic, ChargingStateEnumType.charging)
                if updates and n >= updates:
                    break
        remote_stop = self.stop_event.is_set()
        await self.tx_event(TransactionEventEnumType.ended,
                            TriggerReasonEnumType.remote_stop if remote_stop else TriggerReasonEnumType.ev_departed,
                            ChargingStateEnumType.idle,
                            stopped_reason=ReasonEnumType.remote if remote_stop else ReasonEnumType.local)
        log.info("Transaction %s ended at %.0f Wh", self.tx_id, self.energy_wh)
        self.tx_id = None
        await self.status(ConnectorStatusEnumType.available)


async def run(url="ws://localhost:9000/CP_1", local_session=True, updates=None):
    async with websockets.connect(url, subprotocols=["ocpp2.1"]) as ws:
        c = ChargePoint(url.rstrip("/").split("/")[-1], ws)
        listener = asyncio.create_task(c.start())
        interval = await c.boot()
        hb = asyncio.create_task(c.heartbeats(interval))
        if local_session:
            await c.session(updates=updates)
        else:
            await asyncio.sleep(3600)
        hb.cancel()
        listener.cancel()


if __name__ == "__main__":
    asyncio.run(run(*(sys.argv[1:2] or ["ws://localhost:9000/CP_1"]), updates=5))
