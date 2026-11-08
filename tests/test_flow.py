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
