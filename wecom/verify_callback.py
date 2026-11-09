#!/usr/bin/env python3
"""One-off server that passes WeCom's "receive message URL" check, so the VPS IP can be set as trusted.

WeCom sends GET ?msg_signature&timestamp&nonce&echostr; we check the signature with WECOM_TOKEN,
decrypt echostr with WECOM_AES_KEY and answer the plain text. Stop it once the trusted IP is saved.
Usage: python3 verify_callback.py [port]   (reads ~/x-daily-wecom/.env on every request)
"""
import base64
import hashlib
import http.server
import subprocess
import sys
import urllib.parse
from pathlib import Path

ENV = Path.home() / "x-daily-wecom" / ".env"


def settings():
    values = {}
    for line in ENV.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip()
    return values


def decrypt(echostr, aes_key, corp_id):
    key = base64.b64decode(aes_key + "=")
    raw = subprocess.run(["openssl", "enc", "-d", "-aes-256-cbc", "-nopad", "-K", key.hex(), "-iv", key[:16].hex()],
                         input=base64.b64decode(echostr), capture_output=True, check=True).stdout
    raw = raw[:-raw[-1]]  # PKCS#7 padding (block size 32)
    length = int.from_bytes(raw[16:20], "big")
    message, receiver = raw[20:20 + length], raw[20 + length:].decode()
    if corp_id and receiver != corp_id:
        raise ValueError("corp id in the message does not match WECOM_CORP_ID")
    return message


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(self.path).query))
        try:
            conf = settings()
            parts = sorted([conf["WECOM_TOKEN"], query["timestamp"], query["nonce"], query["echostr"]])
            if hashlib.sha1("".join(parts).encode()).hexdigest() != query["msg_signature"]:
                raise ValueError("signature mismatch: check WECOM_TOKEN")
            body = decrypt(query["echostr"], conf["WECOM_AES_KEY"], conf.get("WECOM_CORP_ID"))
            self.send_response(200)
        except Exception as error:  # Report the reason in our log only, never the secrets.
            print(f"verification failed: {type(error).__name__}: {error}", flush=True)
            body = b"error"
            self.send_response(400)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        print(f"{self.address_string()} {fmt % args}"[:200], flush=True)


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8088
    print(f"listening on :{port}", flush=True)
    http.server.ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
