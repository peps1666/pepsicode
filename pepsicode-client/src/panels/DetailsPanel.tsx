import { useState } from "react";
import styles from "./DetailsPanel.module.css";
import PlanPanel from "./PlanPanel";
import HooksPanel from "./HooksPanel";
import CostPanel from "./CostPanel";

type Tab = "cost" | "plan" | "hooks";

const TABS: Array<{ id: Tab; label: string }> = [
  { id: "cost", label: "Cost" },
  { id: "plan", label: "Plan" },
  { id: "hooks", label: "Hooks" },
];

export default function DetailsPanel() {
  const [activeTab, setActiveTab] = useState<Tab>("cost");

  return (
    <div className={styles.container}>
      <div className={styles.tabBar}>
        {TABS.map((tab) => (
          <button
            key={tab.id}
            className={`${styles.tab} ${activeTab === tab.id ? styles.active : ""}`}
            onClick={() => setActiveTab(tab.id)}
          >
            {tab.label}
          </button>
        ))}
      </div>
      <div className={styles.content}>
        {activeTab === "cost" && <CostPanel />}
        {activeTab === "plan" && <PlanPanel />}
        {activeTab === "hooks" && <HooksPanel />}
      </div>
    </div>
  );
}
