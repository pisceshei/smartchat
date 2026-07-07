import { create } from "zustand";
import { persist } from "zustand/middleware";
import type { User, WorkspaceBrief } from "@/api/types";

interface AuthState {
  token: string | null;
  user: User | null;
  workspaceId: string | null;
  workspaces: WorkspaceBrief[];
  setAuth: (token: string, user: User, workspaces: WorkspaceBrief[]) => void;
  setWorkspace: (workspaceId: string) => void;
  logout: () => void;
}

export const useAuthStore = create<AuthState>()(
  persist(
    (set) => ({
      token: null,
      user: null,
      workspaceId: null,
      workspaces: [],
      setAuth: (token, user, workspaces) =>
        set((s) => ({
          token,
          user,
          workspaces,
          workspaceId:
            s.workspaceId && workspaces.some((w) => w.id === s.workspaceId)
              ? s.workspaceId
              : (workspaces[0]?.id ?? null),
        })),
      setWorkspace: (workspaceId) => set({ workspaceId }),
      logout: () => set({ token: null, user: null, workspaceId: null, workspaces: [] }),
    }),
    { name: "smartchat.auth" },
  ),
);
