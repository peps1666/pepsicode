import { useEffect, useId, useLayoutEffect, useRef, useState, type CSSProperties, type KeyboardEvent } from "react";
import { createPortal } from "react-dom";
import Icon from "./Icon";
import styles from "./Select.module.css";

export interface SelectOption {
  value: string;
  label: string;
  description?: string;
  disabled?: boolean;
  separator?: boolean;
}

interface SelectProps {
  label: string;
  value: string;
  options: SelectOption[];
  onChange: (value: string) => void;
  placeholder?: string;
  disabled?: boolean;
  compact?: boolean;
  autoFocus?: boolean;
}

export default function Select({ label, value, options, onChange, placeholder = "请选择", disabled, compact, autoFocus }: SelectProps) {
  const id = useId();
  const trigger = useRef<HTMLButtonElement>(null);
  const menu = useRef<HTMLDivElement>(null);
  const search = useRef({ text: "", time: 0 });
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState(-1);
  const [position, setPosition] = useState<CSSProperties>({ visibility: "hidden" });
  const selected = options.find(item => item.value === value);
  const expanded = open && !disabled;

  useEffect(() => { if (disabled) setOpen(false); }, [disabled]);

  function show(last = false) {
    if (disabled) return;
    const selectedIndex = options.findIndex(item => item.value === value && !item.disabled);
    setActive(selectedIndex >= 0 ? selectedIndex : last ? options.reduce((previous, item, index) => item.disabled ? previous : index, -1) : options.findIndex(item => !item.disabled));
    setOpen(true);
  }

  function choose(index: number) {
    const item = options[index];
    if (!item || item.disabled) return;
    setOpen(false);
    onChange(item.value);
  }

  function keyDown(event: KeyboardEvent<HTMLButtonElement>) {
    if (disabled) return;
    if (event.key === "Tab") { setOpen(false); return; }
    if (event.key === "Escape" && expanded) {
      event.preventDefault(); event.stopPropagation(); setOpen(false); return;
    }
    if (["ArrowDown", "ArrowUp", "Home", "End", "Enter", " "].includes(event.key)) {
      event.preventDefault();
      if (!expanded) { show(event.key === "ArrowUp" || event.key === "End"); return; }
      if (event.key === "Enter" || event.key === " ") { choose(active); return; }
      const enabled = options.map((item, index) => item.disabled ? -1 : index).filter(index => index >= 0);
      if (!enabled.length) return;
      const current = enabled.indexOf(active);
      setActive(event.key === "Home" ? enabled[0] : event.key === "End" ? enabled[enabled.length - 1] : enabled[(current + (event.key === "ArrowUp" ? -1 : 1) + enabled.length) % enabled.length]);
    } else if (event.key.length === 1 && !event.ctrlKey && !event.metaKey && !event.altKey) {
      event.preventDefault();
      const now = Date.now();
      search.current = { text: (now - search.current.time < 650 ? search.current.text : "") + event.key.toLocaleLowerCase(), time: now };
      const match = options.findIndex(item => !item.disabled && item.label.toLocaleLowerCase().startsWith(search.current.text));
      if (match >= 0) { setActive(match); setOpen(true); }
    }
  }

  useLayoutEffect(() => {
    if (!expanded || !trigger.current || !menu.current) return;
    function place() {
      if (!trigger.current || !menu.current) return;
      const rect = trigger.current.getBoundingClientRect();
      if (rect.bottom < 12 || rect.top > window.innerHeight - 12) { setOpen(false); return; }
      const width = Math.min(Math.max(rect.width, compact ? 208 : 0), window.innerWidth - 24);
      const below = window.innerHeight - rect.bottom - 16;
      const above = rect.top - 16;
      const upwards = below < Math.min(280, menu.current.scrollHeight) && above > below;
      const maxHeight = Math.max(40, Math.min(280, upwards ? above : below));
      const height = Math.min(menu.current.scrollHeight, maxHeight);
      const top = upwards ? rect.top - height - 6 : rect.bottom + 6;
      setPosition({ width, maxHeight, left: Math.max(12, Math.min(rect.left, window.innerWidth - width - 12)), top: Math.max(12, Math.min(top, window.innerHeight - height - 12)) });
    }
    place();
    window.addEventListener("resize", place);
    window.addEventListener("scroll", place, true);
    return () => { window.removeEventListener("resize", place); window.removeEventListener("scroll", place, true); };
  }, [expanded, compact, options]);

  useEffect(() => {
    if (!expanded) return;
    const dismiss = (event: PointerEvent | FocusEvent) => {
      const target = event.target as Node;
      if (!trigger.current?.contains(target) && !menu.current?.contains(target)) setOpen(false);
    };
    document.addEventListener("pointerdown", dismiss, true);
    document.addEventListener("focusin", dismiss);
    return () => { document.removeEventListener("pointerdown", dismiss, true); document.removeEventListener("focusin", dismiss); };
  }, [expanded]);

  useEffect(() => {
    if (expanded) menu.current?.querySelector(`[data-index="${active}"]`)?.scrollIntoView({ block: "nearest" });
  }, [active, expanded]);

  return <>
    <button ref={trigger} type="button" role="combobox" aria-label={label} aria-expanded={expanded} aria-controls={expanded ? id : undefined}
      aria-haspopup="listbox" aria-activedescendant={expanded && active >= 0 ? `${id}-${active}` : undefined}
      className={`${styles.trigger} ${compact ? styles.compact : ""}`} disabled={disabled} autoFocus={autoFocus}
      onClick={() => expanded ? setOpen(false) : show()} onKeyDown={keyDown}>
      <span className={!selected ? styles.placeholder : styles.value} title={selected?.label}>{selected?.label || placeholder}</span><Icon name="chevron" size={15} />
    </button>
    {expanded && trigger.current && createPortal(<div ref={menu} id={id} role="listbox" aria-label={label} className={styles.menu} style={position}>
      {options.length === 0 && <div className={styles.empty}>暂无可选项</div>}
      {options.map((item, index) => <div key={item.value} id={`${id}-${index}`} role="option" aria-selected={item.value === value}
        aria-disabled={item.disabled || undefined} data-value={item.value} data-index={index} data-active={index === active || undefined}
        className={`${styles.option} ${item.separator ? styles.separator : ""}`} onPointerMove={() => { if (!item.disabled) setActive(index); }}
        onMouseDown={event => event.preventDefault()} onClick={() => choose(index)}>
        <span className={styles.optionText}><span>{item.label}</span>{item.description && <small>{item.description}</small>}</span>
        <span className={styles.check}>{item.value === value && <Icon name="check" size={15} />}</span>
      </div>)}
    </div>, trigger.current.closest("dialog") || document.body)}
  </>;
}
