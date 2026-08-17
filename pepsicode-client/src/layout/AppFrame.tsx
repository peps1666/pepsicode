import { useState, useRef, useCallback, useEffect, type ReactNode } from "react";
import styles from "./AppFrame.module.css";

interface AppFrameProps {
  sidebar: ReactNode;
  center: ReactNode;
  details: ReactNode;
}

const MIN_SIDEBAR = 180;
const MAX_SIDEBAR = 400;
const MIN_DETAILS = 240;
const MAX_DETAILS = 520;
const DEFAULT_SIDEBAR = 260;
const DEFAULT_DETAILS = 340;
const COLLAPSE_THRESHOLD = 900;

export default function AppFrame({ sidebar, center, details }: AppFrameProps) {
  const [sidebarWidth, setSidebarWidth] = useState(DEFAULT_SIDEBAR);
  const [detailsWidth, setDetailsWidth] = useState(DEFAULT_DETAILS);
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false);
  const [detailsCollapsed, setDetailsCollapsed] = useState(false);
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
      const newWidth = Math.min(MAX_DETAILS, Math.max(MIN_DETAILS, fromRight));
      setDetailsWidth(newWidth);
    }
  }, []);

  const handleMouseUp = useCallback(() => {
    draggingRef.current = null;
    document.body.style.cursor = "";
    document.body.style.userSelect = "";
  }, []);

  // Auto-collapse on narrow windows
  useEffect(() => {
    const handler = () => {
      setSidebarCollapsed(window.innerWidth < COLLAPSE_THRESHOLD);
      setDetailsCollapsed(window.innerWidth < COLLAPSE_THRESHOLD + 200);
    };
    handler();
    window.addEventListener("resize", handler);
    return () => window.removeEventListener("resize", handler);
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
      <div className={styles.sidebar} style={sidebarStyle}>
        {sidebar}
      </div>
      {!sidebarCollapsed && (
        <div className={styles.dragHandle} onMouseDown={handleMouseDown("sidebar")} />
      )}
      <div className={styles.center}>{center}</div>
      {!detailsCollapsed && (
        <div className={styles.dragHandle} onMouseDown={handleMouseDown("details")} />
      )}
      <div className={styles.details} style={detailsStyle}>
        {details}
      </div>
    </div>
  );
}
