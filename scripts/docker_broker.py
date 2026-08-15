#!/usr/bin/env python3
"""Allowlisted Docker lifecycle broker for PlexMind sidecars."""
import http.client
import json
import os
import secrets
import socket
import struct
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

SOCKET = os.getenv("DOCKER_SOCKET", "/var/run/docker.sock")
TOKEN = os.getenv("PLEXMIND_BROKER_TOKEN", "")
ALLOWED = {x for x in os.getenv("PLEXMIND_ALLOWED_CONTAINERS", "whisper-asr-webservice,llama-cpp").split(",") if x}
if not TOKEN:
    raise RuntimeError("PLEXMIND_BROKER_TOKEN is required")


class UnixConnection(http.client.HTTPConnection):
    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect(SOCKET)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        return

    def reply(self, code, body=b""):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def authorized(self):
        supplied = self.headers.get("X-Broker-Token", "")
        return bool(TOKEN and supplied and secrets.compare_digest(TOKEN, supplied))

    def proxy(self, method):
        if not self.authorized():
            return self.reply(403, b'{"detail":"forbidden"}')
        parts = [p for p in urlparse(self.path).path.split("/") if p]
        if len(parts) not in (2, 3) or parts[0] != "containers" or parts[1] not in ALLOWED:
            return self.reply(404, b'{"detail":"not allowed"}')
        if method == "GET" and len(parts) == 3 and parts[2] == "gpu":
            return self.gpu(parts[1])
        if method == "GET" and len(parts) == 3 and parts[2] == "json":
            docker_path = f"/containers/{parts[1]}/json"
        elif method == "POST" and len(parts) == 3 and parts[2] in ("start", "stop"):
            docker_path = f"/containers/{parts[1]}/{parts[2]}"
        else:
            return self.reply(405, b'{"detail":"method not allowed"}')
        conn = UnixConnection("localhost", timeout=10)
        try:
            conn.request(method, docker_path)
            response = conn.getresponse()
            body = response.read()
            self.reply(response.status, body)
        except OSError as exc:
            self.reply(502, json.dumps({"detail": str(exc)}).encode())
        finally:
            conn.close()

    def docker_request(self, method, path, body=None):
        conn = UnixConnection("localhost", timeout=10)
        try:
            headers = {"Content-Type": "application/json"} if body is not None else {}
            conn.request(method, path, body=body, headers=headers)
            response = conn.getresponse()
            return response.status, response.read()
        finally:
            conn.close()

    @staticmethod
    def docker_stream(raw):
        chunks = []
        pos = 0
        while pos + 8 <= len(raw):
            size = struct.unpack(">I", raw[pos + 4:pos + 8])[0]
            end = pos + 8 + size
            if raw[pos] not in (1, 2) or raw[pos + 1:pos + 4] != b"\0\0\0" or end > len(raw):
                break
            chunks.append(raw[pos + 8:end])
            pos = end
        return b"".join(chunks) if chunks else raw

    def gpu(self, container):
        command = ["nvidia-smi", "--query-gpu=name,utilization.gpu,memory.total,memory.free", "--format=csv,noheader,nounits"]
        try:
            code, created = self.docker_request(
                "POST", f"/containers/{container}/exec",
                json.dumps({"AttachStdout": True, "AttachStderr": True, "Cmd": command}),
            )
            exec_id = (json.loads(created or b"{}") or {}).get("Id")
            if code != 201 or not exec_id:
                return self.reply(502, b'{"detail":"GPU probe exec creation failed"}')
            code, raw = self.docker_request(
                "POST", f"/exec/{exec_id}/start", '{"Detach":false,"Tty":false}'
            )
            output = self.docker_stream(raw).decode("utf-8", errors="replace")
            devices = []
            for line in output.splitlines():
                try:
                    name, utilization, memory_total, memory_free = [part.strip() for part in line.split(",", 3)]
                    devices.append({"name": name, "utilization_pct": float(utilization),
                                    "memory_total_mb": float(memory_total), "memory_free_mb": float(memory_free)})
                except (ValueError, TypeError):
                    pass
            if code != 200 or not devices:
                return self.reply(502, b'{"detail":"GPU probe returned no utilization"}')
            body = json.dumps({
                "vendor": "nvidia",
                "name": devices[0]["name"],
                "utilization_pct": int(sum(item["utilization_pct"] for item in devices) / len(devices)),
                "memory_total_mb": int(sum(item["memory_total_mb"] for item in devices)),
                "memory_free_mb": int(sum(item["memory_free_mb"] for item in devices)),
            }).encode()
            return self.reply(200, body)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            return self.reply(502, json.dumps({"detail": str(exc)}).encode())

    def do_GET(self): self.proxy("GET")
    def do_POST(self): self.proxy("POST")


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 9020), Handler).serve_forever()
