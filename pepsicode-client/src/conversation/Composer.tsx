import { useState, useRef, useCallback, useEffect, type KeyboardEvent } from "react";
import styles from "./Composer.module.css";
import { useSessionStore, type InputMode } from "../stores/session";
import { commandDraft, matchingCommands, type SlashCommandInfo } from "./commandCompletion";
import Icon from "../components/Icon";
import Select from "../components/Select";
import { effortLabels } from "./ModelSettings";
import PermissionPrompt from "./PermissionPrompt";

const accessModes = {
  read_only: { label: "只读", description: "查看文件和搜索，禁止编辑及命令执行。" },
  auto: { label: "自动", description: "项目内编辑免确认；本地命令、项目外访问和外部工具需确认。" },
  trust_commands: { label: "命令免确认（高风险）", description: "所有程序以当前用户权限运行，可访问项目外文件和网络；内置工具访问项目外及外部工具仍需确认。" },
  full_access: { label: "完全访问", description: "编辑、命令和外部工具均免确认，可访问项目外的文件。" },
};

interface ComposerProps {
  onSend: (input: string, mode?: InputMode) => Promise<void>;
  disabled: boolean;
  running: boolean;
  onOpenSettings: () => void;
}

export default function Composer({ onSend, disabled, running, onOpenSettings }: ComposerProps) {
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const permissionsRef = useRef<HTMLDetailsElement>(null);
  const { accessMode, permissionUpdating, updatePermissions, setWorkspaceDefaultPermissions, cacheHitPercentage, latestUsage, contextData, pendingInputs, draft: input, setDraft: setInput, removeQueuedInput, cancelTurn, isStopping } = useSessionStore();
  const [inputMode, setInputMode] = useState<InputMode>("steer");
  const [submitting, setSubmitting] = useState(false);
  const [sendError, setSendError] = useState("");
  const contextPercent = contextData?.max_tokens ? contextData.usage_percentage : null;
  const estimated = contextData?.is_estimate || contextData?.window_is_estimate;
  const [permissionError, setPermissionError] = useState("");
  const [permissionNotice, setPermissionNotice] = useState("");
  const [permissionsOpen, setPermissionsOpen] = useState(false);
  const { commands, refreshCommands, commandDiagnostics, commandError, cwd } = useSessionStore();
  const { model, models, reasoningEffort, isMock } = useSessionStore();
  const modelName = models.find((item) => item.id === model)?.name || model;
  const [selectedCommand, setSelectedCommand] = useState(0);
  const [commandMenuOpen, setCommandMenuOpen] = useState(true);
  const isCommandDraft = /^\/[^\s]*$/.test(input);
  const commandOptions = commandMenuOpen ? matchingCommands(commands, input) : [];
  const activeCommand = Math.min(selectedCommand, Math.max(0, commandOptions.length - 1));

  useEffect(() => {
    if (!permissionsOpen) return;
    const closeOnOutsideClick = (event: PointerEvent) => {
      if (!permissionsRef.current?.contains(event.target as Node)) setPermissionsOpen(false);
    };
    document.addEventListener("pointerdown", closeOnOutsideClick);
    return () => document.removeEventListener("pointerdown", closeOnOutsideClick);
  }, [permissionsOpen]);

  useEffect(() => {
    if (isCommandDraft) void refreshCommands();
  }, [isCommandDraft, cwd, refreshCommands]);

  const completeCommand = useCallback((command: SlashCommandInfo) => {
    setInput(commandDraft(command));
    setCommandMenuOpen(false);
    textareaRef.current?.focus();
  }, [setInput]);

  const handleSend = useCallback(async (mode: InputMode = inputMode) => {
    const trimmed = input.trim();
    if (!trimmed || disabled || submitting || isStopping) return;
    if (trimmed === "/permissions" && !running) {
      setPermissionsOpen(true);
      setInput("");
      return;
    }
    setSubmitting(true);
    setSendError("");
    setInput("");
    try {
      await onSend(trimmed, mode);
      if (textareaRef.current) textareaRef.current.style.height = "auto";
    } catch (error) {
      const draft = useSessionStore.getState().draft;
      setInput([input, ...(draft ? [draft] : [])].join("\n\n"));
      setSendError(error instanceof Error ? error.message : String(error));
    } finally {
      setSubmitting(false);
    }
  }, [input, disabled, submitting, isStopping, running, inputMode, setInput, onSend]);

  const handleKeyDown = useCallback(
    (e: KeyboardEvent<HTMLTextAreaElement>) => {
      if (e.nativeEvent.isComposing || e.keyCode === 229) return;
      if (e.key === "Escape" && commandMenuOpen) {
        setCommandMenuOpen(false);
        return;
      }
      if (commandOptions.length > 0) {
        if (e.key === "ArrowDown" || e.key === "ArrowUp") {
          e.preventDefault();
          setSelectedCommand((activeCommand + (e.key === "ArrowDown" ? 1 : -1) + commandOptions.length) % commandOptions.length);
          return;
        }
        if (e.key === "Tab" || (e.key === "Enter" && !e.shiftKey && input !== commandOptions[activeCommand].name)) {
          e.preventDefault();
          completeCommand(commandOptions[activeCommand]);
          return;
        }
      }
      if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        void handleSend(e.altKey ? "follow_up" : inputMode);
      }
    },
    [handleSend, inputMode, input, commandMenuOpen, commandOptions, activeCommand, completeCommand]
  );

  const handleInput = useCallback((e: React.ChangeEvent<HTMLTextAreaElement>) => {
    setInput(e.target.value);
    setCommandMenuOpen(true);
    setSelectedCommand(0);
    const el = e.target;
    el.style.height = "auto";
    el.style.height = Math.min(el.scrollHeight, 200) + "px";
  }, [setInput]);

  return (
    <div className={styles.container}>
      {pendingInputs.length > 0 && (
        <ul className={styles.queue} aria-label="待处理输入">
          {pendingInputs.map((item) => (
            <li key={item.id}>
              <span title={item.display_content || item.content}><b>{item.mode === "steer" ? "补充指令" : "后续任务"}</b> {item.display_content || item.content}</span>
              <button type="button" onClick={() => {
                void removeQueuedInput(item.id).catch((error) => setSendError(String(error)));
              }} aria-label={`取回：${item.display_content || item.content}`}>取回</button>
            </li>
          ))}
        </ul>
      )}
      {isCommandDraft && commandMenuOpen && (
        <div className={styles.commandMenu}>
          <div className={styles.commandHeader}><span>命令</span>
            <button type="button" onClick={() => { void refreshCommands(true); }}>刷新</button>
          </div>
          <ul role="listbox" id="prompt-commands" aria-label="命令补全">
            {commandOptions.map((command, index) => (
              <li key={command.name} role="option" id={`prompt-command-${index}`} aria-selected={index === activeCommand}>
                <button type="button" onClick={() => completeCommand(command)}>
                  <b>{command.usage}</b><small>{command.description} · {command.source === "project" ? "项目" : command.source === "user" ? "个人" : "内置"}</small>
                </button>
              </li>
            ))}
          </ul>
          {!commandOptions.length && <small>未找到匹配命令。可添加 .pepsi-code/commands/*.md 后刷新。</small>}
          {commandDiagnostics.length > 0 && <small role="status">{commandDiagnostics.length} 项加载错误，输入 /commands 查看。</small>}
          {commandError && <small role="alert">{commandError}</small>}
        </div>
      )}
      <PermissionPrompt />
      <div className={styles.card}>
        <textarea
          id="task-composer"
          aria-label="任务指令"
          ref={textareaRef}
          className={styles.textarea}
          value={input}
          onChange={handleInput}
          onKeyDown={handleKeyDown}
          aria-controls={commandOptions.length ? "prompt-commands" : undefined}
          aria-activedescendant={commandOptions.length ? `prompt-command-${activeCommand}` : undefined}
          placeholder={running ? "补充当前任务，或选择完成后执行…" : "描述你的任务，输入 / 查看命令"}
          disabled={disabled}
          rows={1}
          autoFocus
        />
        <div className={styles.row}>
          <button className={styles.modelButton} type="button" onClick={onOpenSettings} disabled={disabled || running} title={isMock ? "添加厂商并配置 API" : "选择模型与思考深度"} aria-haspopup="dialog"><Icon name="spark" size={15} /><span>{isMock ? "配置模型" : modelName}</span>{!isMock && <small>{effortLabels[reasoningEffort] || reasoningEffort}</small>}<Icon name="chevron" size={12} /></button>
          <details ref={permissionsRef} className={styles.accessMode} open={permissionsOpen}
            onKeyDown={(event) => {
              if (event.key === "Escape") {
                event.preventDefault();
                setPermissionsOpen(false);
              }
            }}>
            <summary aria-label="权限模式" aria-disabled={disabled || running || permissionUpdating}
              title={accessModes[accessMode].description}
              onClick={(event) => {
                event.preventDefault();
                if (!disabled && !running && !permissionUpdating) setPermissionsOpen((value) => !value);
              }}>
              <Icon name="shield" size={14} />{accessModes[accessMode].label}<Icon name="chevron" size={12} />
            </summary>
            <div className={styles.permissionOptions} role="group" aria-label="选择权限模式">
              {Object.entries(accessModes).map(([value, mode]) => (
                <button key={value} type="button" aria-pressed={value === accessMode}
                  disabled={disabled || running || permissionUpdating}
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
              <button type="button" disabled={disabled || running || permissionUpdating} onClick={() => {
                setPermissionError("");
                setPermissionNotice("");
                void setWorkspaceDefaultPermissions(accessMode)
                  .then(() => { setPermissionNotice("已保存工作区默认权限，适用于新会话"); setPermissionsOpen(false); })
                  .catch((error) => setPermissionError(error instanceof Error ? error.message : String(error)));
              }}><span>将当前模式设为工作区默认</span><small>仅影响此工作区的新会话，不更改当前会话。</small></button>
            </div>
          </details>
          {running && (
            <Select compact label="输入执行时机" value={inputMode} onChange={value => setInputMode(value as InputMode)}
              options={[{ value: "steer", label: "补充当前任务" }, { value: "follow_up", label: "完成后执行" }]} />
          )}
          {running && <button type="button" className={styles.stopButton} disabled={isStopping}
            aria-label={isStopping ? "正在停止" : "停止任务"}
            onClick={() => { void cancelTurn().catch((error) => setSendError(String(error))); }}>
            {isStopping ? "停止中" : "停止"}
          </button>}
          <button className={styles.sendButton} aria-label={running ? "加入输入队列" : "发送"}
            onClick={() => void handleSend()} disabled={disabled || submitting || isStopping || !input.trim()}>
            <Icon name="arrow" size={18} />
          </button>
        </div>
      </div>
      {sendError && <div className={styles.modeHint} role="alert">{sendError}</div>}
      <div className={styles.statusBar}>
        <div className={styles.usageStatus} aria-label="实时用量">
          <span title="最近一次模型请求：缓存读取 token / 总输入 token；— 表示接口没有返回缓存数据。">
            缓存命中<strong>{cacheHitPercentage === null ? "—" : `${cacheHitPercentage.toFixed(1)}%`}</strong>
            {cacheHitPercentage !== null && latestUsage?.cache_read != null && latestUsage.input_total != null && `（${latestUsage.cache_read.toLocaleString()}/${latestUsage.input_total.toLocaleString()}）`}
          </span>
          <span className={styles.usageDot} aria-hidden="true" />
          <span title={contextPercent === null ? "上下文窗口数据未提供" : `${contextData.total_tokens.toLocaleString()} / ${contextData.max_tokens.toLocaleString()} tokens${estimated ? "（估算）" : ""}`}>
            上下文<strong>{contextPercent === null ? "—" : `${estimated ? "约 " : ""}${contextPercent.toFixed(1)}%`}</strong>
          </span>
          {contextPercent !== null && (
            <progress aria-label="上下文窗口占比" max={100} value={Math.max(0, Math.min(100, contextPercent))} />
          )}
        </div>
        <div className={styles.hint}>{running ? "响应及工具结束后处理补充指令 · Alt+Enter 排队 · 停止后取回未执行输入" : "Enter 发送 · Shift+Enter 换行"}</div>
      </div>
      {permissionError && <div className={styles.modeHint} role="alert">{permissionError}</div>}
      {permissionNotice && <div className={styles.modeHint} role="status">{permissionNotice}</div>}
    </div>
  );
}
