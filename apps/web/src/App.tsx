import {
  AimOutlined,
  ApiOutlined,
  AppstoreOutlined,
  ContactsOutlined,
  DatabaseOutlined,
  GlobalOutlined,
  MessageOutlined,
  PartitionOutlined,
  ReadOutlined,
  RobotOutlined,
  SafetyOutlined,
  ScheduleOutlined,
  TagsOutlined,
  TeamOutlined,
  ThunderboltOutlined,
} from "@ant-design/icons";
import { Button, Result } from "antd";
import { Navigate, Route, Routes, useNavigate } from "react-router-dom";
import { AppShell } from "@/components/AppShell";
import { ComingSoon } from "@/components/ComingSoon";
import { RequireAuth } from "@/components/RequireAuth";
import { SectionLayout } from "@/components/SectionLayout";
import { t } from "@/i18n";
import { LoginPage } from "@/pages/auth/LoginPage";
import { RegisterPage } from "@/pages/auth/RegisterPage";
import { CustomersPage } from "@/pages/customers/CustomersPage";
import { CustomFieldsPage } from "@/pages/customers/CustomFieldsPage";
import { QuickRepliesPage } from "@/pages/customers/QuickRepliesPage";
import { TagsPage } from "@/pages/customers/TagsPage";
import { AiMembersPage } from "@/pages/ai/AiMembersPage";
import { IntentsPage } from "@/pages/ai/IntentsPage";
import { KnowledgePage } from "@/pages/ai/KnowledgePage";
import { FlowEditor } from "@/pages/automation/FlowEditor";
import { FlowsPage } from "@/pages/automation/FlowsPage";
import { KeywordDictsPage } from "@/pages/automation/KeywordDictsPage";
import { InboxPage } from "@/pages/inbox/InboxPage";
import { ChannelsPage } from "@/pages/integrations/ChannelsPage";
import { WidgetConfigPage } from "@/pages/integrations/WidgetConfigPage";
import { WidgetsPage } from "@/pages/integrations/WidgetsPage";
import { ConversationSettingsPage } from "@/pages/settings/ConversationSettingsPage";
import { DeveloperPage } from "@/pages/settings/DeveloperPage";
import { GroupsPage } from "@/pages/team/GroupsPage";
import { MembersPage } from "@/pages/team/MembersPage";
import { RolesPage } from "@/pages/team/RolesPage";
import { ShiftsPage } from "@/pages/team/ShiftsPage";

function NotFound() {
  const navigate = useNavigate();
  return (
    <Result
      status="404"
      title={t("common.notFound")}
      subTitle={t("common.notFoundDesc")}
      extra={
        <Button type="primary" onClick={() => navigate("/inbox")}>
          {t("common.backHome")}
        </Button>
      }
    />
  );
}

export function App() {
  return (
    <Routes>
      <Route path="/login" element={<LoginPage />} />
      <Route path="/register" element={<RegisterPage />} />

      <Route
        element={
          <RequireAuth>
            <AppShell />
          </RequireAuth>
        }
      >
        <Route index element={<Navigate to="/inbox" replace />} />

        <Route path="/inbox" element={<InboxPage />} />
        <Route path="/inbox/:conversationId" element={<InboxPage />} />

        <Route
          path="/customers"
          element={
            <SectionLayout
              title={t("cust.title")}
              items={[
                { key: "/customers", label: t("cust.nav.list"), icon: <ContactsOutlined /> },
                { key: "/customers/tags", label: t("cust.nav.tags"), icon: <TagsOutlined /> },
                {
                  key: "/customers/quick-replies",
                  label: t("cust.nav.quickReplies"),
                  icon: <ThunderboltOutlined />,
                },
                {
                  key: "/customers/custom-fields",
                  label: t("cust.nav.customFields"),
                  icon: <DatabaseOutlined />,
                },
              ]}
            />
          }
        >
          <Route index element={<CustomersPage />} />
          <Route path="tags" element={<TagsPage />} />
          <Route path="quick-replies" element={<QuickRepliesPage />} />
          <Route path="custom-fields" element={<CustomFieldsPage />} />
        </Route>

        <Route path="/marketing" element={<ComingSoon title={t("nav.marketing")} />} />

        {/* full-screen flow canvas — sibling of the section layout so it fills the shell */}
        <Route path="/automation/flows/:flowId" element={<FlowEditor />} />
        <Route
          path="/automation"
          element={
            <SectionLayout
              title={t("auto.title")}
              items={[
                { key: "/automation", label: t("auto.nav.flows"), icon: <PartitionOutlined /> },
                { key: "/automation/keywords", label: t("auto.nav.keywords"), icon: <ReadOutlined /> },
                { key: "/automation/ai-members", label: t("auto.nav.aiMembers"), icon: <RobotOutlined /> },
                { key: "/automation/knowledge", label: t("auto.nav.knowledge"), icon: <DatabaseOutlined /> },
                { key: "/automation/intents", label: t("auto.nav.intents"), icon: <AimOutlined /> },
              ]}
            />
          }
        >
          <Route index element={<FlowsPage />} />
          <Route path="keywords" element={<KeywordDictsPage />} />
          <Route path="ai-members" element={<AiMembersPage />} />
          <Route path="knowledge" element={<KnowledgePage />} />
          <Route path="intents" element={<IntentsPage />} />
        </Route>

        <Route path="/reports" element={<ComingSoon title={t("nav.reports")} />} />

        <Route
          path="/integrations"
          element={
            <SectionLayout
              title={t("int.title")}
              items={[
                { key: "/integrations", label: t("int.nav.channels"), icon: <AppstoreOutlined /> },
                { key: "/integrations/widgets", label: t("int.nav.widgets"), icon: <GlobalOutlined /> },
                { key: "/integrations/appstore", label: t("int.nav.appstore"), icon: <ApiOutlined /> },
              ]}
            />
          }
        >
          <Route index element={<ChannelsPage />} />
          <Route path="widgets" element={<WidgetsPage />} />
          <Route path="widgets/:id" element={<WidgetConfigPage />} />
          <Route
            path="appstore"
            element={<ComingSoon title={t("appstore.title")} description={t("appstore.hint")} />}
          />
        </Route>

        <Route
          path="/team"
          element={
            <SectionLayout
              title={t("team.title")}
              items={[
                { key: "/team", label: t("team.nav.members"), icon: <TeamOutlined /> },
                { key: "/team/roles", label: t("team.nav.roles"), icon: <SafetyOutlined /> },
                { key: "/team/groups", label: t("team.nav.groups"), icon: <ContactsOutlined /> },
                { key: "/team/shifts", label: t("team.nav.shifts"), icon: <ScheduleOutlined /> },
              ]}
            />
          }
        >
          <Route index element={<MembersPage />} />
          <Route path="roles" element={<RolesPage />} />
          <Route path="groups" element={<GroupsPage />} />
          <Route path="shifts" element={<ShiftsPage />} />
        </Route>

        <Route
          path="/settings"
          element={
            <SectionLayout
              title={t("set.title")}
              items={[
                {
                  key: "/settings/conversation",
                  label: t("set.nav.conversation"),
                  icon: <MessageOutlined />,
                },
                {
                  key: "/settings/custom-fields",
                  label: t("set.nav.customFields"),
                  icon: <DatabaseOutlined />,
                },
                { key: "/settings/developer", label: t("set.nav.developer"), icon: <ApiOutlined /> },
              ]}
            />
          }
        >
          <Route index element={<Navigate to="/settings/conversation" replace />} />
          <Route path="conversation" element={<ConversationSettingsPage />} />
          <Route path="custom-fields" element={<CustomFieldsPage />} />
          <Route path="developer" element={<DeveloperPage />} />
        </Route>

        <Route
          path="/subscription"
          element={<ComingSoon title={t("sub.title")} description={t("sub.hint")} />}
        />

        <Route path="*" element={<NotFound />} />
      </Route>
    </Routes>
  );
}
