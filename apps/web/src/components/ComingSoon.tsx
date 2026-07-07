import { RocketOutlined } from "@ant-design/icons";
import { Tag } from "antd";
import { t } from "@/i18n";

/** Nice placeholder page for P2/P3 modules. */
export function ComingSoon({ title, description }: { title: string; description?: string }) {
  return (
    <div className="sc-page">
      <div className="sc-page-header">
        <h1 className="sc-page-title">
          {title}{" "}
          <Tag color="blue" style={{ marginLeft: 6, verticalAlign: "2px" }}>
            {t("common.comingSoon")}
          </Tag>
        </h1>
      </div>
      <div
        className="sc-page-body"
        style={{ display: "flex", alignItems: "center", justifyContent: "center" }}
      >
        <div style={{ textAlign: "center", maxWidth: 420 }}>
          <div
            style={{
              width: 96,
              height: 96,
              margin: "0 auto 20px",
              borderRadius: 28,
              background: "var(--sc-primary-bg)",
              color: "var(--sc-primary)",
              display: "flex",
              alignItems: "center",
              justifyContent: "center",
              fontSize: 40,
            }}
          >
            <RocketOutlined />
          </div>
          <div style={{ fontSize: 18, fontWeight: 600, color: "var(--sc-text-heading)" }}>
            {title}
          </div>
          <div style={{ marginTop: 8, color: "var(--sc-text-secondary)", lineHeight: 1.7 }}>
            {description ?? t("common.comingSoonDesc")}
          </div>
        </div>
      </div>
    </div>
  );
}
