"""Launch Agent Liftoff on the requested port or the next available one."""

from __future__ import annotations

import os
import socket

import uvicorn


def bind_available(host: str, preferred_port: int) -> tuple[socket.socket, int]:
    """Bind the preferred port, then nearby ports, then an OS-assigned port."""
    for port in range(preferred_port, min(preferred_port + 100, 65_536)):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, port))
        except OSError:
            sock.close()
            continue
        sock.listen(2048)
        sock.setblocking(False)
        return sock, port

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind((host, 0))
    sock.listen(2048)
    sock.setblocking(False)
    return sock, int(sock.getsockname()[1])


def main() -> None:
    host = os.environ.get("WHEATEAR_HOST", "127.0.0.1")
    try:
        preferred = int(os.environ.get("PORT", "8080"))
    except ValueError:
        preferred = 8080
    sock, port = bind_available(host, preferred)
    display_host = "localhost" if host in {"0.0.0.0", "127.0.0.1"} else host
    print(f"Agent Liftoff: http://{display_host}:{port}", flush=True)

    config = uvicorn.Config(
        "wheatear.service.app:create_app",
        host=host,
        port=port,
        log_level=os.environ.get("WHEATEAR_LOG_LEVEL", "info"),
        proxy_headers=True,
        factory=True,
    )
    try:
        uvicorn.Server(config).run(sockets=[sock])
    except KeyboardInterrupt:
        pass
    finally:
        sock.close()


if __name__ == "__main__":
    main()
