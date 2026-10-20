import { useEffect, useRef, useState, useCallback } from "react";
import { useSessionStore, type ChatMessage, type InputMode } from "../stores/session";
import MessageItem from "./MessageItem";
import Composer from "./Composer";
import ThinkingGroup, { isThinkingMessage } from "./ThinkingGroup";
import ModelSettings from "./ModelSettings";
import SessionTree from "./SessionTree";
import styles from "./ChatView.module.css";
import Icon, { type IconName } from "../components/Icon";
import CatLogo from "../components/CatLogo";
import type { FrameControls } from "../layout/AppFrame";

const suggestions: { icon: IconName; title: string; detail: string; prompt: string }[] = [
  { icon: "search", title: "了解这个项目", detail: "梳理结构与核心逻辑", prompt: "帮我梳理这个项目的目录结构、技术栈和核心执行流程。" },
  { icon: "code", title: "一起写点代码", detail: "把想法变成可用的功能", prompt: "我想给这个项目添加一个功能，请先了解现有架构，再和我讨论实现方案。" },
  { icon: "branch", title: "检查代码问题", detail: "发现缺陷与改进空间", prompt: "检查这个项目最近的代码改动，重点关注潜在缺陷和遗漏的边界情况。" },
];

type TimelineBlock =
  | { kind: "message"; message: ChatMessage }
  | { kind: "thinking"; messages: ChatMessage[]; endIndex: number };

function buildTimeline(messages: ChatMessage[]): TimelineBlock[] {
  const blocks: TimelineBlock[] = [];
  let thinking: ChatMessage[] = [];

  const flushThinking = (endIndex: number) => {
    if (thinking.length === 0) return;
    blocks.push({ kind: "thinking", messages: thinking, endIndex });
    thinking = [];
  };

  messages.forEach((message, index) => {
    if (isThinkingMessage(message)) {
      thinking.push(message);
      return;
    }
    flushThinking(index - 1);
    blocks.push({ kind: "message", message });
  });
  flushThinking(messages.length - 1);
  return blocks;
}

export default function ChatView({ onSend, frameControls }: { onSend: (input: string, mode?: InputMode) => Promise<void>; frameControls: FrameControls }) {
  const { messages, isRunning, cwd, modelUpdating, permissionUpdating, sessionId, planMode, verification, branchUpdating, branchError, setDraft } = useSessionStore();
  const [treeOpen, setTreeOpen] = useState(false);
  const projectName = cwd.replace(/\\/g, "/").split("/").filter(Boolean).pop() || "本地项目";
  const taskName = messages.find((message) => message.role === "user")?.content || "新任务";
  const [settingsOpen, setSettingsOpen] = useState(false);
  const scrollRef = useRef<HTMLDivElement>(null);
  const [autoScroll, setAutoScroll] = useState(true);
  const timeline = buildTimeline(messages);
  let lastUserIndex = -1;
  for (let index = messages.length - 1; index >= 0; index -= 1) {
    if (messages[index].role === "user") {
      lastUserIndex = index;
      break;
    }
  }
  const currentTurnHasAnswer = messages.slice(lastUserIndex + 1).some(
    (message) => message.role === "assistant" && !message.isStreaming && Boolean(message.content)
  );

  useEffect(() => {
    if (autoScroll && scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
    }
  }, [messages, autoScroll]);

  const handleScroll = useCallback(() => {
    if (!scrollRef.current) return;
    const { scrollTop, scrollHeight, clientHeight } = scrollRef.current;
    const atBottom = scrollHeight - scrollTop - clientHeight < 50;
    setAutoScroll(atBottom);
  }, []);

  return (
    <div className={styles.container}>
      <header className={styles.header}>
        <div className={styles.headerLeft}>
          <button className={styles.iconButton} type="button" onClick={frameControls.toggleSidebar} title={frameControls.sidebarCollapsed ? "展开侧栏" : "收起侧栏"} aria-label={frameControls.sidebarCollapsed ? "展开侧栏" : "收起侧栏"} aria-expanded={!frameControls.sidebarCollapsed} aria-controls="session-sidebar"><Icon name="sidebar" /></button>
          <span className={styles.projectName} title={cwd}>{projectName}</span><span className={styles.separator}>/</span><span className={styles.taskName} title={taskName}>{taskName}</span>
          {planMode && <span className={styles.planBadge}>计划模式</span>}
        </div>
        <div className={styles.headerRight}>
          <button className={styles.branchButton} type="button" onClick={() => setTreeOpen(!treeOpen)} disabled={branchUpdating || !sessionId} aria-expanded={treeOpen}><Icon name="branch" size={15} /><span>对话分支</span></button>
          <button className={styles.iconButton} type="button" onClick={frameControls.toggleDetails} title={frameControls.detailsCollapsed ? "展开会话详情" : "收起会话详情"} aria-label={frameControls.detailsCollapsed ? "展开会话详情" : "收起会话详情"} aria-expanded={!frameControls.detailsCollapsed} aria-controls="session-details"><Icon name="panel" /></button>
        </div>
      </header>
      {treeOpen && <SessionTree onClose={() => setTreeOpen(false)} />}
      {branchError && !treeOpen && <p className={styles.branchError} role="alert">{branchError}</p>}
      <div className={styles.messageList} ref={scrollRef} onScroll={handleScroll}>
        {messages.length === 0 ? (
          <div className={styles.emptyState}>
            <div className={styles.welcomeMark}><CatLogo size={54} /></div>
            <span className={styles.eyebrow}>你的本地编程搭档</span>
            <h1>想构建些什么？</h1>
            <p>从一个问题、一次修复，或一个新想法开始。</p>
            <span className={styles.projectPill} title={cwd}><Icon name="folder" size={14} />{projectName}</span>
            <div className={styles.suggestions}>{suggestions.map((item) => <button type="button" key={item.title} onClick={() => { setDraft(item.prompt); document.getElementById("task-composer")?.focus(); }} disabled={!sessionId || modelUpdating || branchUpdating}>
              <Icon name={item.icon} size={19} /><strong>{item.title}</strong><small>{item.detail}</small>
            </button>)}</div>
          </div>
        ) : (
          <div className={styles.column}>
            {timeline.map((block) => block.kind === "message" ? (
              <MessageItem key={block.message.id} message={block.message} />
            ) : (
              <ThinkingGroup
                key={`thinking-${block.messages[0].id}`}
                messages={block.messages}
                autoExpanded={isRunning && !currentTurnHasAnswer && block.endIndex > lastUserIndex}
              />
            ))}
          </div>
        )}
      </div>
      {verification && !["no_changes", "awaiting_user"].includes(verification.reason_code || "") && (
        <details className={styles.verification} role="status">
          <summary>
            {({ passed: "验证通过", failed: "验证失败", not_run: "尚未验证", blocked: "验证受阻", cancelled: "验证已停止" } as Record<string, string>)[verification.status] || verification.status}
            {" · "}
            {({ disabled: "项目未开启自动验证", no_tests: "没有找到可运行的检查" } as Record<string, string>)[verification.reason_code || ""] || verification.reason}
          </summary>
          {verification.details && <pre>{verification.details}</pre>}
        </details>
      )}
      <Composer onSend={onSend} onOpenSettings={() => setSettingsOpen(true)} disabled={!sessionId || modelUpdating || permissionUpdating || branchUpdating} running={isRunning} />
      {settingsOpen && <ModelSettings onClose={() => setSettingsOpen(false)} />}
    </div>
  );
}
