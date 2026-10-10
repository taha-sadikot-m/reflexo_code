"use client";

import {
  createContext,
  forwardRef,
  useContext,
  useEffect,
  useId,
  useState,
  type ButtonHTMLAttributes,
  type HTMLAttributes,
  type InputHTMLAttributes,
  type ReactNode,
  type TableHTMLAttributes,
} from "react";
import { Icon } from "@/components/Icon";

function cx(...values: Array<string | false | null | undefined>) {
  return values.filter(Boolean).join(" ");
}

type ButtonVariant = "default" | "primary" | "accent" | "ghost" | "danger" | "solid-danger" | "link";
type ButtonSize = "sm" | "md" | "lg" | "icon";

export interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: ButtonVariant;
  size?: ButtonSize;
  loading?: boolean;
  block?: boolean;
}

export const Button = forwardRef<HTMLButtonElement, ButtonProps>(function Button(
  { variant = "default", size = "md", loading, block, className, disabled, children, ...props },
  ref,
) {
  return (
    <button
      ref={ref}
      className={cx(
        "btn",
        variant !== "default" && `btn-${variant}`,
        size !== "md" && `btn-${size}`,
        loading && "btn-loading",
        block && "btn-block",
        className,
      )}
      disabled={disabled || loading}
      aria-busy={loading || undefined}
      {...props}
    >
      {children}
    </button>
  );
});

export const Input = forwardRef<HTMLInputElement, InputHTMLAttributes<HTMLInputElement>>(
  function Input({ className, ...props }, ref) {
    return <input ref={ref} className={cx("input", className)} {...props} />;
  },
);

export function Field({
  label,
  hint,
  error,
  required,
  children,
}: {
  label: ReactNode;
  hint?: ReactNode;
  error?: ReactNode;
  required?: boolean;
  children: ReactNode;
}) {
  return (
    <label className="field" data-invalid={error ? "" : undefined}>
      <span>
        {label} {required && <span className="req">*</span>}
      </span>
      {children}
      {error ? <span className="err">{error}</span> : hint ? <span className="hint">{hint}</span> : null}
    </label>
  );
}

type CardTone = "default" | "raised" | "sunk" | "quiet";
export function Card({
  tone = "default",
  selvedge,
  className,
  ...props
}: HTMLAttributes<HTMLDivElement> & {
  tone?: CardTone;
  selvedge?: "accent" | "brand" | "pos" | "neg";
}) {
  return (
    <div
      className={cx(
        "card",
        tone !== "default" && `card-${tone}`,
        selvedge && "card-selvedge",
        selvedge && selvedge !== "accent" && `card-selvedge-${selvedge}`,
        className,
      )}
      {...props}
    />
  );
}

type BadgeTone = "default" | "pos" | "warn" | "neg" | "info" | "brand" | "accent" | "outline";
export function Badge({
  tone = "default",
  className,
  ...props
}: HTMLAttributes<HTMLSpanElement> & { tone?: BadgeTone }) {
  return <span className={cx("badge", tone !== "default" && `badge-${tone}`, className)} {...props} />;
}

export interface DataTableProps extends TableHTMLAttributes<HTMLTableElement> {
  footer?: ReactNode;
}
export function DataTable({ footer, className, children, ...props }: DataTableProps) {
  return (
    <div className="tablewrap">
      <table className={cx("tbl", className)} {...props}>
        {children}
      </table>
      {footer && <div className="tbl-foot">{footer}</div>}
    </div>
  );
}

export interface TabItem {
  id: string;
  label: ReactNode;
  count?: number;
}
export function Tabs({
  items,
  value,
  onChange,
  label = "Sections",
}: {
  items: TabItem[];
  value: string;
  onChange: (id: string) => void;
  label?: string;
}) {
  return (
    <div className="tabs" role="tablist" aria-label={label}>
      {items.map((item) => (
        <button
          key={item.id}
          type="button"
          role="tab"
          aria-selected={value === item.id}
          onClick={() => onChange(item.id)}
        >
          {item.label}
          {item.count !== undefined && <span className="tab-count">{item.count}</span>}
        </button>
      ))}
    </div>
  );
}

interface OverlayProps {
  open: boolean;
  title: ReactNode;
  description?: ReactNode;
  children?: ReactNode;
  footer?: ReactNode;
  onClose: () => void;
}

export function Dialog({ open, title, description, children, footer, onClose }: OverlayProps) {
  const titleId = useId();
  useEscape(open, onClose);
  if (!open) return null;
  return (
    <div className="scrim" onMouseDown={(event) => event.target === event.currentTarget && onClose()}>
      <section className="dialog" role="dialog" aria-modal="true" aria-labelledby={titleId}>
        <header className="dialog-head">
          <div className="grow">
            <h3 id={titleId}>{title}</h3>
            {description && <p>{description}</p>}
          </div>
          <Button variant="ghost" size="icon" onClick={onClose} aria-label="Close dialog">
            <Icon name="x" />
          </Button>
        </header>
        {children && <div className="dialog-body">{children}</div>}
        {footer && <footer className="dialog-foot">{footer}</footer>}
      </section>
    </div>
  );
}

export function Drawer({ open, title, description, children, footer, onClose }: OverlayProps) {
  const titleId = useId();
  useEscape(open, onClose);
  if (!open) return null;
  return (
    <>
      <div className="drawer-scrim" onClick={onClose} />
      <section className="drawer" role="dialog" aria-modal="true" aria-labelledby={titleId}>
        <header className="drawer-head">
          <div>
            <h3 id={titleId}>{title}</h3>
            {description && <p className="muted">{description}</p>}
          </div>
          <Button variant="ghost" size="icon" onClick={onClose} aria-label="Close drawer">
            <Icon name="x" />
          </Button>
        </header>
        <div className="drawer-body">{children}</div>
        {footer && <footer className="drawer-foot">{footer}</footer>}
      </section>
    </>
  );
}

function useEscape(active: boolean, onClose: () => void) {
  useEffect(() => {
    if (!active) return;
    const close = (event: KeyboardEvent) => event.key === "Escape" && onClose();
    window.addEventListener("keydown", close);
    return () => window.removeEventListener("keydown", close);
  }, [active, onClose]);
}

type ToastTone = "pos" | "warn" | "neg" | "info";
interface ToastMessage {
  id: number;
  title: string;
  body?: string;
  tone: ToastTone;
}
interface ToastContextValue {
  toast: (message: Omit<ToastMessage, "id">) => void;
}
const ToastContext = createContext<ToastContextValue | null>(null);

export function ToastProvider({ children }: { children: ReactNode }) {
  const [messages, setMessages] = useState<ToastMessage[]>([]);
  function toast(message: Omit<ToastMessage, "id">) {
    const id = Date.now();
    setMessages((current) => [...current, { ...message, id }]);
    window.setTimeout(() => setMessages((current) => current.filter((item) => item.id !== id)), 5000);
  }
  return (
    <ToastContext.Provider value={{ toast }}>
      {children}
      <div className="toast-region" role="region" aria-live="polite" aria-label="Notifications">
        {messages.map((message) => (
          <div className={`toast toast-${message.tone}`} key={message.id}>
            <Icon name={message.tone === "pos" ? "check" : message.tone === "neg" ? "alert" : "info"} className="ic t-ic" />
            <div className="grow">
              <b>{message.title}</b>
              {message.body && <p>{message.body}</p>}
            </div>
            <button
              className="t-close"
              aria-label="Dismiss notification"
              onClick={() => setMessages((current) => current.filter((item) => item.id !== message.id))}
            >
              <Icon name="x" className="ic ic-sm" />
            </button>
            <span className="t-progress" />
          </div>
        ))}
      </div>
    </ToastContext.Provider>
  );
}

export function useToast() {
  const context = useContext(ToastContext);
  if (!context) throw new Error("useToast must be used within ToastProvider");
  return context;
}

export function Checkbox({
  children,
  ...props
}: InputHTMLAttributes<HTMLInputElement> & { children: ReactNode }) {
  const id = useId();
  return (
    <label className="check" htmlFor={id}>
      <input id={id} type="checkbox" {...props} />
      <span className="box">
        <svg viewBox="0 0 24 24" aria-hidden="true">
          <path d="m4.5 12.5 5 5 10-11" fill="none" stroke="currentColor" strokeLinecap="round" strokeLinejoin="round" />
        </svg>
      </span>
      <span>{children}</span>
    </label>
  );
}
