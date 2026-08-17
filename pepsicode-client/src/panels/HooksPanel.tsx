import { useState, useEffect } from "react";
import { sendRequest, useConnectionStore } from "../stores/connection";
import styles from "./Panels.module.css";

interface HookInfo {
  id: string;
  event: string;
  action: string;
  enabled: boolean;
  source?: string;
}

export default function HooksPanel() {
  const { ws } = useConnectionStore();
  const [hooks, setHooks] = useState<HookInfo[]>([]);
  const [loading, setLoading] = useState(true);

  const refresh = async () => {
    if (!ws) return;
    try {
      const result = await sendRequest(ws, "hooks/list", {});
      setHooks(result.hooks || []);
    } catch (e) {
      console.error("Failed to load hooks:", e);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    refresh();
  }, [ws]);

  const handleReload = async () => {
    if (!ws) return;
    try {
      await sendRequest(ws, "hooks/reload", {});
      await refresh();
    } catch (e) {
      console.error("Failed to reload hooks:", e);
    }
  };

  return (
    <div className={styles.panel}>
      <section className={styles.section}>
        <div className={styles.sectionHeader}>
          <h3 className={styles.sectionTitle}>Hooks</h3>
          <button className={styles.reloadBtn} onClick={handleReload}>Reload</button>
        </div>
        {loading ? (
          <div className={styles.loading}>Loading...</div>
        ) : hooks.length === 0 ? (
          <div className={styles.empty}>
            <p>No hooks configured.</p>
            <p className={styles.hint}>
              Add hooks in <code>~/.pepsi-code/hooks/</code> or project <code>.pepsi-code/hooks/</code>.
            </p>
          </div>
        ) : (
          <div className={styles.hookList}>
            {hooks.map((h, i) => (
              <div key={i} className={styles.hookItem}>
                <div className={styles.hookHeader}>
                  <span className={styles.hookId}>{h.id || `hook-${i}`}</span>
                  <span className={`${styles.hookBadge} ${h.enabled ? styles.badgeEnabled : styles.badgeDisabled}`}>
                    {h.enabled ? "on" : "off"}
                  </span>
                </div>
                <div className={styles.hookMeta}>
                  <span className={styles.hookEvent}>{h.event}</span>
                  <span className={styles.hookAction}>{h.action}</span>
                </div>
                {h.source && <div className={styles.hookSource}>{h.source}</div>}
              </div>
            ))}
          </div>
        )}
      </section>
    </div>
  );
}
