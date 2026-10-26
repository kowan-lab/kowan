"""End-to-end demo of the token tunnel over a REAL WebSocket.

Runs a control-plane server and one agent in the same process. The agent
dials out (atos / edge mode), then the control-plane opens a stream and
drives an "op" on the agent over the single agent-dialed WebSocket,
multiplexed by the in-house mux. Uses aiohttp (a core dependency) for both
the server and the client side.

    python3 examples/ws_demo.py
"""

import asyncio

from aiohttp import web
import aiohttp

from kowan.transport.aiohttp_ws import AiohttpWSTransport
from kowan.tunnel.endpoint import establish_server_side, establish_agent_side
from kowan.tunnel.hub import Hub

HOST_TOKEN = "demo-token"
HOST_ID = "host-1"
PORT = 8779


def verify(token):
    return HOST_ID if token == HOST_TOKEN else None


async def agent_ops(stream):
    """The agent's host-ops handler (stands in for Docker calls)."""
    req = (await stream.readall()).decode()
    print(f"  [agent]  received op:   {req!r}")
    await stream.write(f"result of {req}".encode())
    await stream.close()


async def _serve_agent_ops(session):
    """Accept-loop: run agent_ops for each stream the control-plane opens."""
    while True:
        stream = await session.accept_stream()
        if stream is None:
            break
        asyncio.ensure_future(agent_ops(stream))


async def main():
    hub = Hub()
    ready = asyncio.Event()
    stop = asyncio.Event()

    async def server_handler(request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        transport = AiohttpWSTransport(ws)
        try:
            host_id, session = await establish_server_side(
                transport, dialed=False, verify=verify)
        except Exception as e:
            print("  [server] handshake rejected:", e)
            return ws
        print(f"  [server] agent connected: {host_id}")
        await hub.register(host_id, session)
        ready.set()
        await stop.wait()  # keep the WS handler alive for the session's life
        return ws

    app = web.Application()
    app.router.add_get("/", server_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "localhost", PORT)
    await site.start()

    async def agent():
        async with aiohttp.ClientSession() as sess, sess.ws_connect(
            f"ws://localhost:{PORT}/", max_msg_size=0
        ) as ws:
            transport = AiohttpWSTransport(ws)
            session = await establish_agent_side(
                transport, dialed=True, token=HOST_TOKEN)
            print("  [agent]  tunnel up (atos: agent dialed out, sent token)")
            serve = asyncio.ensure_future(_serve_agent_ops(session))
            await stop.wait()
            serve.cancel()

    agent_task = asyncio.ensure_future(agent())
    await asyncio.wait_for(ready.wait(), timeout=5)

    print("\n--- control-plane -> agent (host op over the tunnel) ---")
    st = await hub.dial(HOST_ID)
    await st.write(b"docker ps")
    await st.close()
    print(f"  [server] op response:   {(await st.readall()).decode()!r}")

    stop.set()
    agent_task.cancel()
    await runner.cleanup()
    print("\nOK: token tunnel over a real WebSocket works.")


if __name__ == "__main__":
    asyncio.run(main())
