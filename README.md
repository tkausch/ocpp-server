# OCPP 2.1 Simulator

A small simulator for **OCPP 2.1**: a central system (CSMS / server) and a charging station (client). Both talk OCPP-J (JSON over WebSocket). The station is written in Python. The server comes in three interchangeable versions, one per language, with the same behavior:

| Server | Folder | Built with | Schema validation |
|--------|--------|------------|-------------------|
| Python | repo root (`csms.py`) | [`ocpp`](https://pypi.org/project/ocpp/) library, `ocpp.v21` module | yes |
| Go | `go/` | [`otfabric/go-ocpp`](https://github.com/otfabric/go-ocpp) | yes |
| Kotlin | `kotlin/` | Ktor + `kotlinx.serialization`, hand-written OCPP-J layer | no |

Every server listens on `ws://0.0.0.0:9000/<charge_point_id>` by default, so the Python station works against any of them. Run only one at a time per port.

## Requirements

- Python 3.10+ (developed on 3.14): always needed, for the station and the Python server
- Go 1.26+: only for the Go server
- A JDK (21 recommended) and Gradle or the included `./gradlew`: only for the Kotlin server

## Python setup

```sh
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Activate the environment in each new terminal with `source .venv/bin/activate`, or call `.venv/bin/python` directly.

## Run

Start **one** server in a terminal (Python below; Go and Kotlin follow in their own sections):

```sh
python csms.py
```

Start the charging station in a second terminal. It works against whichever server is running:

```sh
python charge_point.py                          # connects to ws://localhost:9000/CP_1
python charge_point.py ws://localhost:9001/CP_2 # custom URL / station ID, e.g. a server on another port
python charge_point.py --updates 3             # end the local session after 3 meter reports
python charge_point.py --remote-only           # no local session; wait for RequestStartTransaction from the server
```

If you see `address already in use`, another process holds the port, often a server you started earlier. Find it with `lsof -nP -iTCP:9000 -sTCP:LISTEN` and stop it with `kill <PID>`, or start the server on another port (the Go and Kotlin servers take a port option, see below).

## Go server

`go/` contains a Go port of the server (`csms.py`), built on [`otfabric/go-ocpp`](https://github.com/otfabric/go-ocpp). It behaves the same way, so the Python `charge_point.py` works against either server.

```sh
brew install go          # Go 1.26+
cd go
go run .                 # listens on :9000; use -addr :9001 to change
go test -race ./...
```

Stop any other server on port 9000 first, or use `-addr`. The Go server logs each message and the server-initiated RequestStopTransaction.

## Kotlin server

`kotlin/` contains a third version of the server, written in Kotlin with Ktor, `kotlinx.serialization` and coroutines. No Kotlin library with confirmed OCPP 2.1 central-system support was found, so the OCPP-J framing (`OcppFrame.kt`), the message classes (`Messages.kt`) and the handlers (`Csms.kt`) are hand-written. The behavior matches the other two servers.

Requirements: a JDK (the build uses a Java 21 toolchain) and Gradle (or just the included `./gradlew`).

```sh
# one-time, if your default JDK is newer than 21 (macOS + Homebrew):
export JAVA_HOME=/opt/homebrew/opt/openjdk@21/libexec/openjdk.jdk/Contents/Home

cd kotlin
./gradlew test
./gradlew run --args="9000"     # port argument is optional (default 9000)
```

A runnable distribution is also available: `./gradlew installDist`, then `build/install/ocpp-csms/bin/ocpp-csms [port]`.

Clients that do not offer the `ocpp2.1` subprotocol get a `404`. The Kotlin server does not validate payloads against the OCPP JSON schemas; the Python and Go servers do. Malformed payloads are answered with a `FormatViolation` error.

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
| `kotlin/` | Kotlin version of the server (Ktor). `OcppFrame.kt` (CALL / CALLRESULT / CALLERROR framing), `Messages.kt` (payload classes), `Csms.kt` (handlers, sessions), `Main.kt`; tests in `src/test/`. |
| `charge_point.py` | Station simulator. Boots, sends heartbeats, runs a session, and handles RequestStartTransaction, RequestStopTransaction and Reset from the server. Structure: `StationConfig` (fixed parameters), `Transaction` (per-session state), `ChargePoint` (CSMS handlers plus `boot`, `session`, `close`), `run()` and a CLI (`main`). See the module docstring. |
| `tests/test_flow.py` | Starts the server in-process, runs a station, and asserts the event sequence `Started`, `Updated`, `Ended`. |
| `requirements.txt` | Dependencies. |
| `pytest.ini` | Enables `pytest-asyncio` auto mode. |

## Tests

Each server has its own test suite; each starts its server in-process and checks the full flow (Boot, Heartbeat, Authorize, transaction, server-sent stop):

```sh
pytest                        # Python server, repo root (uses port 9100)
cd go && go test -race ./...  # Go server
cd kotlin && ./gradlew test   # Kotlin server
```

For an end-to-end check, start a server and run `python charge_point.py` against it. The server log should show Boot, Authorize, `Started`, three `Updated`, `RequestStopTransaction -> Accepted`, and `Ended`.

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

Server settings exist in all three versions (`csms.py`, `go/main.go`, `kotlin/.../Csms.kt`):

- **Reject a token:** the server blocks the id token `BAD` (`on_authorize` / `Authorize`); everything else is accepted.
- **Change when the server stops a session:** `STOP_AFTER_UPDATES` (`stopAfterUpdates` in Go).

Python-specific:

- **Session length and energy:** the loop in `session()` in `charge_point.py` (2 s interval, 500 Wh per step); `run(updates=N)` ends the session after N updates.
- **Remote start:** `connected[<id>].remote_start()` in `csms.py` sends RequestStartTransaction to a connected station. The Go (`remoteStart`) and Kotlin (`Csms.remoteStart`) servers have the same helper, but nothing calls it yet.

## Limitations

- Plain `ws://` only: no TLS and no basic auth (OCPP security profiles 2 and 3 are not implemented).
- Only the basic messages are handled. Any other action gets a "not implemented" error (from the library in Python and Go, from the server itself in Kotlin).
- The Kotlin server does not validate payloads against the OCPP schemas, and answers clients without the `ocpp2.1` subprotocol with a `404`.
- The Go library's OCPP 2.1 interoperability testing is still listed as in progress upstream.
- No persistence: transactions are only logged.
