package li.kausch.ocpp

import io.ktor.client.plugins.websocket.WebSockets
import io.ktor.client.plugins.websocket.webSocket
import io.ktor.client.request.header
import io.ktor.http.HttpHeaders
import io.ktor.server.testing.testApplication
import io.ktor.websocket.Frame
import io.ktor.websocket.readText
import kotlinx.coroutines.withTimeout
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertNotNull
import kotlin.test.assertTrue

class CsmsTest {
    @Test
    fun fullSession() = testApplication {
        application { csmsModule() }
        val client = createClient { install(WebSockets) }

        client.webSocket("/CP_T", request = { header(HttpHeaders.SecWebSocketProtocol, SUBPROTOCOL) }) {
            val serverCalls = mutableListOf<Call>()
            var n = 0

            // Send a CALL and wait for its CALLRESULT. Server-initiated CALLs that
            // arrive meanwhile are answered with Accepted and recorded.
            suspend fun exchange(action: String, payload: JsonObject): JsonObject = withTimeout(10_000) {
                val id = "id-${n++}"
                send(Frame.Text(Call(id, action, payload).encode()))
                while (true) {
                    val f = incoming.receive() as Frame.Text
                    when (val m = parseFrame(f.readText())) {
                        is CallResult -> if (m.id == id) return@withTimeout m.payload
                        is Call -> {
                            serverCalls += m
                            send(Frame.Text(CallResult(m.id, buildJsonObject { put("status", "Accepted") }).encode()))
                        }
                        is CallError -> error("CALLERROR for $action: ${m.code} ${m.description}")
                    }
                }
                @Suppress("UNREACHABLE_CODE") error("unreachable")
            }

            fun obj(json: String) = ocppJson.parseToJsonElement(json) as JsonObject

            val boot = exchange(
                "BootNotification",
                obj("""{"chargingStation":{"model":"M","vendorName":"V"},"reason":"PowerUp"}"""),
            )
            assertEquals("Accepted", boot["status"].toString().trim('"'))
            assertEquals("10", boot["interval"].toString())

            assertNotNull(exchange("Heartbeat", obj("{}"))["currentTime"])

            for ((token, want) in listOf("RFID123" to "Accepted", "BAD" to "Blocked")) {
                val auth = exchange("Authorize", obj("""{"idToken":{"idToken":"$token","type":"ISO14443"}}"""))
                assertTrue(auth.toString().contains("\"status\":\"$want\""), "authorize $token -> $auth")
            }

            fun tx(event: String, seq: Int, trigger: String) = obj(
                """{"eventType":"$event","seqNo":$seq,"triggerReason":"$trigger","timestamp":"2026-01-01T00:00:00Z",
                    "transactionInfo":{"transactionId":"tx-1"}}""",
            )
            exchange("TransactionEvent", tx("Started", 1, "Authorized"))
            repeat(STOP_AFTER_UPDATES) { exchange("TransactionEvent", tx("Updated", 2 + it, "MeterValuePeriodic")) }

            // The stop request may arrive before or after the 3rd Updated answer.
            withTimeout(10_000) {
                while (serverCalls.none { it.action == "RequestStopTransaction" }) {
                    val m = parseFrame((incoming.receive() as Frame.Text).readText()) as Call
                    serverCalls += m
                    send(Frame.Text(CallResult(m.id, buildJsonObject { put("status", "Accepted") }).encode()))
                }
            }
            val stop = serverCalls.single { it.action == "RequestStopTransaction" }
            assertEquals("tx-1", stop.payload["transactionId"].toString().trim('"'))

            exchange("TransactionEvent", tx("Ended", 5, "RemoteStop"))
        }
    }

    @Test
    fun unknownActionIsRejected() = testApplication {
        application { csmsModule() }
        val client = createClient { install(WebSockets) }
        client.webSocket("/CP_T", request = { header(HttpHeaders.SecWebSocketProtocol, SUBPROTOCOL) }) {
            send(Frame.Text("""[2,"x","NoSuchAction",{}]"""))
            val reply = parseFrame((incoming.receive() as Frame.Text).readText())
            assertEquals(CallError("x", "NotImplemented", "NoSuchAction is not implemented"), reply)
        }
    }
}
