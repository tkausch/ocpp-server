import charge_point
import csms


async def test_full_session(monkeypatch):
    events = []
    orig = csms.ChargePoint.on_transaction_event

    async def spy(self, event_type, **kw):
        events.append(event_type)
        return await orig.__wrapped__(self, event_type=event_type, **kw) if hasattr(orig, "__wrapped__") \
            else await orig(self, event_type=event_type, **kw)

    # keep the @on routing metadata when wrapping
    for attr in ("_on_action", "_skip_schema_validation", "_after_action"):
        if hasattr(orig, attr):
            setattr(spy, attr, getattr(orig, attr))
    monkeypatch.setattr(csms.ChargePoint, "on_transaction_event", spy)

    server = await csms.serve("127.0.0.1", 9100)
    try:
        await charge_point.run("ws://127.0.0.1:9100/CP_T", updates=1)
    finally:
        server.close()
        await server.wait_closed()

    assert list(events) == ["Started", "Updated", "Ended"]


async def _wait_for(cond, timeout=5):
    import asyncio
    async def poll():
        while not cond():
            await asyncio.sleep(0.02)
    await asyncio.wait_for(poll(), timeout)


def _record_events(monkeypatch):
    events = []
    orig = charge_point.ChargePoint.tx_event

    async def spy(self, tx, event, trigger, *a, **kw):
        events.append((str(event), str(trigger), kw.get("stopped_reason")))
        return await orig(self, tx, event, trigger, *a, **kw)

    monkeypatch.setattr(charge_point.ChargePoint, "tx_event", spy)
    return events


async def test_remote_start_and_double_start(monkeypatch):
    import asyncio
    from ocpp.v21 import call
    from ocpp.v21.datatypes import IdTokenType

    events = _record_events(monkeypatch)
    monkeypatch.setattr(csms, "STOP_AFTER_UPDATES", 1)
    cfg = charge_point.StationConfig(meter_interval=0.05)

    server = await csms.serve("127.0.0.1", 9101)
    station = asyncio.create_task(
        charge_point.run("ws://127.0.0.1:9101/CP_R", local_session=False, config=cfg))
    try:
        await _wait_for(lambda: "CP_R" in csms.connected)

        def start(i):
            return csms.connected["CP_R"].call(call.RequestStartTransaction(
                id_token=IdTokenType(id_token="REMOTE1", type="Central"), remote_start_id=i, evse_id=1))

        first = await start(1)
        second = await start(2)  # while the first session is running
        assert (first.status, second.status) == ("Accepted", "Rejected")
        await _wait_for(lambda: len(events) >= 3)
    finally:
        station.cancel()
        await asyncio.gather(station, return_exceptions=True)
        server.close()
        await server.wait_closed()

    assert [e[:2] for e in events] == [
        ("Started", "RemoteStart"), ("Updated", "MeterValuePeriodic"), ("Ended", "RemoteStop")]
    assert str(events[-1][2]) == "Remote"
