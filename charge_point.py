"""Minimal OCPP 2.1 charge station simulator.

Connects to a CSMS over OCPP-J (JSON over WebSocket, subprotocol ``ocpp2.1``) and
plays one station with a single EVSE and connector.

Flow of ``run()``:
    1. connect, send BootNotification, report the connector as Available;
    2. send Heartbeat in the background at the interval the CSMS returned;
    3. either run one local charging session (Authorize, then TransactionEvent
       Started / Updated... / Ended) or, with ``local_session=False``, wait for
       RequestStartTransaction from the CSMS.

The CSMS can start a session (RequestStartTransaction), stop it
(RequestStopTransaction) or reset the station (Reset). At most one transaction
runs at a time; it is held in ``ChargePoint.tx``.

Usage:
    python charge_point.py [url] [--updates N] [--remote-only]
"""
import argparse
import asyncio
import contextlib
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

import websockets
from ocpp.routing import on
from ocpp.v21 import ChargePoint as cp
from ocpp.v21 import call, call_result
from ocpp.v21.datatypes import (ChargingStationType, EVSEType, IdTokenType, MeterValueType,
                                SampledValueType, TransactionType)
from ocpp.v21.enums import (Action, AuthorizationStatusEnumType, BootReasonEnumType, ChargingStateEnumType,
                            ConnectorStatusEnumType, MeasurandEnumType, ReasonEnumType,
                            RegistrationStatusEnumType, RequestStartStopStatusEnumType, ResetStatusEnumType,
                            TransactionEventEnumType, TriggerReasonEnumType)

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("cp")

DEFAULT_URL = "ws://localhost:9000/CP_1"


def now() -> str:
    """Current UTC time as an ISO 8601 string, the format OCPP expects."""
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class StationConfig:
    """Fixed identity and simulation parameters of the station.

    Attributes:
        model, vendor: reported in BootNotification.
        evse_id, connector_id: the single EVSE/connector this station simulates.
        token_type: OCPP IdToken type sent with Authorize (e.g. an RFID card).
        meter_interval: seconds between ``Updated`` events while charging.
        energy_step_wh: energy added to the meter per ``Updated`` event.
    """
    model: str = "SimCharger"
    vendor: str = "Sim Inc"
    evse_id: int = 1
    connector_id: int = 1
    token_type: str = "ISO14443"
    meter_interval: float = 2.0  # seconds between Updated events
    energy_step_wh: float = 500.0  # energy added per Updated event


@dataclass
class Transaction:
    """State of one charging session; created per session and dropped when it ends.

    Keeping this per session (instead of on the station) means a stop signal or
    meter reading can never leak into the next session.

    Attributes:
        token: id token that authorized the session.
        remote_start_id: set when the CSMS started the session; ``None`` for a local one.
        id: transaction id sent in every TransactionEvent.
        seq: last used ``seqNo``; incremented before each event.
        energy_wh: simulated meter reading (Wh).
        stop: set to end the metering loop (remote stop, reset).
        stop_trigger, stop_reason: trigger and reason reported in the ``Ended`` event
            when ``stop`` was set; default to a remote stop.
    """
    token: str
    remote_start_id: int | None = None
    id: str = field(default_factory=lambda: f"tx-{uuid.uuid4().hex[:12]}")
    seq: int = 0
    energy_wh: float = 0.0
    stop: asyncio.Event = field(default_factory=asyncio.Event)
    stop_trigger: TriggerReasonEnumType = TriggerReasonEnumType.remote_stop
    stop_reason: ReasonEnumType = ReasonEnumType.remote

    @property
    def remote(self) -> bool:
        """True if the CSMS started this session, so Authorize is skipped."""
        return self.remote_start_id is not None


class ChargePoint(cp):
    """The station: answers CSMS requests and drives the charging session.

    Attributes:
        config: station identity and simulation parameters.
        tx: the running transaction, or ``None`` when the connector is free.
            It is also the guard against starting two sessions at once.
        session_task: task of a remotely started session, kept so ``close()`` can cancel it.
    """

    def __init__(self, *a, config: StationConfig | None = None, **kw):
        super().__init__(*a, **kw)
        self.config = config or StationConfig()
        self.tx: Transaction | None = None
        self.session_task: asyncio.Task | None = None

    # --- requests from the CSMS ---
    @on(Action.request_start_transaction)
    async def on_remote_start(self, id_token, remote_start_id, **kw):
        """RequestStartTransaction: start a session in the background, or reject if one is running."""
        if self.tx:
            return call_result.RequestStartTransaction(status=RequestStartStopStatusEnumType.rejected)
        # Claim the connector before any await, so a concurrent start is rejected.
        tx = self.tx = Transaction(token=id_token["id_token"], remote_start_id=remote_start_id)
        self.session_task = asyncio.create_task(self._run_session(tx))
        return call_result.RequestStartTransaction(status=RequestStartStopStatusEnumType.accepted)

    @on(Action.request_stop_transaction)
    async def on_remote_stop(self, transaction_id, **kw):
        """RequestStopTransaction: end the matching session; reject an unknown transaction id.

        Only signals the metering loop; the loop sends the ``Ended`` event itself.
        """
        if not self.tx or transaction_id != self.tx.id:
            return call_result.RequestStopTransaction(status=RequestStartStopStatusEnumType.rejected)
        self.tx.stop.set()
        return call_result.RequestStopTransaction(status=RequestStartStopStatusEnumType.accepted)

    @on(Action.reset)
    async def on_reset(self, **kw):
        """Reset: always accepted. Nothing is rebooted, but a running session is ended with ImmediateReset."""
        if self.tx:  # end a running session as a reset would
            self.tx.stop_trigger = TriggerReasonEnumType.reset_command
            self.tx.stop_reason = ReasonEnumType.immediate_reset
            self.tx.stop.set()
        return call_result.Reset(status=ResetStatusEnumType.accepted)

    # --- station behaviour ---
    async def boot(self):
        """Send BootNotification, then report the connector Available.

        Returns the heartbeat interval (seconds) chosen by the CSMS.
        Raises RuntimeError if the CSMS does not accept the boot.
        """
        res = await self.call(call.BootNotification(
            charging_station=ChargingStationType(model=self.config.model, vendor_name=self.config.vendor),
            reason=BootReasonEnumType.power_up))
        log.info("Boot -> %s (interval %s)", res.status, res.interval)
        if res.status != RegistrationStatusEnumType.accepted:
            raise RuntimeError("boot not accepted")
        await self.status(ConnectorStatusEnumType.available)
        return res.interval

    async def status(self, st):
        """Send StatusNotification for the station's EVSE/connector."""
        await self.call(call.StatusNotification(
            timestamp=now(), connector_status=st,
            evse_id=self.config.evse_id, connector_id=self.config.connector_id))

    async def heartbeats(self, interval):
        """Send Heartbeat every ``interval`` seconds, forever (run as a background task)."""
        while True:
            await asyncio.sleep(interval)
            res = await self.call(call.Heartbeat())
            log.info("Heartbeat -> %s", res.current_time)

    async def tx_event(self, tx: Transaction, event, trigger, state=None, **kw):
        """Send one TransactionEvent with the next ``seqNo`` and the current meter reading.

        ``kw`` is passed to TransactionType (e.g. ``remote_start_id``, ``stopped_reason``).
        """
        tx.seq += 1
        ts = now()
        return await self.call(call.TransactionEvent(
            event_type=event, timestamp=ts, trigger_reason=trigger, seq_no=tx.seq,
            transaction_info=TransactionType(transaction_id=tx.id, charging_state=state, **kw),
            evse=EVSEType(id=self.config.evse_id, connector_id=self.config.connector_id),
            meter_value=[MeterValueType(timestamp=ts, sampled_value=[SampledValueType(
                value=tx.energy_wh, measurand=MeasurandEnumType.energy_active_import_register)])]))

    async def session(self, token="RFID123", updates=None):
        """Local session: authorize `token`, charge until stopped (or `updates` meter reports), unplug."""
        if self.tx:
            log.warning("session ignored: transaction %s already running", self.tx.id)
            return
        tx = self.tx = Transaction(token=token)  # claim before the first await
        await self._run_session(tx, updates)

    async def _run_session(self, tx: Transaction, updates=None):
        """Run the full lifecycle of ``tx``; the caller must already have set ``self.tx = tx``.

        Authorize (local only) -> Occupied -> Started -> Updated... -> Ended -> Available.
        The ``finally`` block always frees the connector, and on failure or cancellation
        makes a best-effort attempt to send ``Ended`` and ``Available``.
        """
        started = ended = False
        try:
            if not tx.remote and not await self._authorize(tx):
                return
            await self.status(ConnectorStatusEnumType.occupied)
            await self.tx_event(
                tx, TransactionEventEnumType.started,
                TriggerReasonEnumType.remote_start if tx.remote else TriggerReasonEnumType.authorized,
                ChargingStateEnumType.charging, remote_start_id=tx.remote_start_id)
            started = True
            await self._meter_loop(tx, updates)
            await self._end(tx)
            ended = True
        finally:
            if started and not ended:  # best effort, e.g. on cancellation
                with contextlib.suppress(Exception):
                    await self._end(tx)
            self.tx = None
            if started:
                with contextlib.suppress(Exception):
                    await self.status(ConnectorStatusEnumType.available)

    async def _authorize(self, tx: Transaction) -> bool:
        """Send Authorize for the session's token; True if the CSMS accepted it."""
        auth = await self.call(call.Authorize(
            id_token=IdTokenType(id_token=tx.token, type=self.config.token_type)))
        status = auth.id_token_info["status"]
        log.info("Authorize -> %s", status)
        return status == AuthorizationStatusEnumType.accepted

    async def _meter_loop(self, tx: Transaction, updates):
        """Send an ``Updated`` event every ``meter_interval`` seconds until stopped.

        Waiting on ``tx.stop`` doubles as the interval timer and wakes at once on a stop.
        If ``updates`` is set, the loop also ends (a local stop) after that many events.
        """
        n = 0
        while not tx.stop.is_set():
            try:
                await asyncio.wait_for(tx.stop.wait(), timeout=self.config.meter_interval)
            except asyncio.TimeoutError:
                tx.energy_wh += self.config.energy_step_wh
                n += 1
                await self.tx_event(tx, TransactionEventEnumType.updated,
                                    TriggerReasonEnumType.meter_value_periodic, ChargingStateEnumType.charging)
                if updates and n >= updates:
                    break

    async def _end(self, tx: Transaction):
        """Send the ``Ended`` event: remote stop/reset if ``tx.stop`` was set, else EV departed."""
        stopped = tx.stop.is_set()
        await self.tx_event(
            tx, TransactionEventEnumType.ended,
            tx.stop_trigger if stopped else TriggerReasonEnumType.ev_departed,
            ChargingStateEnumType.idle,
            stopped_reason=tx.stop_reason if stopped else ReasonEnumType.local)
        log.info("Transaction %s ended at %.0f Wh", tx.id, tx.energy_wh)

    async def close(self, *tasks: asyncio.Task | None):
        """Cancel and await the session task, then the given tasks (heartbeat, listener).

        The session goes first so it can still send its final messages while the connection is up.
        """
        for t in (self.session_task, *tasks):
            if t and not t.done():
                t.cancel()
            if t:
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await t


async def run(url=DEFAULT_URL, local_session=True, updates=None, config: StationConfig | None = None):
    """Connect to the CSMS at ``url`` and run the station until the session ends.

    The last URL path segment is the station id (``ws://host:9000/CP_1`` -> ``CP_1``).

    Args:
        local_session: run one local session; if False, wait for CSMS requests until cancelled.
        updates: end the local session after this many ``Updated`` events (``None``: run until stopped).
        config: station parameters; defaults to ``StationConfig()``.
    """
    async with websockets.connect(url, subprotocols=["ocpp2.1"]) as ws:
        c = ChargePoint(url.rstrip("/").split("/")[-1], ws, config=config)
        listener = asyncio.create_task(c.start())
        hb = None
        try:
            interval = await c.boot()
            hb = asyncio.create_task(c.heartbeats(interval))
            if local_session:
                await c.session(updates=updates)
            else:
                await asyncio.Event().wait()  # serve remote requests until cancelled
        finally:
            await c.close(hb, listener)


def main(argv=None):
    """Command-line entry point; ``argv`` defaults to ``sys.argv[1:]``."""
    p = argparse.ArgumentParser(description="OCPP 2.1 charge station simulator")
    p.add_argument("url", nargs="?", default=DEFAULT_URL, help="CSMS URL ending in the station id")
    p.add_argument("--updates", type=int, default=5, help="meter reports before the local session ends")
    p.add_argument("--remote-only", action="store_true",
                   help="no local session; wait for RequestStartTransaction from the CSMS")
    args = p.parse_args(argv)
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(run(args.url, local_session=not args.remote_only, updates=args.updates))


if __name__ == "__main__":
    main()
