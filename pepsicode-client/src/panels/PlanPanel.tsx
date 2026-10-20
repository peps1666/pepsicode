import { useSessionStore } from "../stores/session";
import styles from "./Panels.module.css";

export default function PlanPanel() {
  const { planMode, planFile, enterPlanMode, exitPlanMode } = useSessionStore();

  return (
    <div className={styles.panel}>
      <section className={styles.section}>
        <h3 className={styles.sectionTitle}>计划模式</h3>
        <div className={styles.planStatus}>
          <div className={`${styles.statusDot} ${planMode ? styles.statusActive : styles.statusInactive}`} />
          <span>{planMode ? "已启用" : "未启用"}</span>
        </div>
        {planFile && (
          <div className={styles.planFile}>
            <div className={styles.statLabel}>计划文件</div>
            <code className={styles.planFilePath}>{planFile}</code>
          </div>
        )}
        <div className={styles.buttonRow}>
          {planMode ? (
            <button className={styles.actionBtn} onClick={exitPlanMode}>退出计划模式</button>
          ) : (
            <button className={styles.actionBtn} onClick={enterPlanMode}>进入计划模式</button>
          )}
        </div>
        <p className={styles.hint}>
          计划模式下代理只能读取文件、写入计划文件；计划获批前，会修改项目的工具一律被拒绝。
        </p>
      </section>
    </div>
  );
}
