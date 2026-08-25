import { useState, useCallback } from "react";
import { useSessionStore } from "../stores/session";
import styles from "./SessionList.module.css";

function shortenPath(p: string): string {
  if (!p) return "";
  const parts = p.replace(/\\/g, "/").split("/").filter(Boolean);
  if (parts.length <= 2) return p;
  return ".../" + parts.slice(-2).join("/");
}

export default function SessionList() {
  const {
    sessionId,
    cwd,
    sessions,
    sessionsLoading,
    isRunning,
    createSession,
    resumeSession,
    switchCwd,
  } = useSessionStore();
  const [switching, setSwitching] = useState(false);

  const handleNew = useCallback(() => {
    void createSession();
  }, [createSession]);

  const handleResume = useCallback(
    (sid: string) => {
      resumeSession(sid);
    },
    [resumeSession]
  );

  const handleSelectFolder = useCallback(async () => {
    if (switching) return;
    try {
      setSwitching(true);
      const folder = await window.pepsiAPI.selectFolder();
      if (!folder) return;
      await switchCwd(folder);
    } catch (e) {
      console.error("Failed to switch project folder:", e);
    } finally {
      setSwitching(false);
    }
  }, [switchCwd, switching]);

  return (
    <div className={styles.container}>
      <div className={styles.header}>
        <span className={styles.title}>Sessions</span>
        <button className={styles.newBtn} onClick={handleNew} disabled={isRunning} title="New session">
          +
        </button>
      </div>

      <div className={styles.projectBar}>
        <button
          className={styles.folderBtn}
          onClick={handleSelectFolder}
          disabled={switching || isRunning}
          title={cwd || "选择项目文件夹"}
        >
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
            <path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z" />
          </svg>
          <span className={styles.folderText}>
            {switching ? "切换中..." : cwd ? shortenPath(cwd) : "选择项目文件夹"}
          </span>
        </button>
      </div>

      <div className={styles.list}>
        {sessionsLoading ? (
          <div className={styles.loading}>Loading...</div>
        ) : sessions.length === 0 ? (
          <div className={styles.empty}>No saved sessions</div>
        ) : (
          sessions.map((s) => (
            <div
              key={s.session_id}
              className={`${styles.item} ${s.session_id === sessionId ? styles.active : ""}`}
              onClick={() => handleResume(s.session_id)}
            >
              <div className={styles.itemFirst}>{s.first_message || "(empty)"}</div>
              <div className={styles.itemMeta}>
                <span>{new Date(s.updated_at * 1000).toLocaleString()}</span>
                <span>{s.message_count} msgs</span>
              </div>
            </div>
          ))
        )}
      </div>
    </div>
  );
}
