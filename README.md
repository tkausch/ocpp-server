# OCPP 2.1 Simulator

A small Python simulator for **OCPP 2.1**: a central system (CSMS / server) and a charging station (client). Both talk OCPP-J (JSON over WebSocket) using the [`ocpp`](https://pypi.org/project/ocpp/) library and its `ocpp.v21` module.

## Requirements

- Python 3.10+ (developed on 3.14)

## Setup

```sh
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Activate the environment in each new terminal with `source .venv/bin/activate`, or call `.venv/bin/python` directly.

## Run

Start the server in one terminal:

```sh
python csms.py
```

It listens on `ws://0.0.0.0:9000/<charge_point_id>`.

Start the charging station in a second terminal:

```sh
python charge_point.py                          # connects to ws://localhost:9000/CP_1
python charge_point.py ws://localhost:9000/CP_2 # custom URL / station ID
```

If you see `address already in use`, another process holds port 9000. Find it with `lsof -nP -iTCP:9000 -sTCP:LISTEN` and stop it with `kill <PID>`.

## Go server

`go/` contains a Go port of the server (`csms.py`), built on [`otfabric/go-ocpp`](https://github.com/otfabric/go-ocpp). It behaves the same way, so the Python `charge_point.py` works against either server.

```sh
brew install go          # Go 1.26+
cd go
go run .                 # listens on :9000; use -addr :9001 to change
go test -race ./...
```

Stop the Python server first, because both use port 9000. The Go server logs each message and the server-initiated RequestStopTransaction.

## What the simulation does

1. The station connects with the `ocpp2.1` subprotocol and sends **BootNotification**; the server accepts it and sets a 10 s heartbeat interval.
2. The station reports **StatusNotification** (`Available`) and sends **Heartbeat** periodically.
3. The station runs a charging session: **Authorize**, then **TransactionEvent** `Started`, several `Updated` events (+500 Wh every 2 s), and `Ended`.
4. After the third `Updated`, the server sends **RequestStopTransaction**; the station accepts and ends the session with stop reason `Remote`.

## Files

| File | Purpose |
|------|---------|
| `csms.py` | Server. One `ChargePoint` handler object per connection, with `@on(Action.…)` handlers for Boot, Heartbeat, Status, Authorize, MeterValues and TransactionEvent. Also has `remote_start()` to send RequestStartTransaction. |
| `go/main.go`, `go/main_test.go` | Go version of the server, with a test that uses the library's own station client. |
| `charge_point.py` | Station simulator. Boots, sends heartbeats, runs a session, and handles RequestStartTransaction, RequestStopTransaction and Reset from the server. |
| `tests/test_flow.py` | Starts the server in-process, runs a station, and asserts the event sequence `Started`, `Updated`, `Ended`. |
| `requirements.txt` | Dependencies. |
| `pytest.ini` | Enables `pytest-asyncio` auto mode. |

## Tests

```sh
pytest
```

The test uses port 9100.

## Manual testing

With [`websocat`](https://github.com/vi/websocat) (`brew install websocat`) you can talk to the server by hand:

```sh
websocat -t --protocol ocpp2.1 ws://localhost:9000/CP_X
```

Then send a raw OCPP call, for example:

```json
[2,"1","Heartbeat",{}]
```

The server answers with `[3,"1",{"currentTime":"…"}]`.

## Customising

- **Reject a token:** the server blocks the id token `BAD` (see `on_authorize` in `csms.py`); everything else is accepted.
- **Change when the server stops a session:** `STOP_AFTER_UPDATES` in `csms.py`.
- **Session length and energy:** the loop in `session()` in `charge_point.py` (2 s interval, 500 Wh per step); `run(updates=N)` ends the session after N updates.
- **Remote start:** `connected[<id>].remote_start()` in `csms.py` sends RequestStartTransaction to a connected station.

## Limitations

- Plain `ws://` only: no TLS and no basic auth (OCPP security profiles 2 and 3 are not implemented).
- Only the basic messages are handled. Any other action gets a "not implemented" error from the library.
- No persistence: transactions are only logged.
