import { useEffect, useCallback } from "react";
import AppFrame from "./layout/AppFrame";
import ChatView from "./conversation/ChatView";
import SessionList from "./sidebar/SessionList";
import DetailsPanel from "./panels/DetailsPanel";
import { useConnectionStore } from "./stores/connection";
import { useSessionStore, type InputMode } from "./stores/session";
import PermissionPrompt from "./conversation/PermissionPrompt";

export default function App() {
  const { connect, status, error } = useConnectionStore();
  const { initialize, pendingPermission } = useSessionStore();

  useEffect(() => {
    async function init() {
      try {
        const { port, token } = await window.pepsiAPI.getServerConnection();
        await connect(port, token);
        await initialize();
      } catch (e) {
        console.error("Initialization failed:", e);
      }
    }
    init();
  }, [connect, initialize]);

  const handleSend = useCallback((input: string, mode?: InputMode) => {
    return useSessionStore.getState().runTurn(input, mode);
  }, []);

  if (status === "error" || error) {
    return (
      <div style={{ display: "flex", flexDirection: "column", alignItems: "center", justifyContent: "center", gap: 8, height: "100%" }}>
        <strong style={{ color: "var(--dsw-alias-state-error-primary)", fontSize: 14 }}>无法连接本地服务</strong>
        <span style={{ color: "var(--dsw-alias-label-secondary)", fontSize: 12 }}>{error || "未知错误"}</span>
      </div>
    );
  }

  if (status !== "connected") {
    return (
      <div style={{ display: "flex", alignItems: "center", justifyContent: "center", height: "100%", color: "var(--dsw-alias-label-secondary)", fontSize: 13 }}>
        正在连接本地服务…
      </div>
    );
  }

  return (
    <>
      <AppFrame
        sidebar={<SessionList />}
        center={(controls) => <ChatView onSend={handleSend} frameControls={controls} />}
        details={<DetailsPanel />}
      />
      {pendingPermission && <PermissionPrompt />}
    </>
  );
}
