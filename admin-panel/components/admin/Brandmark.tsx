import { Icon } from "@/components/Icon";

export function Brandmark({ compact = false }: { compact?: boolean }) {
  return (
    <div className="brandmark" aria-label="REFLEXO Control Room">
      <span className="bm-tile">
        <Icon name="spark" className="ic brandmark-icon" />
      </span>
      {!compact && (
        <span className="bm-text">
          <span className="bm-word">REFLEXO</span>
          <span className="bm-sub">Control Room</span>
        </span>
      )}
    </div>
  );
}
