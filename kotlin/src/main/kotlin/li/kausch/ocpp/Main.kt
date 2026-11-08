package li.kausch.ocpp

import io.ktor.server.engine.embeddedServer
import io.ktor.server.netty.Netty

/** Minimal OCPP 2.1 central system, a Kotlin port of csms.py. Usage: [port] (default 9000). */
fun main(args: Array<String>) {
    val port = args.firstOrNull()?.toInt() ?: 9000
    embeddedServer(Netty, port = port, host = "0.0.0.0") { csmsModule() }.start(wait = true)
}
