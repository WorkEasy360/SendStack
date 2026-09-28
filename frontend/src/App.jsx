import { Suspense, lazy } from "react";
import { BrowserRouter, Navigate, Route, Routes, useLocation } from "react-router-dom";
import { AuthProvider, useAuth } from "./context/AuthContext";
import { BrandingProvider } from "./context/BrandingContext";
import { EntitlementsProvider } from "./context/EntitlementsContext";
import { ToastProvider } from "./components/ui/Toast";
import AppLayout from "./layouts/AppLayout";
import Spinner from "./components/ui/Spinner";

/*
  Every page below this line is loaded on demand.

  Statically imported, the whole product was one 954 kB bundle (275 kB
  gzipped): somebody opening the login page downloaded the campaign wizard,
  the CRM pipeline, the knowledge base and the reseller's plan editor before
  they could type a password. On the mobile connection a field sales user
  actually has, that is the difference between a login screen that appears and
  one that is still blank.

  Splitting is by *route*, not by component. A route is a real boundary -- the
  user navigates to it, there is somewhere sensible to show a spinner, and the
  chunk is big enough to be worth a request. Splitting individual components
  would trade one large download for dozens of small ones and be slower.

  Login is the one page kept eagerly loaded, because it is the first thing
  almost every session renders and a lazy boundary there would show a spinner
  before the login form on every cold visit -- paying the split's cost exactly
  where there is nothing to gain.
*/
import Login from "./pages/auth/Login";

const Signup = lazy(() => import("./pages/auth/Signup"));
const ForgotPassword = lazy(() => import("./pages/auth/ForgotPassword"));
const ResetPassword = lazy(() => import("./pages/auth/ResetPassword"));
const LegalDocument = lazy(() => import("./pages/legal/LegalDocument"));
const Pricing = lazy(() => import("./pages/public/Pricing"));
const Landing = lazy(() => import("./pages/public/Landing"));
const Trust = lazy(() => import("./pages/public/Trust"));
const Dashboard = lazy(() => import("./pages/Dashboard"));
const WhatsAppAccounts = lazy(() => import("./pages/whatsapp/WhatsAppAccounts"));
const Compliance = lazy(() => import("./pages/compliance/Compliance"));
const Contacts = lazy(() => import("./pages/contacts/Contacts"));
const ContactProfile = lazy(() => import("./pages/contacts/ContactProfile"));
const NumberFilter = lazy(() => import("./pages/leadtools/NumberFilter"));
const GroupGrabber = lazy(() => import("./pages/leadtools/GroupGrabber"));
const Templates = lazy(() => import("./pages/templates/Templates"));
const Campaigns = lazy(() => import("./pages/campaigns/Campaigns"));
const CampaignWizard = lazy(() => import("./pages/campaigns/CampaignWizard"));
const CampaignDetail = lazy(() => import("./pages/campaigns/CampaignDetail"));
const Inbox = lazy(() => import("./pages/inbox/Inbox"));
const Automation = lazy(() => import("./pages/automation/Automation"));
const Chatbot = lazy(() => import("./pages/chatbot/Chatbot"));
const Pipeline = lazy(() => import("./pages/pipeline/Pipeline"));
const Tasks = lazy(() => import("./pages/tasks/Tasks"));
const AIAgent = lazy(() => import("./pages/ai/AIAgent"));
const Knowledge = lazy(() => import("./pages/knowledge/Knowledge"));
const VoiceAgent = lazy(() => import("./pages/voice/VoiceAgent"));
const Team = lazy(() => import("./pages/team/Team"));
const Branding = lazy(() => import("./pages/branding/Branding"));
const Plans = lazy(() => import("./pages/plans/Plans"));
const Billing = lazy(() => import("./pages/billing/Billing"));
const Onboarding = lazy(() => import("./pages/onboarding/Onboarding"));
const Customers = lazy(() => import("./pages/customers/Customers"));
const Reports = lazy(() => import("./pages/reports/Reports"));
const Credits = lazy(() => import("./pages/credits/Credits"));
const Users = lazy(() => import("./pages/users/Users"));
const Settings = lazy(() => import("./pages/Settings"));
const Developer = lazy(() => import("./pages/developer/Developer"));
const BusinessProfile = lazy(() => import("./pages/business/BusinessProfile"));
const Support = lazy(() => import("./pages/support/Support"));
const BusinessDashboard = lazy(() => import("./pages/admin/BusinessDashboard"));

function FullPageSpinner() {
    return (
        <div className="min-h-screen flex items-center justify-center bg-ink-50">
            <Spinner />
        </div>
    );
}

/**
 * What `/` is, which depends entirely on who is asking.
 *
 * Signed in, it is the application shell and the dashboard inside it. Signed
 * out, it is the marketing landing page -- **not** a redirect to /login, which
 * is what it used to be and which meant the product had no front door at all.
 * A visitor who had heard the name and typed it in was shown a password box.
 *
 * The pathname check is what keeps this honest. Only the root itself falls
 * through to the landing page; every other route under this layout still sends
 * a signed-out visitor to /login, so this cannot become an accidental way to
 * render an authenticated screen's chrome to somebody with no session.
 */
function RootRoute() {
    const { user, loading } = useAuth();
    const location = useLocation();

    if (loading) return <FullPageSpinner />;
    if (user) return <AppLayout />;
    if (location.pathname === "/") return <Landing />;
    return <Navigate to="/login" replace />;
}

function AppRoutes() {
    return (
        // One Suspense boundary around the whole route table rather than one
        // per route. A route transition replaces the page anyway, so a single
        // fallback is what the user sees either way -- and thirty boundaries
        // would be thirty places for the fallback to drift apart.
        <Suspense fallback={<FullPageSpinner />}>
        <Routes>
            <Route path="/login" element={<Login />} />
            <Route path="/signup" element={<Signup />} />
            <Route path="/forgot-password" element={<ForgotPassword />} />
            <Route path="/reset-password" element={<ResetPassword />} />

            {/*
              Public, and it has to be: somebody deciding whether to sign up
              reads the prices before they have an account. The plans, their
              limits and their features all come from the API, so this page
              cannot promise a limit the send gate does not honour.
            */}
            <Route path="/pricing" element={<Pricing />} />

            {/*
              Public for the same reason as /pricing: the buyer who reads a
              security page is reading it before they have an account, and a
              trust page behind a login is not a trust page.
            */}
            <Route path="/trust" element={<Trust />} />

            {/*
              Public and outside RootRoute on purpose. Somebody deciding
              whether to sign up has to be able to read the terms before they
              have an account, and a link to /privacy in an email must open for
              a recipient who is not a user of this workspace at all.

              These are the only unauthenticated application routes. Everything
              else stays inside RootRoute below, which sends a signed-out
              visitor to /login for every path except the root itself.
              The catch-all
              redirect at the bottom still sends an unknown path home.
            */}
            <Route path="/terms" element={<LegalDocument slug="terms" />} />
            <Route path="/privacy" element={<LegalDocument slug="privacy" />} />
            <Route path="/dpa" element={<LegalDocument slug="dpa" />} />

            {/* Signed in: the app shell. Signed out: the landing page. See RootRoute. */}
            <Route path="/" element={<RootRoute />}>
                <Route index element={<Dashboard />} />
                <Route path="reports" element={<Reports />} />
                <Route path="inbox" element={<Inbox />} />
                <Route path="campaigns" element={<Campaigns />} />
                <Route path="campaigns/new" element={<CampaignWizard />} />
                <Route path="campaigns/:id" element={<CampaignDetail />} />
                <Route path="contacts" element={<Contacts />} />
                <Route path="contacts/:id" element={<ContactProfile />} />
                <Route path="number-filter" element={<NumberFilter />} />
                <Route path="group-grabber" element={<GroupGrabber />} />
                <Route path="pipeline" element={<Pipeline />} />
                <Route path="tasks" element={<Tasks />} />
                <Route path="automation" element={<Automation />} />
                <Route path="chatbot" element={<Chatbot />} />
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
                {/* The customer's own billing. `plans` above is the reseller's
                    plan editor -- a different screen for a different person. */}
                <Route path="billing" element={<Billing />} />
                <Route path="onboarding" element={<Onboarding />} />
                <Route path="customers" element={<Customers />} />
                <Route path="support" element={<Support />} />
                {/*
                  Superadmin-only in practice: the API returns 404 to anybody
                  else, so a customer who guesses the path gets an empty page
                  rather than a partial one. The nav link is hidden too, but
                  the server is what enforces it.
                */}
                <Route path="business-dashboard" element={<BusinessDashboard />} />
                <Route path="settings" element={<Settings />} />
                <Route path="developer" element={<Developer />} />
            </Route>

            <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
        </Suspense>
    );
}

function App() {
    return (
        <BrowserRouter>
            <AuthProvider>
                <BrandingProvider>
                    <EntitlementsProvider>
                        <ToastProvider>
                            <AppRoutes />
                        </ToastProvider>
                    </EntitlementsProvider>
                </BrandingProvider>
            </AuthProvider>
        </BrowserRouter>
    );
}

export default App;
