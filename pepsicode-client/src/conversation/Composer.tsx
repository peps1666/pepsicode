import { useState, useRef, useCallback, type KeyboardEvent } from "react";
import styles from "./Composer.module.css";
import { useSessionStore } from "../stores/session";

const accessModes = {
  read_only: { label: "只读", description: "查看文件和搜索，禁止编辑及命令执行。" },
  auto: { label: "自动", description: "项目内编辑免确认；越界、危险命令及外部写入仍需确认。" },
  full_access: { label: "完全访问", description: "编辑、命令和外部工具均免确认，可访问项目外的文件。" },
};

interface ComposerProps {
  onSend: (input: string) => void;
  disabled: boolean;
}

export default function Composer({ onSend, disabled }: ComposerProps) {
  const [input, setInput] = useState("");
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const { accessMode, permissionUpdating, updatePermissions, cacheHitPercentage, contextData } = useSessionStore();
  const contextPercent = contextData?.max_tokens ? contextData.usage_percentage : null;
  const estimated = contextData?.is_estimate || contextData?.window_is_estimate;
  const [permissionError, setPermissionError] = useState("");
  const [permissionsOpen, setPermissionsOpen] = useState(false);

  const handleSend = useCallback(() => {
    const trimmed = input.trim();
    if (!trimmed || disabled) return;
    if (trimmed === "/permissions") {
      setPermissionsOpen(true);
      setInput("");
      return;
    }
    onSend(trimmed);
    setInput("");
    if (textareaRef.current) {
      textareaRef.current.style.height = "auto";
    }
  }, [input, disabled, onSend]);

  const handleKeyDown = useCallback(
    (e: KeyboardEvent<HTMLTextAreaElement>) => {
      if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        handleSend();
      }
    },
    [handleSend]
  );

  const handleInput = useCallback((e: React.ChangeEvent<HTMLTextAreaElement>) => {
    setInput(e.target.value);
    const el = e.target;
    el.style.height = "auto";
    el.style.height = Math.min(el.scrollHeight, 200) + "px";
  }, []);

  return (
    <div className={styles.container}>
      <div className={styles.card}>
        <textarea
          ref={textareaRef}
          className={styles.textarea}
          value={input}
          onChange={handleInput}
          onKeyDown={handleKeyDown}
          placeholder={disabled ? "Running..." : "Ask anything, or type / for commands"}
          disabled={disabled}
          rows={1}
          autoFocus
        />
        <div className={styles.row}>
          <details className={styles.accessMode} open={permissionsOpen}
            onToggle={(event) => setPermissionsOpen(event.currentTarget.open)}
            onKeyDown={(event) => {
              if (event.key === "Escape") setPermissionsOpen(false);
            }}>
            <summary aria-label="权限模式" aria-disabled={disabled || permissionUpdating}
              onClick={(event) => { if (disabled || permissionUpdating) event.preventDefault(); }}>
              权限：{accessModes[accessMode].label} ▾
            </summary>
            <div className={styles.permissionOptions} role="group" aria-label="选择权限模式">
              {Object.entries(accessModes).map(([value, mode]) => (
                <button key={value} type="button" aria-pressed={value === accessMode}
                  disabled={disabled || permissionUpdating}
                  onClick={() => {
                    setPermissionError("");
                    void updatePermissions(value as keyof typeof accessModes)
                      .then(() => setPermissionsOpen(false))
                      .catch((error) => setPermissionError(error instanceof Error ? error.message : String(error)));
                  }}>
                  <span>{mode.label}{value === accessMode ? " √" : ""}</span>
                  <small>{mode.description}</small>
                </button>
              ))}
            </div>
          </details>
          <button className={styles.sendButton} onClick={handleSend} disabled={disabled || !input.trim()}>
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <path d="M12 19V5M5 12l7-7 7 7" />
            </svg>
          </button>
        </div>
      </div>
      <div className={styles.usageStatus} aria-label="实时用量">
        <span title="最近一次模型请求：缓存读取 token / 总输入 token；未提供表示接口没有返回缓存数据。">
          缓存命中 {cacheHitPercentage === null ? "未提供" : `${cacheHitPercentage.toFixed(1)}%`}
        </span>
        <span title={contextPercent === null ? "上下文窗口数据未提供" : `${contextData.total_tokens.toLocaleString()} / ${contextData.max_tokens.toLocaleString()} tokens${estimated ? "（估算）" : ""}`}>
          上下文 {contextPercent === null ? "未提供" : `${estimated ? "约 " : ""}${contextPercent.toFixed(1)}%`}
        </span>
        {contextPercent !== null && (
          <progress aria-label="上下文窗口占比" max={100} value={Math.max(0, Math.min(100, contextPercent))} />
        )}
      </div>
      <div className={styles.modeHint} role={permissionError ? "alert" : undefined}>
        {permissionError || accessModes[accessMode].description}
      </div>
      <div className={styles.hint}>Enter to send • Shift+Enter for new line</div>
    </div>
  );
}
