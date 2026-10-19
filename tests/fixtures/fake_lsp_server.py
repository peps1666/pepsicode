"""Tiny real stdio server for testing framing, document sync and workspace edits."""

import json
import sys

documents = {}
root = None


def send(message):
    payload = json.dumps({"jsonrpc": "2.0", **message}, ensure_ascii=False).encode()
    sys.stdout.buffer.write(f"Content-Length: {len(payload)}\r\n\r\n".encode() + payload)
    sys.stdout.buffer.flush()


while True:
    headers = {}
    while True:
        line = sys.stdin.buffer.readline()
        if not line:
            sys.exit(0)
        if line == b"\r\n":
            break
        key, value = line.decode().split(":", 1)
        headers[key.lower()] = value.strip()
    message = json.loads(sys.stdin.buffer.read(int(headers["content-length"])))
    method, params = message.get("method"), message.get("params", {})
    result = None
    if method == "initialize":
        root = params["rootUri"]
        result = {
            "capabilities": {
                "positionEncoding": "utf-16",
                "definitionProvider": True,
                "referencesProvider": True,
                "renameProvider": True,
                "documentSymbolProvider": True,
                "experimental": {"initializationOptions": params.get("initializationOptions")},
            }
        }
    elif method == "textDocument/didOpen":
        document = params["textDocument"]
        documents[document["uri"]] = document
        send(
            {
                "method": "textDocument/publishDiagnostics",
                "params": {
                    "uri": document["uri"],
                    "version": document["version"],
                    "diagnostics": [
                        {
                            "message": document["text"].strip(),
                            "severity": 1,
                            "range": {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 1}},
                        }
                    ],
                },
            }
        )
    elif method == "textDocument/didChange":
        document = params["textDocument"]
        documents[document["uri"]] = {
            **documents[document["uri"]],
            **document,
            "text": params["contentChanges"][0]["text"],
        }
        send(
            {
                "method": "textDocument/publishDiagnostics",
                "params": {"uri": document["uri"], "version": document["version"], "diagnostics": []},
            }
        )
    elif method == "textDocument/documentSymbol":
        result = [
            {
                "name": documents[params["textDocument"]["uri"]]["text"].strip(),
                "detail": documents[params["textDocument"]["uri"]]["languageId"],
                "kind": 12,
                "range": {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 3}},
                "selectionRange": {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 3}},
            }
        ]
    elif method in {"textDocument/definition", "textDocument/references"}:
        result = [
            {"uri": params["textDocument"]["uri"], "range": {"start": params["position"], "end": params["position"]}}
        ]
    elif method == "textDocument/rename":
        result = {
            "changes": {
                params["textDocument"]["uri"]: [
                    {
                        "range": {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 3}},
                        "newText": params["newName"],
                    }
                ],
                root + "/use.py": [
                    {
                        "range": {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 3}},
                        "newText": params["newName"],
                    }
                ],
            }
        }
    if "id" in message:
        send({"id": message["id"], "result": result})
