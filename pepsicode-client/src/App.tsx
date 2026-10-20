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
      <div style={{ display: "flex", alignItems: "center", justifyContent: "center", height: "100%", color: "var(--dsw-alias-state-error-primary)" }}>
        Connection error: {error || "Unknown error"}
      </div>
    );
  }

  if (status !== "connected") {
    return (
      <div style={{ display: "flex", alignItems: "center", justifyContent: "center", height: "100%", color: "var(--dsw-alias-label-secondary)" }}>
        Connecting to pepsicode server...
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
