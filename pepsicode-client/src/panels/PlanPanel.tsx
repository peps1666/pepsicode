import { useSessionStore } from "../stores/session";
import styles from "./Panels.module.css";

export default function PlanPanel() {
  const { planMode, planFile, enterPlanMode, exitPlanMode } = useSessionStore();

  return (
    <div className={styles.panel}>
      <section className={styles.section}>
        <h3 className={styles.sectionTitle}>Plan Mode</h3>
        <div className={styles.planStatus}>
          <div className={`${styles.statusDot} ${planMode ? styles.statusActive : styles.statusInactive}`} />
          <span>{planMode ? "Active" : "Inactive"}</span>
        </div>
        {planFile && (
          <div className={styles.planFile}>
            <div className={styles.statLabel}>Plan file</div>
            <code className={styles.planFilePath}>{planFile}</code>
          </div>
        )}
        <div className={styles.buttonRow}>
          {planMode ? (
            <button className={styles.actionBtn} onClick={exitPlanMode}>Exit Plan Mode</button>
          ) : (
            <button className={styles.actionBtn} onClick={enterPlanMode}>Enter Plan Mode</button>
          )}
        </div>
        <p className={styles.hint}>
          In Plan mode, the agent can only read files and write to the plan file.
          Destructive tools are blocked until the plan is approved.
        </p>
      </section>
    </div>
  );
}
