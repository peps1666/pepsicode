import { useState, useCallback, useEffect, useRef } from "react";
import { useSessionStore } from "../stores/session";
import { useProjectsStore, projectKey, projectName } from "../stores/projects";
import styles from "./SessionList.module.css";
import Icon from "../components/Icon";
import CatLogo from "../components/CatLogo";

function shortenPath(path: string): string {
  const parts = path.replace(/\\/g, "/").split("/").filter(Boolean);
  return parts.length <= 2 ? path : "…/" + parts.slice(-2).join("/");
}

export default function SessionList() {
  const {
    sessionId, cwd, sessions, sessionsLoading, isRunning, branchUpdating,
    modelUpdating, permissionUpdating, createSession, resumeSession, switchCwd,
  } = useSessionStore();
  const { projects, byProject, rememberProject, setExpanded, loadSessions, syncSessions } = useProjectsStore();
  const [switching, setSwitching] = useState(false);
  const [actionError, setActionError] = useState("");
  const operation = useRef(false);
  const busy = switching || isRunning || branchUpdating || modelUpdating || permissionUpdating || !sessionId;
  const currentKey = projectKey(cwd);

  useEffect(() => {
    if (cwd) rememberProject(cwd);
  }, [cwd, rememberProject]);

  useEffect(() => {
    if (cwd && !sessionsLoading) syncSessions(cwd, sessions);
  }, [cwd, sessions, sessionsLoading, syncSessions]);

  useEffect(() => {
    for (const project of projects) {
      if (project.expanded && projectKey(project.path) !== currentKey) void loadSessions(project.path);
    }
  }, [projects, currentKey, loadSessions]);

  const runAction = useCallback(async (action: () => Promise<void>) => {
    if (busy || operation.current) return;
    operation.current = true;
    setSwitching(true);
    setActionError("");
    try { await action(); }
    catch (error) { setActionError(error instanceof Error ? error.message : String(error)); }
    finally { operation.current = false; setSwitching(false); }
  }, [busy]);

  const handleNew = (path = cwd) => void runAction(async () => {
    if (projectKey(path) === currentKey) await createSession();
    else await switchCwd(path);
    rememberProject(path);
    setExpanded(path, true);
  });

  const handleAddProject = () => void runAction(async () => {
    const folder = await window.pepsiAPI.selectFolder();
    if (!folder) return;
    // Only add the folder after the server accepts it as a workspace.
    if (projectKey(folder) !== currentKey) await switchCwd(folder);
    rememberProject(folder);
    setExpanded(folder, true);
  });

  return (
    <div className={styles.container}>
      <div className={styles.header}>
        <span className={styles.brand}><CatLogo size={27} /><span>pepsicode</span></span>
        <span className={styles.localBadge}>本地</span>
      </div>
      <button type="button" className={styles.newBtn} onClick={() => handleNew()} disabled={busy}>
        <Icon name="compose" size={17} />新建对话<span className={styles.plus}>+</span>
      </button>
      <div className={styles.sectionLabel}>
        <span>项目</span>
        <button type="button" className={styles.addProject} onClick={handleAddProject} disabled={busy} aria-label="添加项目" title="选择文件夹，添加为项目">+</button>
      </div>
      {actionError && <div className={styles.actionError} role="alert">{actionError}</div>}
      <div className={styles.list} aria-label="项目列表">
        {projects.map((project, index) => {
          const key = projectKey(project.path);
          const activeProject = key === currentKey;
          const cached = byProject[key];
          const conversations = activeProject ? sessions : cached?.sessions || [];
          const loading = activeProject ? sessionsLoading : cached?.loading;
          const name = projectName(project.path);
          const hasUnsavedSession = activeProject && sessionId && !conversations.some((item) => item.session_id === sessionId);
          const listId = `project-conversations-${index}`;
          return (
            <section key={key} className={styles.project} aria-label={`${name} 项目`}>
              <div className={`${styles.projectRow} ${activeProject ? styles.currentProject : ""}`}>
                <button type="button" className={styles.projectToggle} onClick={() => setExpanded(project.path, !project.expanded)} aria-expanded={project.expanded} aria-controls={listId} title={project.path}>
                  <span className={`${styles.chevron} ${project.expanded ? styles.expanded : ""}`}><Icon name="chevron" size={12} /></span>
                  <Icon name="folder" size={16} />
                  <span className={styles.folderText}>{name}</span>
                </button>
                <button type="button" className={styles.projectNew} disabled={busy} onClick={() => handleNew(project.path)} aria-label={`在 ${name} 中新建对话`} title="新建对话"><Icon name="compose" size={14} /></button>
              </div>
              {project.expanded && (
                <div id={listId} className={styles.conversations}>
                  {loading && !conversations.length && <div className={styles.loading}>正在加载对话…</div>}
                  {!activeProject && cached?.error && <div className={styles.loadError} role="alert">加载失败<button type="button" onClick={() => void loadSessions(project.path)}>重试</button></div>}
                  {hasUnsavedSession && <button type="button" className={`${styles.item} ${styles.active}`} disabled={busy} aria-current="page"><span className={styles.itemFirst}>新对话</span><span className={styles.unsavedDot} /></button>}
                  {conversations.map((conversation) => {
                    const selected = activeProject && conversation.session_id === sessionId;
                    return (
                      <button type="button" key={conversation.session_id} className={`${styles.item} ${selected ? styles.active : ""}`} disabled={busy} aria-current={selected ? "page" : undefined} title={conversation.first_message || "新对话"}
                        onClick={() => { if (!selected) void runAction(() => resumeSession(conversation.session_id, project.path)); }}>
                        <span className={styles.itemFirst}>{conversation.first_message || "新对话"}</span>
                        <span className={styles.itemDate}>{new Date(conversation.updated_at * 1000).toLocaleDateString("zh-CN", { month: "numeric", day: "numeric" })}</span>
                      </button>
                    );
                  })}
                  {!loading && !cached?.error && !conversations.length && !hasUnsavedSession && <div className={styles.empty}>暂无对话，点击右侧按钮开始</div>}
                </div>
              )}
            </section>
          );
        })}
        {!projects.length && <div className={styles.empty}>添加一个项目，开始新的对话。</div>}
      </div>
      <div className={styles.footer}>
        <span className={styles.avatar}><CatLogo size={24} /></span>
        <span>本地工作空间<small title={cwd}>{cwd ? shortenPath(cwd) : "选择一个项目开始"}</small></span>
        <span className={styles.statusDot} title="已连接本地服务" />
      </div>
    </div>
  );
}
