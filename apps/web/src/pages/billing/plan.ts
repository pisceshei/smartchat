/** Plan-gating helpers shared by the 行銷 / 報告 / 訂閱 sections.
 *  Reads the current workspace plan from the auth store (cheap, no network);
 *  broadcasts + reports are Pro-gated features per the plan's feature matrix. */
import { useAuthStore } from "@/stores/auth";

/** Current workspace plan code (free/pro/max/custom); "free" when unknown. */
export function usePlanCode(): string {
  const workspaceId = useAuthStore((s) => s.workspaceId);
  const workspaces = useAuthStore((s) => s.workspaces);
  const ws = workspaces.find((w) => w.id === workspaceId);
  return (ws?.plan_code ?? "free").toLowerCase();
}

/** Whether the workspace is on a paid plan (Pro / Max / Custom). */
export function useIsPro(): boolean {
  const code = usePlanCode();
  return code !== "free";
}

/** Whether the current member can use super-admin-only self-service controls
 *  (e.g. the no-charge plan switch — the self-use path). The backend enforces
 *  this hard (403 otherwise); the UI gate is cosmetic. An unknown role is
 *  treated as the workspace owner so the self-use control stays reachable. */
export function useIsSuperAdmin(): boolean {
  const workspaceId = useAuthStore((s) => s.workspaceId);
  const workspaces = useAuthStore((s) => s.workspaces);
  const ws = workspaces.find((w) => w.id === workspaceId);
  const role = (ws?.role_name ?? "").toLowerCase();
  return role === "" || role === "super_admin" || role === "owner" || role === "admin";
}
