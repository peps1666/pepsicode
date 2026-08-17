import { create } from "zustand";

type ConnectionStatus = "disconnected" | "connecting" | "connected" | "error";

interface ConnectionState {
  status: ConnectionStatus;
  port: number | null;
  error: string | null;
  ws: WebSocket | null;
  connect: (port: number) => Promise<void>;
  disconnect: () => void;
  send: (message: object) => void;
  setWS: (ws: WebSocket | null) => void;
}

let messageHandlers: ((message: any) => void)[] = [];
let nextRequestId = 1;
const pendingRequests = new Map<number, { resolve: (v: any) => void; reject: (e: any) => void }>();

export function onMessage(handler: (message: any) => void): () => void {
  messageHandlers.push(handler);
  return () => {
    messageHandlers = messageHandlers.filter((h) => h !== handler);
  };
}

export function sendRequest(ws: WebSocket | null, method: string, params: object = {}): Promise<any> {
  if (!ws || ws.readyState !== WebSocket.OPEN) {
    return Promise.reject(new Error("WebSocket not connected"));
  }
  const id = nextRequestId++;
  const message = { kind: "request", id, method, params };
  return new Promise((resolve, reject) => {
    pendingRequests.set(id, { resolve, reject });
    ws.send(JSON.stringify(message));
    // Timeout after 60s
    setTimeout(() => {
      if (pendingRequests.has(id)) {
        pendingRequests.delete(id);
        reject(new Error(`Request ${method} timed out`));
      }
    }, 60000);
  });
}

export const useConnectionStore = create<ConnectionState>((set, get) => ({
  status: "disconnected",
  port: null,
  error: null,
  ws: null,

  connect: async (port: number) => {
    set({ status: "connecting", port, error: null });
    return new Promise<void>((resolve, reject) => {
      const ws = new WebSocket(`ws://127.0.0.1:${port}`);
      const timeout = setTimeout(() => {
        reject(new Error("Connection timeout"));
        set({ status: "error", error: "Connection timeout" });
      }, 10000);

      ws.onopen = () => {
        clearTimeout(timeout);
        set({ status: "connected", ws, error: null });
        resolve();
      };

      ws.onerror = () => {
        clearTimeout(timeout);
        set({ status: "error", error: "WebSocket connection failed", ws: null });
        reject(new Error("WebSocket connection failed"));
      };

      ws.onclose = () => {
        set({ status: "disconnected", ws: null });
      };

      ws.onmessage = (event) => {
        try {
          const message = JSON.parse(event.data);
          // Route responses to pending requests
          if (message.kind === "response" && pendingRequests.has(message.id)) {
            const pending = pendingRequests.get(message.id)!;
            pendingRequests.delete(message.id);
            if (message.error) {
              pending.reject(new Error(message.error.message || "Unknown error"));
            } else {
              pending.resolve(message.result);
            }
            return;
          }
          // Dispatch events and other messages to handlers
          for (const handler of messageHandlers) {
            try {
              handler(message);
            } catch (e) {
              console.error("Message handler error:", e);
            }
          }
        } catch (e) {
          console.error("Failed to parse message:", e);
        }
      };
    });
  },

  disconnect: () => {
    const { ws } = get();
    if (ws) {
      ws.close();
    }
    set({ status: "disconnected", ws: null });
  },

  send: (message: object) => {
    const { ws } = get();
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify(message));
    }
  },

  setWS: (ws: WebSocket | null) => set({ ws }),
}));
