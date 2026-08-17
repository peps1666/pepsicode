import { useEffect } from "react";
import { useSessionStore } from "../stores/session";
import styles from "./Panels.module.css";

export default function CostPanel() {
  const { costData, contextData, refreshCost, refreshContext } = useSessionStore();

  useEffect(() => {
    refreshCost();
    refreshContext();
  }, [refreshCost, refreshContext]);

  const totalCost = costData?.total_cost_usd ?? 0;
  const inputTokens = costData?.total_input_tokens ?? 0;
  const outputTokens = costData?.total_output_tokens ?? 0;
  const ctxTokens = contextData?.total_tokens ?? 0;
  const ctxPct = contextData?.usage_percentage ?? 0;
  const ctxMax = contextData?.max_tokens ?? 0;
  const ctxMsgs = contextData?.messages_count ?? 0;

  return (
    <div className={styles.panel}>
      <section className={styles.section}>
        <h3 className={styles.sectionTitle}>Context Window</h3>
        <div className={styles.progressWrap}>
          <div className={styles.progressTrack}>
            <div
              className={styles.progressFill}
              style={{ width: `${Math.min(ctxPct, 100)}%` }}
            />
          </div>
          <div className={styles.progressLabel}>
            {ctxTokens.toLocaleString()} / {ctxMax.toLocaleString()} tokens ({ctxPct.toFixed(1)}%)
          </div>
        </div>
        <div className={styles.statRow}>
          <span className={styles.statLabel}>Messages</span>
          <span className={styles.statValue}>{ctxMsgs}</span>
        </div>
      </section>

      <section className={styles.section}>
        <h3 className={styles.sectionTitle}>Token Usage</h3>
        <div className={styles.statRow}>
          <span className={styles.statLabel}>Input</span>
          <span className={styles.statValue}>{inputTokens.toLocaleString()}</span>
        </div>
        <div className={styles.statRow}>
          <span className={styles.statLabel}>Output</span>
          <span className={styles.statValue}>{outputTokens.toLocaleString()}</span>
        </div>
        <div className={styles.statRow}>
          <span className={styles.statLabel}>Total</span>
          <span className={styles.statValue}>{(inputTokens + outputTokens).toLocaleString()}</span>
        </div>
      </section>

      <section className={styles.section}>
        <h3 className={styles.sectionTitle}>Cost</h3>
        <div className={styles.bigCost}>${totalCost.toFixed(4)}</div>
        {costData?.entries?.length > 0 && (
          <div className={styles.entries}>
            {costData.entries.map((e: any, i: number) => (
              <div key={i} className={styles.entry}>
                <span className={styles.entryModel}>{e.model}</span>
                <span className={styles.entryCost}>${e.cost_usd.toFixed(4)}</span>
              </div>
            ))}
          </div>
        )}
      </section>
    </div>
  );
}
