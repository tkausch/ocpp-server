package li.kausch.ocpp

import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.int
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive

/** OCPP-J message frames: CALL [2], CALLRESULT [3] and CALLERROR [4]. */
sealed interface OcppFrame {
    val id: String
}

data class Call(override val id: String, val action: String, val payload: JsonObject) : OcppFrame
data class CallResult(override val id: String, val payload: JsonObject) : OcppFrame
data class CallError(
    override val id: String,
    val code: String,
    val description: String = "",
    val details: JsonObject = JsonObject(emptyMap()),
) : OcppFrame

class FrameException(message: String, val id: String = "-1") : Exception(message)

fun parseFrame(text: String): OcppFrame {
    val arr = try {
        Json.parseToJsonElement(text).jsonArray
    } catch (e: Exception) {
        throw FrameException("not a JSON array: ${e.message}")
    }
    try {
        val id = arr[1].jsonPrimitive.content
        return when (val type = arr[0].jsonPrimitive.int) {
            2 -> Call(id, arr[2].jsonPrimitive.content, arr[3].jsonObject)
            3 -> CallResult(id, arr[2].jsonObject)
            4 -> CallError(
                id,
                arr[2].jsonPrimitive.content,
                arr.getOrNull(3)?.jsonPrimitive?.content ?: "",
                arr.getOrNull(4)?.jsonObject ?: JsonObject(emptyMap()),
            )
            else -> throw FrameException("unknown message type $type", id)
        }
    } catch (e: FrameException) {
        throw e
    } catch (e: Exception) {
        val id = runCatching { arr[1].jsonPrimitive.content }.getOrDefault("-1")
        throw FrameException("malformed frame: ${e.message}", id)
    }
}

fun OcppFrame.encode(): String {
    val elements: List<JsonElement> = when (this) {
        is Call -> listOf(JsonPrimitive(2), JsonPrimitive(id), JsonPrimitive(action), payload)
        is CallResult -> listOf(JsonPrimitive(3), JsonPrimitive(id), payload)
        is CallError -> listOf(JsonPrimitive(4), JsonPrimitive(id), JsonPrimitive(code), JsonPrimitive(description), details)
    }
    return JsonArray(elements).toString()
}
