/** Two-level module layout: secondary nav on the left, routed content right.
 *  Used by 客戶/整合/團隊/設定. */
import { Menu } from "antd";
import type { ReactNode } from "react";
import { Outlet, useLocation, useNavigate } from "react-router-dom";

export interface SectionNavItem {
  key: string; // route path
  label: string;
  icon?: ReactNode;
}

export function SectionLayout({ title, items }: { title: string; items: SectionNavItem[] }) {
  const navigate = useNavigate();
  const { pathname } = useLocation();

  // longest matching prefix wins so /customers doesn't swallow /customers/tags
  const selected = items
    .filter((i) => pathname === i.key || pathname.startsWith(`${i.key}/`))
    .sort((a, b) => b.key.length - a.key.length)[0]?.key;

  return (
    <div className="sc-section-layout">
      <nav className="sc-section-nav" aria-label={title}>
        <div className="sc-section-nav-title">{title}</div>
        <Menu
          mode="inline"
          selectedKeys={selected ? [selected] : []}
          onClick={(e) => navigate(e.key)}
          items={items.map((i) => ({ key: i.key, label: i.label, icon: i.icon }))}
          style={{ border: "none", background: "transparent" }}
        />
      </nav>
      <div className="sc-section-content">
        <Outlet />
      </div>
    </div>
  );
}
