import type { ReactNode } from "react";
import { Navigate, useLocation } from "react-router-dom";
import { useAuthStore } from "@/stores/auth";

export function RequireAuth({ children }: { children: ReactNode }) {
  const token = useAuthStore((s) => s.token);
  const location = useLocation();
  if (!token) {
    return <Navigate to="/login" state={{ from: location.pathname }} replace />;
  }
  return <>{children}</>;
}
