import { BrowserRouter, Navigate, Route, Routes } from "react-router-dom";
import { AuthProvider, useAuth } from "./context/AuthContext";
import { BrandingProvider } from "./context/BrandingContext";
import { ToastProvider } from "./components/ui/Toast";
import AppLayout from "./layouts/AppLayout";
import Login from "./pages/auth/Login";
import Dashboard from "./pages/Dashboard";
import WhatsAppAccounts from "./pages/whatsapp/WhatsAppAccounts";
import Compliance from "./pages/compliance/Compliance";
import Contacts from "./pages/contacts/Contacts";
import ContactProfile from "./pages/contacts/ContactProfile";
import Templates from "./pages/templates/Templates";
import Campaigns from "./pages/campaigns/Campaigns";
import CampaignWizard from "./pages/campaigns/CampaignWizard";
import CampaignDetail from "./pages/campaigns/CampaignDetail";
import Inbox from "./pages/inbox/Inbox";
import Automation from "./pages/automation/Automation";
import Pipeline from "./pages/pipeline/Pipeline";
import Tasks from "./pages/tasks/Tasks";
import AIAgent from "./pages/ai/AIAgent";
import Knowledge from "./pages/knowledge/Knowledge";
import VoiceAgent from "./pages/voice/VoiceAgent";
import Team from "./pages/team/Team";
import Branding from "./pages/branding/Branding";
import Plans from "./pages/plans/Plans";
import Customers from "./pages/customers/Customers";
import Reports from "./pages/reports/Reports";
import Credits from "./pages/credits/Credits";
import Users from "./pages/users/Users";
import Settings from "./pages/Settings";
import BusinessProfile from "./pages/business/BusinessProfile";
import Spinner from "./components/ui/Spinner";

function ProtectedRoute({ children }) {
    const { user, loading } = useAuth();

    if (loading) {
        return (
            <div className="min-h-screen flex items-center justify-center bg-ink-50">
                <Spinner />
            </div>
        );
    }
    if (!user) return <Navigate to="/login" replace />;
    return children;
}

function AppRoutes() {
    return (
        <Routes>
            <Route path="/login" element={<Login />} />

            <Route
                path="/"
                element={
                    <ProtectedRoute>
                        <AppLayout />
                    </ProtectedRoute>
                }
            >
                <Route index element={<Dashboard />} />
                <Route path="reports" element={<Reports />} />
                <Route path="inbox" element={<Inbox />} />
                <Route path="campaigns" element={<Campaigns />} />
                <Route path="campaigns/new" element={<CampaignWizard />} />
                <Route path="campaigns/:id" element={<CampaignDetail />} />
                <Route path="contacts" element={<Contacts />} />
                <Route path="contacts/:id" element={<ContactProfile />} />
                <Route path="pipeline" element={<Pipeline />} />
                <Route path="tasks" element={<Tasks />} />
                <Route path="automation" element={<Automation />} />
                <Route path="ai" element={<AIAgent />} />
                <Route path="knowledge" element={<Knowledge />} />
                <Route path="voice" element={<VoiceAgent />} />
                <Route path="team" element={<Team />} />
                <Route path="templates" element={<Templates />} />
                <Route path="whatsapp" element={<WhatsAppAccounts />} />
                <Route path="compliance" element={<Compliance />} />
                <Route path="business" element={<BusinessProfile />} />
                <Route path="credits" element={<Credits />} />
                <Route path="users" element={<Users />} />
                <Route path="branding" element={<Branding />} />
                <Route path="plans" element={<Plans />} />
                <Route path="customers" element={<Customers />} />
                <Route path="settings" element={<Settings />} />
            </Route>

            <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
    );
}

function App() {
    return (
        <BrowserRouter>
            <AuthProvider>
                <BrandingProvider>
                    <ToastProvider>
                        <AppRoutes />
                    </ToastProvider>
                </BrandingProvider>
            </AuthProvider>
        </BrowserRouter>
    );
}

export default App;
