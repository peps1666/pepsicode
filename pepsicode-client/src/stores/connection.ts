import { create } from "zustand";

type ConnectionStatus = "disconnected" | "connecting" | "connected" | "error";

interface ConnectionState {
  status: ConnectionStatus;
  port: number | null;
  error: string | null;
  ws: WebSocket | null;
  connect: (port: number, token: string) => Promise<void>;
  disconnect: () => void;
  send: (message: object) => void;
  setWS: (ws: WebSocket | null) => void;
}

let messageHandlers: ((message: any) => void)[] = [];
let nextRequestId = 1;
let generation = 0;
let currentSocket: WebSocket | null = null;
const pendingRequests = new Map<number, { resolve: (v: any) => void; reject: (e: any) => void; ws: WebSocket; timer: ReturnType<typeof setTimeout> | null }>();

export function onMessage(handler: (message: any) => void): () => void {
  messageHandlers.push(handler);
  return () => {
    messageHandlers = messageHandlers.filter((h) => h !== handler);
  };
}

export function sendRequest(
  ws: WebSocket | null, method: string, params: object = {}, options: { timeoutMs?: number | null } = {},
): Promise<any> {
  if (!ws || ws.readyState !== WebSocket.OPEN) {
    return Promise.reject(new Error("WebSocket not connected"));
  }
  const id = nextRequestId++;
  const message = { kind: "request", id, method, params };
  return new Promise((resolve, reject) => {
    // Callers choose a deadline; null keeps a streaming task alive until its
    // response or a closed socket, including future long-running RPCs.
    const timeoutMs = options.timeoutMs === undefined ? 60000 : options.timeoutMs;
    const timer = timeoutMs === null ? null : setTimeout(() => {
      if (pendingRequests.has(id)) {
        pendingRequests.delete(id);
        reject(new Error(`Request ${method} timed out`));
      }
    }, timeoutMs);
    pendingRequests.set(id, { resolve, reject, ws, timer });
    try {
      ws.send(JSON.stringify(message));
    } catch (error) {
      pendingRequests.delete(id);
      if (timer !== null) clearTimeout(timer);
      reject(error);
    }
  });
}

export const useConnectionStore = create<ConnectionState>((set, get) => ({
  status: "disconnected",
  port: null,
  error: null,
  ws: null,

  connect: async (port: number, token: string) => {
    if (!token) throw new Error("Connection token is required");
    const attempt = ++generation;
    currentSocket?.close();
    const ws = new WebSocket(`ws://127.0.0.1:${port}`);
    currentSocket = ws;
    set({ status: "connecting", port, error: null, ws: null });
    return new Promise<void>((resolve, reject) => {
      let authenticated = false;
      const active = () => attempt === generation && currentSocket === ws;
      const fail = (error: Error) => {
        clearTimeout(timeout);
        if (active()) set({ status: "error", error: error.message, ws: null });
        reject(error);
        ws.close();
      };
      const timeout = setTimeout(() => fail(new Error("Connection authentication timed out")), 10000);

      ws.onopen = () => {
        if (!active()) return;
        try {
          ws.send(JSON.stringify({ kind: "request", id: 0, method: "connection/auth", params: { token } }));
        } catch {
          fail(new Error("Connection authentication failed"));
        }
      };
      ws.onerror = () => {
        if (active()) fail(new Error("WebSocket connection failed"));
      };
      ws.onclose = () => {
        clearTimeout(timeout);
        for (const [id, pending] of pendingRequests) {
          if (pending.ws === ws) {
            if (pending.timer !== null) clearTimeout(pending.timer);
            pendingRequests.delete(id);
            pending.reject(new Error("WebSocket connection closed"));
          }
        }
        if (active()) {
          currentSocket = null;
          if (get().status !== "error") set({ status: "disconnected", ws: null });
        }
        reject(new Error("WebSocket connection closed"));
      };
      ws.onmessage = (event) => {
        if (!active()) return;
        try {
          const message = JSON.parse(event.data);
          if (!authenticated) {
            if (message.kind !== "response" || message.id !== 0) return;
            if (message.error || message.result?.authenticated !== true) {
              fail(new Error("Connection authentication failed"));
              return;
            }
            authenticated = true;
            clearTimeout(timeout);
            set({ status: "connected", ws, error: null });
            resolve();
            return;
          }
          if (message.kind === "response" && pendingRequests.has(message.id)) {
            const pending = pendingRequests.get(message.id)!;
            if (pending.ws !== ws) return;
            pendingRequests.delete(message.id);
            if (pending.timer !== null) clearTimeout(pending.timer);
            if (message.error) pending.reject(new Error(message.error.message || "Unknown error"));
            else pending.resolve(message.result);
            return;
          }
          for (const handler of messageHandlers) {
            try { handler(message); }
            catch (error) { console.error("Message handler error:", error); }
          }
        } catch (error) {
          console.error("Failed to parse message:", error);
        }
      };
    });
  },

  disconnect: () => {
    ++generation;
    currentSocket?.close();
    currentSocket = null;
    set({ status: "disconnected", ws: null, error: null });
  },

  send: (message: object) => {
    const { ws } = get();
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify(message));
    }
  },

  setWS: (ws: WebSocket | null) => set({ ws }),
}));
