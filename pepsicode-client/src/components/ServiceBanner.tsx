import styles from "./ServiceBanner.module.css";
import { useSessionStore } from "../stores/session";

/**
 * M3 resident-service banner. The service announces itself via
 * connection/ready; while attached this reminds the operator that closing
 * the window only detaches the control view — the "停止" controls stop the
 * *task* (turn/cancel), never the service. Pending merged notifications from
 * the service outbox are surfaced here instead of the chat timeline.
 */
export default function ServiceBanner() {
  const serviceInfo = useSessionStore((state) => state.serviceInfo);
  const serviceNotifications = useSessionStore((state) => state.serviceNotifications);
  if (!serviceInfo) return null;
  const generation = serviceInfo.generation.slice(0, 8);
  return (
    <div className={styles.banner} role="status" title="常驻服务已连接；关闭窗口只会断开控制端，已授权任务继续推进">
      <span className={styles.dot} aria-hidden />
      <span>
        常驻服务已连接{generation ? ` · 代次 ${generation}` : ""}
        {serviceNotifications > 0 ? ` · 待处理通知 ${serviceNotifications} 条` : " · 无待处理通知"}
      </span>
      <span className={styles.hint}>关闭窗口不会停止任务</span>
    </div>
  );
}
