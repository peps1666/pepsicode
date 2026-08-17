import { useState, useCallback } from "react";
import { useSessionStore } from "../stores/session";
import styles from "./PermissionPrompt.module.css";

export default function PermissionPrompt() {
  const { pendingPermission, approvePermission } = useSessionStore();
  const [feedback, setFeedback] = useState("");
  const [showFeedback, setShowFeedback] = useState(false);

  const handleChoice = useCallback(
    (decision: string) => {
      if (decision === "deny_with_feedback") {
        setShowFeedback(true);
        return;
      }
      approvePermission(decision);
      setFeedback("");
      setShowFeedback(false);
    },
    [approvePermission]
  );

  const handleSubmitFeedback = useCallback(() => {
    approvePermission("deny_with_feedback", feedback);
    setFeedback("");
    setShowFeedback(false);
  }, [approvePermission, feedback]);

  if (!pendingPermission) return null;

  const isDangerous = pendingPermission.kind === "command";
  const icon = pendingPermission.kind === "command" ? "⚠" : pendingPermission.kind === "edit" ? "✎" : "?";

  return (
    <div className={styles.overlay}>
      <div className={`${styles.modal} ${isDangerous ? styles.dangerous : ""}`}>
        <div className={styles.header}>
          <span className={styles.icon}>{icon}</span>
          <span className={styles.title}>{pendingPermission.summary}</span>
        </div>
        <div className={styles.body}>
          <div className={styles.details}>
            {pendingPermission.details.map((line, i) => (
              <div key={i} className={styles.detailLine}>{line}</div>
            ))}
          </div>
          {showFeedback && (
            <div className={styles.feedbackSection}>
              <textarea
                className={styles.feedbackInput}
                placeholder="Provide feedback to the model..."
                value={feedback}
                onChange={(e) => setFeedback(e.target.value)}
                autoFocus
                rows={3}
              />
              <div className={styles.feedbackActions}>
                <button className={styles.cancelBtn} onClick={() => setShowFeedback(false)}>Cancel</button>
                <button className={styles.submitBtn} onClick={handleSubmitFeedback} disabled={!feedback.trim()}>
                  Send feedback
                </button>
              </div>
            </div>
          )}
        </div>
        {!showFeedback && (
          <div className={styles.actions}>
            {pendingPermission.choices.map((choice) => {
              const isDeny = choice.decision.startsWith("deny");
              const isAllow = choice.decision.startsWith("allow");
              const className = isDeny
                ? styles.denyBtn
                : isAllow
                ? styles.allowBtn
                : styles.neutralBtn;
              return (
                <button
                  key={choice.key}
                  className={className}
                  onClick={() => handleChoice(choice.decision)}
                >
                  {choice.label}
                </button>
              );
            })}
          </div>
        )}
      </div>
    </div>
  );
}
