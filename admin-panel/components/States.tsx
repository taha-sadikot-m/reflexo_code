"use client";

import { Icon } from "./Icon";
import { Button, Card } from "./ui";

export function LoadingState({ label = "Weaving…" }: { label?: string }) {
  return (
    <Card className="state-loading" aria-busy="true" aria-live="polite">
      <div className="row-flex gap3">
        <span className="spinner" aria-hidden="true" />
        <div>
          <div className="eyebrow">Loading</div>
          <p className="muted">{label}</p>
        </div>
      </div>
      <div className="stack gap2 mt4" aria-hidden="true">
        <div className="skel skel-line" />
        <div className="skel skel-line state-skeleton-short" />
      </div>
    </Card>
  );
}

export function EmptyState({
  title,
  body,
  action,
}: {
  title: string;
  body: string;
  action?: React.ReactNode;
}) {
  return (
    <div className="state-panel">
      <Icon name="inbox" className="ic state-art state-art-icon" />
      <h3>{title}</h3>
      <p>{body}</p>
      {action && <div className="state-actions">{action}</div>}
    </div>
  );
}

export function ErrorState({ message, onRetry }: { message: string; onRetry?: () => void }) {
  return (
    <div className="state-panel" role="alert">
      <Icon name="alert" className="ic state-art state-art-icon state-art-neg" />
      <h3>Something snagged</h3>
      <p>{message}</p>
      {onRetry && <div className="state-actions"><Button onClick={onRetry}><Icon name="refresh" />Try again</Button></div>}
    </div>
  );
}

export function Forbidden() {
  return (
    <div className="state-panel">
      <Icon name="shield" className="ic state-art state-art-icon" />
      <div className="eyebrow">403 · Access denied</div>
      <h3>You don’t have the selvedge for this</h3>
      <p>Your role can view this area but cannot change it.</p>
    </div>
  );
}
