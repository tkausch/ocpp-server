package li.kausch.ocpp

import io.ktor.server.application.Application
import io.ktor.server.routing.routing
import io.ktor.server.websocket.WebSockets
import io.ktor.server.websocket.DefaultWebSocketServerSession
import io.ktor.server.websocket.webSocket
import io.ktor.server.websocket.pingPeriod
import io.ktor.server.application.install
import io.ktor.websocket.Frame
import io.ktor.websocket.readText
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.coroutineScope
import kotlinx.coroutines.launch
import kotlinx.coroutines.withTimeout
import kotlinx.serialization.encodeToString
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.decodeFromJsonElement
import kotlinx.serialization.json.encodeToJsonElement
import kotlinx.serialization.json.jsonObject
import org.slf4j.LoggerFactory
import kotlin.time.Duration.Companion.seconds
import java.time.Instant
import java.util.UUID
import java.util.concurrent.ConcurrentHashMap

const val SUBPROTOCOL = "ocpp2.1"
const val STOP_AFTER_UPDATES = 3 // remotely stop a transaction after this many Updated events

val ocppJson = Json { ignoreUnknownKeys = true; explicitNulls = false }

private fun now() = Instant.now().toString()

class OcppCallFailed(message: String) : Exception(message)

/** One connected charging station. */
class Session(val id: String, private val ws: DefaultWebSocketServerSession, private val scope: CoroutineScope, private val csms: Csms) {
    private val log = LoggerFactory.getLogger("csms")
    private val pending = ConcurrentHashMap<String, CompletableDeferred<OcppFrame>>()

    private suspend fun send(frame: OcppFrame) = ws.send(Frame.Text(frame.encode()))

    /** Send a CALL to the station and wait for its answer. */
    suspend fun sendCall(action: String, payload: JsonObject, timeoutMs: Long = 30_000): JsonObject {
        val callId = UUID.randomUUID().toString()
        val answer = CompletableDeferred<OcppFrame>()
        pending[callId] = answer
        try {
            send(Call(callId, action, payload))
            return when (val r = withTimeout(timeoutMs) { answer.await() }) {
                is CallResult -> r.payload
                is CallError -> throw OcppCallFailed("$action failed: ${r.code} ${r.description}")
                else -> throw OcppCallFailed("unexpected answer to $action")
            }
        } finally {
            pending.remove(callId)
        }
    }

    suspend inline fun <reified Req, reified Res> request(action: String, req: Req): Res =
        ocppJson.decodeFromJsonElement(sendCall(action, ocppJson.encodeToJsonElement(req).jsonObject))

    /** Handle one text message from the station. */
    suspend fun onText(text: String) {
        val frame = try {
            parseFrame(text)
        } catch (e: FrameException) {
            log.warn("{} bad frame: {}", id, e.message)
            send(CallError(e.id, "FormatViolation", e.message ?: ""))
            return
        }
        when (frame) {
            is Call -> handleCall(frame)
            is CallResult, is CallError -> pending[frame.id]?.complete(frame)
                ?: log.warn("{} unsolicited answer {}", id, frame.id)
        }
    }

    private suspend fun handleCall(call: Call) {
        val reply = try {
            val payload = csms.dispatch(this, scope, call.action, call.payload)
                ?: return send(CallError(call.id, "NotImplemented", "${call.action} is not implemented"))
            CallResult(call.id, payload)
        } catch (e: kotlinx.serialization.SerializationException) {
            CallError(call.id, "FormatViolation", e.message ?: "invalid payload")
        } catch (e: IllegalArgumentException) {
            CallError(call.id, "FormatViolation", e.message ?: "invalid payload")
        }
        send(reply)
    }

    fun cancelPending() = pending.values.forEach { it.cancel() }
}

/** The central system: handlers and the registry of connected stations. */
class Csms {
    private val log = LoggerFactory.getLogger("csms")
    val sessions = ConcurrentHashMap<String, Session>()
    private val updates = ConcurrentHashMap<String, Int>() // transaction id -> Updated events seen

    private inline fun <reified T> JsonObject.decode(): T = ocppJson.decodeFromJsonElement(this)
    private inline fun <reified T> T.json(): JsonObject = ocppJson.encodeToJsonElement(this).jsonObject
    private val empty = JsonObject(emptyMap())

    /** Returns the response payload, or null for an unknown action. */
    suspend fun dispatch(s: Session, scope: CoroutineScope, action: String, payload: JsonObject): JsonObject? = when (action) {
        "BootNotification" -> {
            val r = payload.decode<BootNotificationRequest>()
            log.info("{} Boot: {} ({})", s.id, r.chargingStation, r.reason)
            BootNotificationResponse(now(), 10, "Accepted").json()
        }
        "Heartbeat" -> {
            log.info("{} Heartbeat", s.id)
            HeartbeatResponse(now()).json()
        }
        "StatusNotification" -> {
            val r = payload.decode<StatusNotificationRequest>()
            log.info("{} Status evse={} connector={} -> {}", s.id, r.evseId, r.connectorId, r.connectorStatus)
            empty
        }
        "Authorize" -> {
            val r = payload.decode<AuthorizeRequest>()
            log.info("{} Authorize {}", s.id, r.idToken.idToken)
            AuthorizeResponse(IdTokenInfo(if (r.idToken.idToken == "BAD") "Blocked" else "Accepted")).json()
        }
        "MeterValues" -> empty
        "TransactionEvent" -> {
            val r = payload.decode<TransactionEventRequest>()
            val tx = r.transactionInfo.transactionId
            log.info("{} TransactionEvent {} tx={} seq={} trigger={}", s.id, r.eventType, tx, r.seqNo, r.triggerReason)
            when (r.eventType) {
                "Updated" -> if (updates.merge(tx, 1, Int::plus) == STOP_AFTER_UPDATES) {
                    // Respond first; ask the station to stop from a separate coroutine.
                    scope.launch { stop(s, tx) }
                }
                "Ended" -> updates.remove(tx)
            }
            empty
        }
        else -> null
    }

    private suspend fun stop(s: Session, tx: String) {
        try {
            val res = s.request<RequestStopTransactionRequest, RequestStopTransactionResponse>(
                "RequestStopTransaction", RequestStopTransactionRequest(tx),
            )
            log.info("{} RequestStopTransaction -> {}", s.id, res.status)
        } catch (e: Exception) {
            log.error("{} RequestStopTransaction failed: {}", s.id, e.message)
        }
    }

    /** Ask a connected station to start a transaction. */
    suspend fun remoteStart(cpId: String, token: String = "REMOTE1", remoteStartId: Int = 1, evseId: Int = 1): RequestStartTransactionResponse {
        val s = sessions[cpId] ?: throw OcppCallFailed("station not connected: $cpId")
        return s.request("RequestStartTransaction", RequestStartTransactionRequest(IdToken(token, "Central"), remoteStartId, evseId))
    }
}

fun Application.csmsModule(csms: Csms = Csms()) {
    val log = LoggerFactory.getLogger("csms")
    install(WebSockets) { pingPeriod = 30.seconds }
    routing {
        // Matches only clients that offer the ocpp2.1 subprotocol.
        webSocket("/{cpId}", protocol = SUBPROTOCOL) {
            val cpId = call.parameters["cpId"]!!
            log.info("{} connected", cpId)
            try {
                coroutineScope {
                    val session = Session(cpId, this@webSocket, this, csms)
                    csms.sessions[cpId] = session
                    try {
                        for (frame in incoming) {
                            if (frame !is Frame.Text) continue
                            val text = frame.readText()
                            launch {
                                try {
                                    session.onText(text)
                                } catch (e: Exception) {
                                    log.error("{} handler error: {}", cpId, e.toString())
                                }
                            }
                        }
                    } finally {
                        csms.sessions.remove(cpId, session)
                        session.cancelPending()
                    }
                }
            } finally {
                log.info("{} disconnected", cpId)
            }
        }
    }
}
