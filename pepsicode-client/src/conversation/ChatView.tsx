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
import ExecutionTrace from "./ExecutionTrace";
import type { TraceRow } from "./trajectory";

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
  const { messages, isRunning, cwd, modelUpdating, permissionUpdating, sessionId, planMode, verification, task, branchUpdating, branchError, setDraft } = useSessionStore();
  const [treeOpen, setTreeOpen] = useState(false);
  const [view, setView] = useState<"chat" | "trace">("chat");
  const [continuationError, setContinuationError] = useState("");
  const traceFailures = messages.filter(message => message.isError && ["tool_result", "assistant"].includes(message.role)).length;
  function continueFrom(row: TraceRow) {
    if (isRunning || modelUpdating || branchUpdating || permissionUpdating || !sessionId) return;
    setContinuationError("");
    setView("chat");
    const prompt = "继续处理当前任务中的失败步骤：" + row.label + " " + row.summary +
      "\n失败记录：" + row.output.slice(0, 1500) +
      "\n请先分析错误和当前项目状态，确认之前操作是否部分完成，再调整参数或选择替代方案继续。保留已有结果；不要直接重复写入或命令，也不要绕过权限或取消操作。若仍受阻，请说明未完成的部分。";
    void onSend(prompt).catch(error => setContinuationError(error instanceof Error ? error.message : String(error)));
  }

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
      <nav className={styles.viewTabs} role="tablist" aria-label="会话视图" onKeyDown={event => {
        if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
        event.preventDefault();
        const next = event.key === "Home" ? "chat" : event.key === "End" ? "trace" : view === "chat" ? "trace" : "chat";
        setView(next); event.currentTarget.querySelector<HTMLButtonElement>(next === "chat" ? "#chat-tab" : "#trace-tab")?.focus();
      }}>
        <button type="button" role="tab" id="chat-tab" tabIndex={view === "chat" ? 0 : -1} aria-selected={view === "chat"} aria-controls="conversation-content" onClick={() => setView("chat")}>对话</button>
        <button type="button" role="tab" id="trace-tab" tabIndex={view === "trace" ? 0 : -1} aria-selected={view === "trace"} aria-controls="execution-content" onClick={() => setView("trace")}>执行轨迹{traceFailures > 0 && <span className={styles.failureCount}>{traceFailures}</span>}</button>
      </nav>
      {treeOpen && <SessionTree onClose={() => setTreeOpen(false)} />}
      {branchError && !treeOpen && <p className={styles.branchError} role="alert">{branchError}</p>}
      {continuationError && <p className={styles.branchError} role="alert">{continuationError}</p>}
      {view === "trace" ? <div id="execution-content" role="tabpanel" aria-labelledby="trace-tab" className={styles.traceArea}>
        <ExecutionTrace messages={messages} running={isRunning} verification={verification} taskReport={task} canContinue={Boolean(sessionId) && !isRunning && !modelUpdating && !branchUpdating && !permissionUpdating} onContinue={continueFrom} />
      </div> : <div id="conversation-content" role="tabpanel" aria-labelledby="chat-tab" className={styles.messageList} ref={scrollRef} onScroll={handleScroll}>
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
                onViewTrace={() => setView("trace")}
              />
            ))}
          </div>
        )}
      </div>}
      <Composer onSend={onSend} onOpenSettings={() => setSettingsOpen(true)} disabled={!sessionId || modelUpdating || permissionUpdating || branchUpdating} running={isRunning} />
      {settingsOpen && <ModelSettings onClose={() => setSettingsOpen(false)} />}
    </div>
  );
}
