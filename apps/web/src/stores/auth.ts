import { create } from "zustand";
import { persist } from "zustand/middleware";
import type { User, WorkspaceBrief } from "@/api/types";

interface AuthState {
  token: string | null;
  refreshToken: string | null;
  user: User | null;
  workspaceId: string | null;
  workspaces: WorkspaceBrief[];
  setAuth: (token: string, user: User, workspaces: WorkspaceBrief[], refreshToken?: string | null) => void;
  setTokens: (token: string, refreshToken: string) => void;
  setWorkspace: (workspaceId: string) => void;
  logout: () => void;
}

export const useAuthStore = create<AuthState>()(
  persist(
    (set) => ({
      token: null,
      refreshToken: null,
      user: null,
      workspaceId: null,
      workspaces: [],
      setAuth: (token, user, workspaces, refreshToken = null) =>
        set((s) => ({
          token,
          refreshToken,
          user,
          workspaces,
          workspaceId:
            s.workspaceId && workspaces.some((w) => w.id === s.workspaceId)
              ? s.workspaceId
              : (workspaces[0]?.id ?? null),
        })),
      setTokens: (token, refreshToken) => set({ token, refreshToken }),
      setWorkspace: (workspaceId) => set({ workspaceId }),
      logout: () =>
        set({ token: null, refreshToken: null, user: null, workspaceId: null, workspaces: [] }),
    }),
    { name: "smartchat.auth" },
  ),
);
