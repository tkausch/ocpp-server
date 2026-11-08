package li.kausch.ocpp

import kotlinx.serialization.Serializable

// OCPP 2.1 payloads used by the simulator. Only the fields the CSMS needs are
// modelled; unknown fields are ignored on input.

@Serializable data class ChargingStation(val model: String, val vendorName: String)
@Serializable data class BootNotificationRequest(val chargingStation: ChargingStation, val reason: String)
@Serializable data class BootNotificationResponse(val currentTime: String, val interval: Int, val status: String)

@Serializable data class HeartbeatResponse(val currentTime: String)

@Serializable
data class StatusNotificationRequest(
    val timestamp: String,
    val connectorStatus: String,
    val evseId: Int,
    val connectorId: Int,
)

@Serializable data class IdToken(val idToken: String, val type: String)
@Serializable data class IdTokenInfo(val status: String)
@Serializable data class AuthorizeRequest(val idToken: IdToken)
@Serializable data class AuthorizeResponse(val idTokenInfo: IdTokenInfo)

@Serializable data class TransactionInfo(val transactionId: String)
@Serializable
data class TransactionEventRequest(
    val eventType: String,
    val seqNo: Int,
    val triggerReason: String,
    val transactionInfo: TransactionInfo,
)

// Server-initiated requests.
@Serializable data class RequestStopTransactionRequest(val transactionId: String)
@Serializable data class RequestStopTransactionResponse(val status: String)
@Serializable
data class RequestStartTransactionRequest(
    val idToken: IdToken,
    val remoteStartId: Int,
    val evseId: Int? = null,
)
@Serializable data class RequestStartTransactionResponse(val status: String, val transactionId: String? = null)
