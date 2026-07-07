import type { ReactNode } from "react";

/** Consistent empty state: soft illustration circle + title + hint + action. */
export function EmptyState({
  icon,
  title,
  hint,
  action,
  compact = false,
}: {
  icon: ReactNode;
  title: string;
  hint?: string;
  action?: ReactNode;
  compact?: boolean;
}) {
  return (
    <div
      style={{
        display: "flex",
        flexDirection: "column",
        alignItems: "center",
        justifyContent: "center",
        padding: compact ? "24px 16px" : "56px 24px",
        textAlign: "center",
        gap: 6,
      }}
    >
      <div
        style={{
          width: compact ? 48 : 72,
          height: compact ? 48 : 72,
          borderRadius: "50%",
          background: "var(--sc-primary-bg)",
          color: "var(--sc-primary)",
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
          fontSize: compact ? 20 : 30,
          marginBottom: 8,
        }}
      >
        {icon}
      </div>
      <div style={{ fontWeight: 600, fontSize: compact ? 13.5 : 15, color: "var(--sc-text-heading)" }}>
        {title}
      </div>
      {hint && (
        <div style={{ fontSize: 12.5, color: "var(--sc-text-secondary)", maxWidth: 320 }}>{hint}</div>
      )}
      {action && <div style={{ marginTop: 10 }}>{action}</div>}
    </div>
  );
}
