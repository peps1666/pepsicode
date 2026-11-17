"""Unprivileged loop driver for the M2 host broker."""

import json
import sys


def main():
    start = json.loads(sys.stdin.buffer.readline(65537))
    identity = {key: start[key] for key in ("version", "run_id", "attempt_id", "generation", "nonce")}
    sequence = 0

    def send(kind, payload):
        nonlocal sequence
        sequence += 1
        print(json.dumps({**identity, "sequence": sequence, "kind": kind, "payload": payload}), flush=True)

    def request(operation, round_number):
        send("request", {"operation": operation, "arguments": {"round": round_number}})
        reply = json.loads(sys.stdin.buffer.readline(65537))
        if (
            any(reply.get(key) != value for key, value in identity.items())
            or reply.get("kind") != "response"
            or reply.get("sequence") != sequence
        ):
            raise ValueError("Host response does not match the active request")
        return reply["payload"]

    for round_number in range(1, start["payload"]["max_rounds"] + 1):
        response = request("next", round_number)
        if response["action"] == "tools":
            response = request("tools", round_number)
        if response["action"] == "done":
            send("result", {})
            return
    raise RuntimeError("Host did not settle the bounded Worker turn")


if __name__ == "__main__":
    main()
