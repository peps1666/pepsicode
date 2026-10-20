import { useState, useRef, useCallback, useEffect, type ReactNode } from "react";
import styles from "./AppFrame.module.css";

interface AppFrameProps {
  sidebar: ReactNode;
  center: (controls: FrameControls) => ReactNode;
  details: ReactNode;
}

export interface FrameControls {
  sidebarCollapsed: boolean;
  detailsCollapsed: boolean;
  toggleSidebar: () => void;
  toggleDetails: () => void;
}

const MIN_SIDEBAR = 180;
const MAX_SIDEBAR = 400;
const MIN_DETAILS = 240;
const MAX_DETAILS = 520;
const DEFAULT_SIDEBAR = 248;
const DEFAULT_DETAILS = 340;
const COLLAPSE_THRESHOLD = 900;

export default function AppFrame({ sidebar, center, details }: AppFrameProps) {
  const [sidebarWidth, setSidebarWidth] = useState(DEFAULT_SIDEBAR);
  const [detailsWidth, setDetailsWidth] = useState(DEFAULT_DETAILS);
  const [sidebarCollapsed, setSidebarCollapsed] = useState(() => window.innerWidth < COLLAPSE_THRESHOLD);
  const [detailsCollapsed, setDetailsCollapsed] = useState(true);
  const [narrow, setNarrow] = useState(() => window.innerWidth < COLLAPSE_THRESHOLD);
  const draggingRef = useRef<"sidebar" | "details" | null>(null);

  const handleMouseDown = useCallback((which: "sidebar" | "details") => (e: React.MouseEvent) => {
    e.preventDefault();
    draggingRef.current = which;
    document.body.style.cursor = "col-resize";
    document.body.style.userSelect = "none";
  }, []);

  const handleMouseMove = useCallback((e: MouseEvent) => {
    if (!draggingRef.current) return;
    if (draggingRef.current === "sidebar") {
      const newWidth = Math.min(MAX_SIDEBAR, Math.max(MIN_SIDEBAR, e.clientX));
      setSidebarWidth(newWidth);
    } else if (draggingRef.current === "details") {
      const fromRight = window.innerWidth - e.clientX;
      const available = window.innerWidth - (sidebarCollapsed ? 0 : sidebarWidth) - 400;
      const newWidth = Math.min(MAX_DETAILS, Math.max(MIN_DETAILS, Math.min(available, fromRight)));
      setDetailsWidth(newWidth);
    }
  }, [sidebarCollapsed, sidebarWidth]);

  const handleMouseUp = useCallback(() => {
    draggingRef.current = null;
    document.body.style.cursor = "";
    document.body.style.userSelect = "";
  }, []);

  // Auto-collapse on narrow windows
  useEffect(() => {
    const media = window.matchMedia(`(max-width: ${COLLAPSE_THRESHOLD - 1}px)`);
    const detailsMedia = window.matchMedia("(max-width: 1149px)");
    const handleDetailsResize = () => { if (detailsMedia.matches) setDetailsCollapsed(true); };
    const handler = () => {
      setNarrow(media.matches);
      setSidebarCollapsed(media.matches);
      if (media.matches) setDetailsCollapsed(true);
    };
    media.addEventListener("change", handler);
    detailsMedia.addEventListener("change", handleDetailsResize);
    return () => {
      media.removeEventListener("change", handler);
      detailsMedia.removeEventListener("change", handleDetailsResize);
    };
  }, []);

  useEffect(() => {
    window.addEventListener("mousemove", handleMouseMove);
    window.addEventListener("mouseup", handleMouseUp);
    return () => {
      window.removeEventListener("mousemove", handleMouseMove);
      window.removeEventListener("mouseup", handleMouseUp);
    };
  }, [handleMouseMove, handleMouseUp]);

  const sidebarStyle = sidebarCollapsed
    ? { width: 0, borderRight: "none" as const, overflow: "hidden" as const }
    : { width: sidebarWidth };

  const detailsStyle = detailsCollapsed
    ? { width: 0, borderLeft: "none" as const, overflow: "hidden" as const }
    : { width: detailsWidth };

  return (
    <div className={styles.frame}>
      {narrow && (!sidebarCollapsed || !detailsCollapsed) && <button className={styles.backdrop} aria-label="关闭侧面板" onClick={() => { setSidebarCollapsed(true); setDetailsCollapsed(true); }} />}
      <aside id="session-sidebar" aria-label="项目与会话" aria-hidden={sidebarCollapsed} {...(sidebarCollapsed ? { inert: "" } : {})} className={`${styles.sidebar} ${narrow ? styles.overlaySidebar : ""}`} style={sidebarStyle}>
        {sidebar}
      </aside>
      {!sidebarCollapsed && !narrow && (
        <div className={styles.dragHandle} onMouseDown={handleMouseDown("sidebar")} />
      )}
      <main className={styles.center}>{center({ sidebarCollapsed, detailsCollapsed,
        toggleSidebar: () => { setSidebarCollapsed((value) => !value); if (window.innerWidth < 1150) setDetailsCollapsed(true); },
        toggleDetails: () => { setDetailsCollapsed((value) => !value); if (window.innerWidth < 1150) setSidebarCollapsed(true); },
      })}</main>
      {!detailsCollapsed && !narrow && (
        <div className={styles.dragHandle} onMouseDown={handleMouseDown("details")} />
      )}
      <aside id="session-details" aria-label="会话详情" aria-hidden={detailsCollapsed} {...(detailsCollapsed ? { inert: "" } : {})} className={`${styles.details} ${narrow ? styles.overlayDetails : ""}`} style={detailsStyle}>
        {details}
      </aside>
    </div>
  );
}
