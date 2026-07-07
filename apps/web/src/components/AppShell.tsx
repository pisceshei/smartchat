/** App shell: dark navy icon rail (8 modules + bottom utilities) with
 *  expand-on-hover labels; content renders in the outlet. */
import {
  ApartmentOutlined,
  AppstoreOutlined,
  BarChartOutlined,
  BellOutlined,
  ContactsOutlined,
  CrownOutlined,
  InboxOutlined,
  LogoutOutlined,
  NotificationOutlined,
  QuestionCircleOutlined,
  SettingOutlined,
  SwapOutlined,
  TeamOutlined,
} from "@ant-design/icons";
import { Avatar, Badge, Dropdown, Popover, Tooltip } from "antd";
import type { ReactNode } from "react";
import { Outlet, useLocation, useNavigate } from "react-router-dom";
import { useRealtime } from "@/hooks/useRealtime";
import { t } from "@/i18n";
import { useAuthStore } from "@/stores/auth";
import { useRealtimeStore } from "@/stores/realtime";
import { EmptyState } from "./EmptyState";
import { LogoMark } from "./Logo";

interface RailItem {
  key: string;
  label: string;
  icon: ReactNode;
}

const MAIN_ITEMS: RailItem[] = [
  { key: "/inbox", label: t("nav.inbox"), icon: <InboxOutlined /> },
  { key: "/customers", label: t("nav.customers"), icon: <ContactsOutlined /> },
  { key: "/marketing", label: t("nav.marketing"), icon: <NotificationOutlined /> },
  { key: "/automation", label: t("nav.automation"), icon: <ApartmentOutlined /> },
  { key: "/reports", label: t("nav.reports"), icon: <BarChartOutlined /> },
  { key: "/integrations", label: t("nav.integrations"), icon: <AppstoreOutlined /> },
  { key: "/team", label: t("nav.team"), icon: <TeamOutlined /> },
  { key: "/settings", label: t("nav.settings"), icon: <SettingOutlined /> },
];

function RailButton({
  item,
  active,
  onClick,
}: {
  item: RailItem;
  active?: boolean;
  onClick?: () => void;
}) {
  return (
    <button
      type="button"
      className={`sc-rail-item${active ? " sc-active" : ""}`}
      onClick={onClick}
      aria-label={item.label}
      aria-current={active ? "page" : undefined}
    >
      {item.icon}
      <span className="sc-rail-label">{item.label}</span>
    </button>
  );
}

export function AppShell() {
  useRealtime();
  const navigate = useNavigate();
  const { pathname } = useLocation();
  const user = useAuthStore((s) => s.user);
  const workspaces = useAuthStore((s) => s.workspaces);
  const workspaceId = useAuthStore((s) => s.workspaceId);
  const setWorkspace = useAuthStore((s) => s.setWorkspace);
  const logout = useAuthStore((s) => s.logout);
  const wsStatus = useRealtimeStore((s) => s.wsStatus);

  const currentWs = workspaces.find((w) => w.id === workspaceId);

  const accountMenu = {
    items: [
      {
        key: "user",
        label: (
          <div style={{ padding: "4px 0" }}>
            <div style={{ fontWeight: 600 }}>{user?.name}</div>
            <div style={{ fontSize: 12, color: "var(--sc-text-secondary)" }}>{user?.email}</div>
          </div>
        ),
        disabled: true,
      },
      { type: "divider" as const },
      ...(workspaces.length > 1
        ? [
            {
              key: "ws-switch",
              icon: <SwapOutlined />,
              label: t("nav.workspace"),
              children: workspaces.map((w) => ({
                key: `ws:${w.id}`,
                label: w.name + (w.id === workspaceId ? " ✓" : ""),
              })),
            },
          ]
        : []),
      { key: "logout", icon: <LogoutOutlined />, label: t("nav.logout"), danger: true },
    ],
    onClick: ({ key }: { key: string }) => {
      if (key === "logout") {
        logout();
        navigate("/login");
      } else if (key.startsWith("ws:")) {
        setWorkspace(key.slice(3));
        location.reload();
      }
    },
  };

  return (
    <div className="sc-shell">
      <div className="sc-rail" role="navigation" aria-label={t("app.name")}>
        <div className="sc-rail-expander">
          <div className="sc-rail-logo">
            <LogoMark size={30} />
            <span className="sc-rail-label">{t("app.name")}</span>
          </div>
          <div className="sc-rail-group">
            {MAIN_ITEMS.map((item) => (
              <RailButton
                key={item.key}
                item={item}
                active={pathname === item.key || pathname.startsWith(`${item.key}/`)}
                onClick={() => navigate(item.key)}
              />
            ))}
          </div>
          <div className="sc-rail-spacer" />
          <div className="sc-rail-divider" />
          <div className="sc-rail-group">
            <RailButton
              item={{ key: "/subscription", label: t("nav.subscription"), icon: <CrownOutlined /> }}
              active={pathname.startsWith("/subscription")}
              onClick={() => navigate("/subscription")}
            />
            <Popover
              placement="rightBottom"
              trigger="click"
              content={
                <div style={{ width: 280 }}>
                  <EmptyState
                    compact
                    icon={<BellOutlined />}
                    title={t("shell.notifications.empty")}
                  />
                </div>
              }
            >
              <button type="button" className="sc-rail-item" aria-label={t("nav.notifications")}>
                <BellOutlined />
                <span className="sc-rail-label">{t("nav.notifications")}</span>
              </button>
            </Popover>
            <Popover
              placement="rightBottom"
              trigger="click"
              content={
                <div style={{ display: "flex", flexDirection: "column", gap: 8, width: 180 }}>
                  <a href="https://chat.chilling.com.hk" target="_blank" rel="noreferrer">
                    {t("shell.help.docs")}
                  </a>
                  <a onClick={() => navigate("/settings/developer")}>{t("shell.help.api")}</a>
                </div>
              }
            >
              <button type="button" className="sc-rail-item" aria-label={t("nav.help")}>
                <QuestionCircleOutlined />
                <span className="sc-rail-label">{t("nav.help")}</span>
              </button>
            </Popover>
            <Dropdown menu={accountMenu} placement="topRight" trigger={["click"]}>
              <button type="button" className="sc-rail-item" aria-label={t("nav.account")}>
                <Badge
                  dot
                  color={wsStatus === "online" ? "var(--sc-success)" : "var(--sc-warning)"}
                  offset={[-2, 18]}
                >
                  <Avatar size={22} style={{ background: "var(--sc-primary)", fontSize: 12 }}>
                    {(user?.name ?? "?").slice(0, 1).toUpperCase()}
                  </Avatar>
                </Badge>
                <span className="sc-rail-label">
                  {user?.name}
                  {currentWs && (
                    <span style={{ display: "block", fontSize: 11, opacity: 0.7 }}>
                      {currentWs.name}
                    </span>
                  )}
                </span>
              </button>
            </Dropdown>
          </div>
        </div>
      </div>

      <div className="sc-shell-content">
        {wsStatus === "offline" && (
          <Tooltip title={t("shell.ws.offline")}>
            <div
              role="status"
              style={{
                background: "var(--sc-warning-bg)",
                color: "var(--sc-warning)",
                fontSize: 12.5,
                textAlign: "center",
                padding: "3px 8px",
                flex: "none",
              }}
            >
              {t("shell.ws.offline")}
            </div>
          </Tooltip>
        )}
        <Outlet />
      </div>
    </div>
  );
}
