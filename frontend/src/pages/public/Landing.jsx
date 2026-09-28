import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import {
    ArrowRight,
    BarChart3,
    Bot,
    Check,
    Inbox,
    ListChecks,
    MessageSquare,
    ShieldCheck,
    Smartphone,
    Upload,
    Users,
    Workflow,
    Zap,
} from "lucide-react";
import { useBranding } from "../../context/BrandingContext";
import api from "../../services/api";
import Button from "../../components/ui/Button";
import { LogoMark } from "../../components/layout/Logo";
import { formatMoney } from "../../utils/plans";
import { useSeo } from "../../hooks/useSeo";

/**
 * The page somebody who has never heard of this product lands on.
 *
 * Two rules shape everything here, and both are about not writing cheques the
 * product cannot cash.
 *
 * **Every number comes from the API.** Prices, trial length, whether signup is
 * even open -- all of it is read from `GET /billing/pricing/`, the same
 * endpoint the pricing page uses and the same `Plan` rows the send gate
 * enforces. There is no marketing copy in this file that states a limit, a
 * price or an allowance, because a landing page that promises what the
 * entitlement service refuses is a support ticket with a signup form attached.
 *
 * **Every claim is one the code actually supports.** No delivery guarantee: we
 * hand messages to WhatsApp and report what WhatsApp says back, and nobody in
 * this stack can promise what Meta does next. No "unlimited": every plan has
 * limits and they are printed. No compliance certification: the trust page
 * lists implemented controls and stops there. The strongest honest claim about
 * deliverability is that consent is enforced before a send, which is true, is
 * enforced server-side, and is worth more to a serious buyer than a number
 * nobody could stand behind.
 *
 * The route is public and is what `/` renders for a signed-out visitor. A
 * signed-in one is sent to their dashboard before this ever mounts.
 */
function Landing() {
    const { branding } = useBranding();
    const [pricing, setPricing] = useState(null);

    const product = branding.product_name;

    useSeo({
        title: `${product} — WhatsApp campaigns your customers actually agreed to`,
        description:
            "Send WhatsApp campaigns to contacts who opted in, answer replies in a shared inbox, " +
            "and see what happened. Consent is checked before every send.",
        canonical: "/",
        index: true,
    });

    useEffect(() => {
        let cancelled = false;
        api.get("/billing/pricing/")
            .then((response) => {
                if (!cancelled) setPricing(response.data);
            })
            .catch(() => {
                // A landing page that cannot reach the API still has to render.
                // It simply stops quoting prices rather than inventing one.
                if (!cancelled) setPricing(null);
            });
        return () => {
            cancelled = true;
        };
    }, []);

    const signupEnabled = pricing?.signup_enabled !== false;
    const trialDays = pricing?.trials_enabled ? pricing.trial_days : 0;
    const cheapest = (pricing?.plans || [])
        .slice()
        .sort((a, b) => (a.price_minor || 0) - (b.price_minor || 0))[0];

    const primaryCta = trialDays > 0 ? `Start your ${trialDays}-day free trial` : "Create your workspace";

    return (
        <div className="min-h-screen bg-surface">
            <SiteHeader product={product} signupEnabled={signupEnabled} />

            <main>
                <Hero
                    product={product}
                    cta={primaryCta}
                    signupEnabled={signupEnabled}
                    trialDays={trialDays}
                />
                <Problem />
                <Solution />
                <HowItWorks />
                <Demo product={product} />
                <Features />
                <UseCases />
                <PricingTeaser
                    pricing={pricing}
                    cheapest={cheapest}
                    trialDays={trialDays}
                    signupEnabled={signupEnabled}
                />
                <Trust />
                <Faq product={product} trialDays={trialDays} />
                <FinalCta cta={primaryCta} signupEnabled={signupEnabled} product={product} />
            </main>

            <SiteFooter product={product} supportEmail={branding.support_email} />
        </div>
    );
}

/* -------------------------------------------------------------------------- */
/* Chrome                                                                      */
/* -------------------------------------------------------------------------- */

function SiteHeader({ product, signupEnabled }) {
    return (
        <header className="sticky top-0 z-20 border-b border-ink-200 bg-surface/90 backdrop-blur">
            <div className="max-w-6xl mx-auto px-4 sm:px-6 py-3.5 flex items-center justify-between gap-4">
                <Link to="/" className="flex items-center gap-2.5 no-underline">
                    <LogoMark size={30} />
                    <span className="font-bold text-ink-900">{product}</span>
                </Link>
                <nav className="flex items-center gap-1 sm:gap-2">
                    <a
                        href="#how"
                        className="hidden sm:inline-block px-3 py-2 text-sm text-ink-600 hover:text-ink-900 no-underline"
                    >
                        How it works
                    </a>
                    <Link
                        to="/pricing"
                        className="hidden sm:inline-block px-3 py-2 text-sm text-ink-600 hover:text-ink-900 no-underline"
                    >
                        Pricing
                    </Link>
                    <Link
                        to="/trust"
                        className="hidden sm:inline-block px-3 py-2 text-sm text-ink-600 hover:text-ink-900 no-underline"
                    >
                        Security
                    </Link>
                    <Link to="/login">
                        <Button variant="ghost" size="sm">Sign in</Button>
                    </Link>
                    {signupEnabled && (
                        <Link to="/signup">
                            <Button size="sm">Get started</Button>
                        </Link>
                    )}
                </nav>
            </div>
        </header>
    );
}

function SiteFooter({ product, supportEmail }) {
    return (
        <footer className="border-t border-ink-200 bg-ink-50">
            <div className="max-w-6xl mx-auto px-4 sm:px-6 py-10">
                <div className="flex flex-wrap gap-8 justify-between">
                    <div className="max-w-xs">
                        <div className="flex items-center gap-2.5 mb-2">
                            <LogoMark size={26} />
                            <span className="font-bold text-ink-900">{product}</span>
                        </div>
                        <p className="text-sm text-ink-500 m-0">
                            WhatsApp campaigns, a shared inbox and automation for businesses that
                            message customers who asked to hear from them.
                        </p>
                    </div>
                    <FooterColumn
                        title="Product"
                        links={[
                            { to: "/pricing", label: "Pricing" },
                            { to: "/trust", label: "Security and trust" },
                            { to: "/login", label: "Sign in" },
                        ]}
                    />
                    <FooterColumn
                        title="Legal"
                        links={[
                            { to: "/terms", label: "Terms" },
                            { to: "/privacy", label: "Privacy" },
                            { to: "/dpa", label: "Data processing" },
                        ]}
                    />
                </div>
                <div className="mt-10 pt-6 border-t border-ink-200 text-xs text-ink-400 flex flex-wrap gap-x-3 gap-y-1">
                    <span>© {new Date().getFullYear()} {product}</span>
                    {supportEmail && (
                        <a href={`mailto:${supportEmail}`} className="text-ink-400 hover:text-ink-600">
                            {supportEmail}
                        </a>
                    )}
                    <span>
                        Not affiliated with or endorsed by WhatsApp or Meta.
                    </span>
                </div>
            </div>
        </footer>
    );
}

function FooterColumn({ title, links }) {
    return (
        <div>
            <h3 className="text-xs font-bold uppercase tracking-wide text-ink-400 m-0 mb-3">{title}</h3>
            <ul className="list-none p-0 m-0 space-y-2">
                {links.map((link) => (
                    <li key={link.to}>
                        <Link to={link.to} className="text-sm text-ink-600 hover:text-ink-900 no-underline">
                            {link.label}
                        </Link>
                    </li>
                ))}
            </ul>
        </div>
    );
}

/* -------------------------------------------------------------------------- */
/* The first screen                                                            */
/* -------------------------------------------------------------------------- */

/**
 * Answers four questions above the fold, in this order: what is this, who is
 * it for, why should I care, what do I do next. Anything that does not serve
 * one of those four is below it.
 */
function Hero({ product, cta, signupEnabled, trialDays }) {
    return (
        <section className="border-b border-ink-200 bg-gradient-to-b from-brand-50/60 to-white">
            <div className="max-w-6xl mx-auto px-4 sm:px-6 py-16 sm:py-24 grid gap-12 lg:grid-cols-2 lg:items-center">
                <div>
                    <span className="inline-block text-xs font-bold uppercase tracking-wide text-brand-700 bg-brand-100 rounded-full px-3 py-1 mb-5">
                        For small businesses and agencies
                    </span>
                    <h1 className="text-4xl sm:text-5xl font-bold text-ink-900 leading-tight m-0 mb-5">
                        WhatsApp campaigns your customers actually agreed to
                    </h1>
                    <p className="text-lg text-ink-600 m-0 mb-4 leading-relaxed">
                        Import your contacts, write one message, and send it to the people who opted
                        in — never to the ones who opted out. Replies land in a shared inbox your
                        whole team can answer.
                    </p>
                    <p className="text-base text-ink-500 m-0 mb-8">
                        {product} checks consent, your plan limits and your credit balance
                        <em> before </em>
                        a single message leaves. You see exactly who is eligible before you press send.
                    </p>

                    <div className="flex flex-wrap items-center gap-3">
                        {signupEnabled ? (
                            <Link to="/signup" className="no-underline">
                                <Button size="lg" className="gap-2">
                                    {cta}
                                    <ArrowRight size={18} aria-hidden="true" />
                                </Button>
                            </Link>
                        ) : (
                            <Link to="/login" className="no-underline">
                                <Button size="lg">Sign in</Button>
                            </Link>
                        )}
                        <Link to="/pricing" className="no-underline">
                            <Button variant="secondary" size="lg">See pricing</Button>
                        </Link>
                    </div>

                    <p className="text-sm text-ink-400 mt-4 mb-0">
                        {trialDays > 0
                            ? `No card needed to start. ${trialDays} days, then choose a plan.`
                            : "Choose a plan when you are ready."}
                    </p>
                </div>

                <HeroPreview />
            </div>
        </section>
    );
}

/**
 * A still of the one screen that carries the product's argument: the send
 * confirmation, where eligibility is resolved before anything goes out.
 *
 * Drawn rather than screenshotted so it cannot go stale against a UI change,
 * and so it never has to be redacted -- every name and number in it is
 * invented, and the phone numbers sit in a documentation range.
 */
function HeroPreview() {
    return (
        <div className="relative">
            <div className="rounded-card border border-ink-200 bg-surface shadow-raised overflow-hidden">
                <div className="border-b border-ink-200 bg-ink-50 px-4 py-2.5 flex items-center gap-2">
                    <span className="w-2.5 h-2.5 rounded-full bg-ink-300" />
                    <span className="w-2.5 h-2.5 rounded-full bg-ink-300" />
                    <span className="w-2.5 h-2.5 rounded-full bg-ink-300" />
                    <span className="text-xs text-ink-500 ml-2">Review and send</span>
                </div>
                <div className="p-5">
                    <p className="text-xs font-bold uppercase tracking-wide text-ink-400 m-0 mb-3">
                        Diwali offer · 2 lines selected
                    </p>
                    <dl className="m-0 space-y-2.5">
                        <PreviewRow label="Selected" value="1,240 contacts" />
                        <PreviewRow label="Eligible to receive" value="1,186" tone="good" />
                        <PreviewRow label="Opted out" value="38 — skipped" tone="muted" />
                        <PreviewRow label="No consent recorded" value="16 — skipped" tone="muted" />
                        <PreviewRow label="Estimated credits" value="1,186" />
                    </dl>
                    <div className="mt-5 rounded-lg bg-brand-50 border border-brand-200 px-3.5 py-3 flex gap-2.5">
                        <ShieldCheck size={17} className="text-brand-700 shrink-0 mt-0.5" aria-hidden="true" />
                        <p className="text-xs text-brand-900 m-0 leading-relaxed">
                            The 54 skipped contacts are counted here and never charged. The same rules
                            run again on the server when you send.
                        </p>
                    </div>
                </div>
            </div>
        </div>
    );
}

function PreviewRow({ label, value, tone }) {
    const valueClass =
        tone === "good" ? "text-brand-700 font-bold" : tone === "muted" ? "text-ink-400" : "text-ink-900 font-medium";
    return (
        <div className="flex items-baseline justify-between gap-4 text-sm">
            <dt className="text-ink-500 m-0">{label}</dt>
            <dd className={`m-0 ${valueClass}`}>{value}</dd>
        </div>
    );
}

/* -------------------------------------------------------------------------- */
/* The argument                                                                */
/* -------------------------------------------------------------------------- */

function Problem() {
    return (
        <Section
            eyebrow="The problem"
            title="Messaging customers on WhatsApp gets messy fast"
            lead="It works beautifully until you have more than a handful of customers. Then it stops."
        >
            <div className="grid gap-5 sm:grid-cols-3">
                <ProblemCard
                    title="Copy, paste, repeat"
                    body="Sending the same offer to four hundred people by hand takes an afternoon and produces no record of who got it."
                />
                <ProblemCard
                    title="Nobody knows who said stop"
                    body="Someone asks to be left alone, one person on the team notes it, and the next campaign messages them anyway."
                />
                <ProblemCard
                    title="Replies land on one phone"
                    body="The number is in somebody's pocket. Customers wait, and when that person is on leave, they keep waiting."
                />
            </div>
        </Section>
    );
}

function ProblemCard({ title, body }) {
    return (
        <div className="rounded-card border border-ink-200 bg-surface p-5">
            <h3 className="text-base font-bold text-ink-900 m-0 mb-2">{title}</h3>
            <p className="text-sm text-ink-500 m-0 leading-relaxed">{body}</p>
        </div>
    );
}

function Solution() {
    return (
        <Section
            tone="tinted"
            eyebrow="The solution"
            title="One workspace for the whole conversation"
            lead="Your contacts, their consent, your campaigns and every reply in one place — with the rules enforced by the server, not by whoever remembers them."
        >
            <div className="grid gap-5 sm:grid-cols-2">
                <SolutionPoint
                    icon={ShieldCheck}
                    title="Consent decided before the send, not after"
                    body="Opt-outs beat consent, consent beats eligibility, and that order is enforced in the send path itself. A campaign cannot message somebody who said stop, whatever the interface says."
                />
                <SolutionPoint
                    icon={Inbox}
                    title="Replies your team can actually answer"
                    body="Every reply arrives in a shared inbox with the contact's history beside it. Assign it, answer it, and it stays visible to everyone."
                />
                <SolutionPoint
                    icon={BarChart3}
                    title="Numbers you can act on"
                    body="Sent, delivered, read, failed and skipped — with failures grouped by reason, so 'it did not work' becomes 'the device disconnected at 4pm'."
                />
                <SolutionPoint
                    icon={Zap}
                    title="Costs you can see in advance"
                    body="Before you send, the same rules that will run on the server tell you how many people are eligible and what it will cost."
                />
            </div>
        </Section>
    );
}

function SolutionPoint({ icon: Icon, title, body }) {
    return (
        <div className="flex gap-4">
            <span className="shrink-0 w-10 h-10 rounded-lg bg-brand-100 text-brand-700 flex items-center justify-center">
                <Icon size={20} aria-hidden="true" />
            </span>
            <div>
                <h3 className="text-base font-bold text-ink-900 m-0 mb-1.5">{title}</h3>
                <p className="text-sm text-ink-500 m-0 leading-relaxed">{body}</p>
            </div>
        </div>
    );
}

/* -------------------------------------------------------------------------- */

const STEPS = [
    {
        icon: Smartphone,
        title: "Connect a WhatsApp number",
        body: "Scan a QR code with the phone you already use, or connect an official WhatsApp Business API number. The connection state is shown to you at all times.",
    },
    {
        icon: Upload,
        title: "Bring your contacts",
        body: "Import a CSV or add them one at a time. Record who opted in and when — that record is what every later send is checked against.",
    },
    {
        icon: MessageSquare,
        title: "Write once, personalise per contact",
        body: "Save it as a template with the contact's name and your own fields, add an image or a document, and reuse it.",
    },
    {
        icon: ListChecks,
        title: "Review who is actually eligible",
        body: "Before sending, see the split: eligible, opted out, missing consent, over your plan limit — and the credits it will use.",
    },
    {
        icon: BarChart3,
        title: "Send and watch it land",
        body: "Messages go out at a paced rate. Delivery and read receipts come back where the provider supports them, and failures are grouped by cause.",
    },
];

function HowItWorks() {
    return (
        <Section
            id="how"
            eyebrow="How it works"
            title="Five steps from signing up to your first campaign"
            lead="Nothing here needs a developer, and none of it can be skipped by turning off a rule."
        >
            <ol className="list-none p-0 m-0 grid gap-4 sm:grid-cols-2 lg:grid-cols-5">
                {STEPS.map((step, index) => (
                    <li
                        key={step.title}
                        className="rounded-card border border-ink-200 bg-surface p-5 flex flex-col"
                    >
                        <div className="flex items-center gap-2.5 mb-3">
                            <span className="w-8 h-8 rounded-lg bg-ink-900 text-white text-sm font-bold flex items-center justify-center shrink-0">
                                {index + 1}
                            </span>
                            <step.icon size={18} className="text-brand-600" aria-hidden="true" />
                        </div>
                        <h3 className="text-sm font-bold text-ink-900 m-0 mb-1.5">{step.title}</h3>
                        <p className="text-xs text-ink-500 m-0 leading-relaxed">{step.body}</p>
                    </li>
                ))}
            </ol>
        </Section>
    );
}

/* -------------------------------------------------------------------------- */
/* The demo                                                                    */
/* -------------------------------------------------------------------------- */

const DEMO_TABS = [
    {
        key: "connect",
        label: "Connect",
        caption: "A number is linked, and its state is never guessed.",
        render: () => (
            <DemoPanel title="WhatsApp numbers">
                <DemoRow
                    left="Sales line · +91 90000 00000"
                    right={<DemoBadge tone="good">Connected</DemoBadge>}
                    sub="Last seen a few seconds ago"
                />
                <DemoRow
                    left="Support line · +91 90000 00001"
                    right={<DemoBadge tone="warn">Reconnecting</DemoBadge>}
                    sub="Campaigns using this line pause until it is back"
                />
            </DemoPanel>
        ),
    },
    {
        key: "contacts",
        label: "Contacts",
        caption: "Consent is a field on the contact, not a note in someone's head.",
        render: () => (
            <DemoPanel title="Contacts · 1,240">
                <DemoRow left="Priya Sharma" right={<DemoBadge tone="good">Opted in</DemoBadge>} sub="Added by CSV import · 12 Mar" />
                <DemoRow left="Rahul Nair" right={<DemoBadge tone="good">Opted in</DemoBadge>} sub="Replied START on WhatsApp · 4 Apr" />
                <DemoRow left="Anita Rao" right={<DemoBadge tone="bad">Opted out</DemoBadge>} sub="Sent STOP · 19 Apr · excluded from every campaign" />
            </DemoPanel>
        ),
    },
    {
        key: "campaign",
        label: "Campaign",
        caption: "One message, personalised per contact, saved for reuse.",
        render: () => (
            <DemoPanel title="New campaign · Diwali offer">
                <div className="rounded-lg border border-ink-200 bg-ink-50 p-3.5 text-sm text-ink-700 leading-relaxed">
                    Hi <span className="bg-brand-100 text-brand-800 rounded px-1">{"{{name}}"}</span>, our
                    Diwali offer is open until Sunday — 20% off everything in store. Reply STOP to
                    stop receiving offers.
                </div>
                <p className="text-xs text-ink-400 mt-3 mb-0">
                    Audience: tag “regulars”, opted in only. Attachment: offer.jpg
                </p>
            </DemoPanel>
        ),
    },
    {
        key: "send",
        label: "Review & send",
        caption: "The eligibility split, computed by the same rules the server enforces.",
        render: () => (
            <DemoPanel title="Review and send">
                <DemoStat label="Selected" value="1,240" />
                <DemoStat label="Eligible" value="1,186" tone="good" />
                <DemoStat label="Opted out — skipped" value="38" tone="muted" />
                <DemoStat label="No consent — skipped" value="16" tone="muted" />
                <DemoStat label="Credits this will use" value="1,186" />
            </DemoPanel>
        ),
    },
    {
        key: "results",
        label: "Results",
        caption: "What happened, including the part that did not work.",
        render: () => (
            <DemoPanel title="Diwali offer · completed">
                <DemoStat label="Sent" value="1,186" />
                <DemoStat label="Delivered" value="1,151" tone="good" />
                <DemoStat label="Read" value="734" tone="good" />
                <DemoStat label="Failed" value="35" tone="bad" />
                <p className="text-xs text-ink-500 mt-3 mb-0">
                    Failures: 29 invalid number, 6 device disconnected. Failed messages are not charged.
                </p>
            </DemoPanel>
        ),
    },
];

/**
 * A five-step walkthrough of the real flow, built from illustrative panels
 * rather than a video.
 *
 * Deliberately not screenshots and deliberately not a recording. Screenshots
 * of a product under active development are stale within a release and have to
 * be scrubbed of whichever workspace they were taken in; a video needs hosting,
 * a player, and a person to re-record it every time a screen moves. These
 * panels are markup, they are reviewed in the same diff as the UI they
 * describe, and every name, number and count in them is invented.
 */
function Demo({ product }) {
    const [active, setActive] = useState(DEMO_TABS[0].key);
    const tab = DEMO_TABS.find((t) => t.key === active) || DEMO_TABS[0];

    return (
        <Section
            tone="dark"
            eyebrow="A look inside"
            title={`What ${product} looks like in use`}
            lead="Illustrative screens with sample data — the flow, end to end, in five steps."
        >
            <div
                role="tablist"
                aria-label="Product walkthrough"
                className="flex flex-wrap gap-2 mb-6"
            >
                {DEMO_TABS.map((item) => (
                    <button
                        key={item.key}
                        type="button"
                        role="tab"
                        aria-selected={item.key === active}
                        onClick={() => setActive(item.key)}
                        className={`px-3.5 py-2 rounded-lg text-sm font-medium border transition-colors ${
                            item.key === active
                                ? "bg-surface text-ink-900 border-white"
                                : "bg-transparent text-ink-300 border-ink-700 hover:text-white"
                        }`}
                    >
                        {item.label}
                    </button>
                ))}
            </div>

            <div className="grid gap-6 lg:grid-cols-[1fr_20rem] lg:items-start">
                {tab.render()}
                <p className="text-sm text-ink-300 m-0 leading-relaxed lg:pt-4">{tab.caption}</p>
            </div>
        </Section>
    );
}

function DemoPanel({ title, children }) {
    return (
        <div className="rounded-card bg-surface border border-ink-200 overflow-hidden">
            <div className="px-4 py-2.5 border-b border-ink-200 bg-ink-50">
                <span className="text-xs font-bold uppercase tracking-wide text-ink-500">{title}</span>
            </div>
            <div className="p-4 space-y-2.5">{children}</div>
        </div>
    );
}

function DemoRow({ left, right, sub }) {
    return (
        <div className="flex items-start justify-between gap-4 py-2 border-b border-ink-100 last:border-0">
            <div>
                <p className="text-sm text-ink-900 m-0 font-medium">{left}</p>
                {sub && <p className="text-xs text-ink-400 m-0 mt-0.5">{sub}</p>}
            </div>
            {right}
        </div>
    );
}

function DemoStat({ label, value, tone }) {
    const valueClass =
        tone === "good" ? "text-brand-700" : tone === "bad" ? "text-red-600" : tone === "muted" ? "text-ink-400" : "text-ink-900";
    return (
        <div className="flex items-baseline justify-between gap-4 text-sm">
            <span className="text-ink-500">{label}</span>
            <span className={`font-bold ${valueClass}`}>{value}</span>
        </div>
    );
}

function DemoBadge({ tone, children }) {
    const classes =
        tone === "good"
            ? "bg-brand-50 text-brand-700 border-brand-200"
            : tone === "warn"
              ? "bg-amber-50 text-amber-700 border-amber-200"
              : "bg-red-50 text-red-700 border-red-200";
    return (
        <span className={`shrink-0 text-xs font-medium rounded-full border px-2.5 py-0.5 ${classes}`}>
            {children}
        </span>
    );
}

/* -------------------------------------------------------------------------- */

const FEATURES = [
    {
        icon: MessageSquare,
        title: "Campaigns",
        body: "Personalised bulk sends with images, documents and buttons, paced so a number is not hammered, and schedulable for later.",
    },
    {
        icon: Users,
        title: "Contacts and CRM",
        body: "Tags, groups, custom fields, lifecycle stages, a deal pipeline and tasks — with every conversation on the contact's timeline.",
    },
    {
        icon: Inbox,
        title: "Shared inbox",
        body: "Replies in one place, assignable across your team, with the contact's whole history beside the thread.",
    },
    {
        icon: Workflow,
        title: "Automation",
        body: "Keyword replies, welcome messages and follow-ups. One inbound message gets one answer, chosen by an ordered set of rules.",
    },
    {
        icon: Bot,
        title: "AI agent",
        body: "Answers routine questions from a knowledge base you upload. It replies when it has a relevant answer and stays quiet when it does not.",
    },
    {
        icon: ShieldCheck,
        title: "Consent and compliance",
        body: "Opt-out handling, consent records, per-workspace policy, and an audit log of who did what. Enforced in the send path.",
    },
];

function Features() {
    return (
        <Section
            eyebrow="What is included"
            title="Everything in one workspace"
            lead="Which of these your plan opens up is set by the plan itself, and enforced on the server."
        >
            <div className="grid gap-5 sm:grid-cols-2 lg:grid-cols-3">
                {FEATURES.map((feature) => (
                    <div key={feature.title} className="rounded-card border border-ink-200 bg-surface p-5">
                        <feature.icon size={20} className="text-brand-600 mb-3" aria-hidden="true" />
                        <h3 className="text-base font-bold text-ink-900 m-0 mb-1.5">{feature.title}</h3>
                        <p className="text-sm text-ink-500 m-0 leading-relaxed">{feature.body}</p>
                    </div>
                ))}
            </div>
        </Section>
    );
}

/* -------------------------------------------------------------------------- */

const USE_CASES = [
    {
        who: "Local shops and showrooms",
        job: "Tell regulars about an offer without spending an afternoon on it.",
        result: "One campaign to the tag “regulars”, sent in minutes, with the people who opted out left alone.",
    },
    {
        who: "Marketing agencies",
        job: "Run WhatsApp for several clients without mixing them up.",
        result: "A separate workspace per client, your own branding on it, and usage you can see per client.",
    },
    {
        who: "Sales teams",
        job: "Follow up on leads before they go cold.",
        result: "Leads land as contacts, replies land in the shared inbox, and the pipeline shows what is still open.",
    },
    {
        who: "Coaching and education",
        job: "Get batch announcements and reminders to the right group.",
        result: "Groups per batch, scheduled sends, and a record of who received each announcement.",
    },
    {
        who: "Clinics and service businesses",
        job: "Reduce no-shows with reminders people actually read.",
        result: "Scheduled reminders from a template, with replies answered by whoever is on the desk.",
    },
    {
        who: "Online stores",
        job: "Answer “where is my order” without a person watching the phone.",
        result: "Keyword automation and an AI agent that answers from your own FAQ, and hands over when it cannot.",
    },
];

function UseCases() {
    return (
        <Section
            tone="tinted"
            eyebrow="Who it is for"
            title="Built for businesses that message people who asked to hear from them"
            lead="If your list is bought or scraped, this is the wrong product — the consent rules will simply block the send."
        >
            <div className="grid gap-5 sm:grid-cols-2 lg:grid-cols-3">
                {USE_CASES.map((item) => (
                    <div key={item.who} className="rounded-card border border-ink-200 bg-surface p-5">
                        <h3 className="text-base font-bold text-ink-900 m-0 mb-2">{item.who}</h3>
                        <p className="text-sm text-ink-600 m-0 mb-2">{item.job}</p>
                        <p className="text-sm text-ink-500 m-0 leading-relaxed">{item.result}</p>
                    </div>
                ))}
            </div>
        </Section>
    );
}

/* -------------------------------------------------------------------------- */

function PricingTeaser({ pricing, cheapest, trialDays, signupEnabled }) {
    return (
        <Section
            eyebrow="Pricing"
            title="Pick a plan, or start free and decide later"
            lead={
                trialDays > 0
                    ? `Every plan starts with a ${trialDays}-day trial. Your data stays put when it ends.`
                    : "Plans differ by how many messages, contacts, numbers and team members they allow."
            }
        >
            <div className="rounded-card border border-ink-200 bg-surface p-6 sm:p-8 flex flex-wrap items-center justify-between gap-6">
                <div>
                    {cheapest ? (
                        <>
                            <p className="text-sm text-ink-500 m-0 mb-1">Plans start at</p>
                            <p className="m-0">
                                <span className="text-3xl font-bold text-ink-900">
                                    {formatMoney(cheapest.price_minor, cheapest.currency || pricing?.currency)}
                                </span>
                                <span className="text-sm text-ink-500">
                                    {" / "}
                                    {cheapest.interval === "YEARLY" ? "year" : "month"}
                                </span>
                            </p>
                            <p className="text-sm text-ink-500 mt-2 mb-0">
                                Every plan states its message, contact, number and team limits. There is
                                no unlimited tier, because there is no unlimited WhatsApp.
                            </p>
                        </>
                    ) : (
                        <>
                            <p className="text-base font-bold text-ink-900 m-0 mb-1">
                                Plans are arranged with our team
                            </p>
                            <p className="text-sm text-ink-500 m-0">
                                Tell us the size of your list and we will put together the right plan.
                            </p>
                        </>
                    )}
                </div>
                <div className="flex flex-wrap gap-3">
                    <Link to="/pricing" className="no-underline">
                        <Button size="lg" variant={signupEnabled ? "secondary" : "primary"}>
                            Compare plans
                        </Button>
                    </Link>
                    {signupEnabled && (
                        <Link to="/signup" className="no-underline">
                            <Button size="lg">{trialDays > 0 ? "Start free trial" : "Get started"}</Button>
                        </Link>
                    )}
                </div>
            </div>
        </Section>
    );
}

/* -------------------------------------------------------------------------- */

function Trust() {
    return (
        <Section
            eyebrow="Security and trust"
            title="What we do with your customers' data"
            lead="Stated plainly, and limited to controls that are actually implemented."
        >
            <div className="grid gap-5 sm:grid-cols-2 lg:grid-cols-4">
                <TrustPoint title="Separated by workspace" body="Every query is scoped to your workspace. One customer cannot read another's contacts, messages or billing." />
                <TrustPoint title="Encrypted in transit" body="HTTPS everywhere, and provider credentials are encrypted before they are stored." />
                <TrustPoint title="Backed up and restored" body="Backups run on a schedule, leave the host, and are restored on a schedule to prove they work." />
                <TrustPoint title="Audited" body="Sensitive actions are written to an audit log — who did what, when, and from where." />
            </div>
            <p className="text-sm text-ink-500 mt-6 mb-0">
                <Link to="/trust" className="text-brand-700 font-medium">
                    Read the full trust page
                </Link>{" "}
                for what is implemented, what is not, and what we do not claim.
            </p>
        </Section>
    );
}

function TrustPoint({ title, body }) {
    return (
        <div className="flex gap-3">
            <Check size={18} className="text-brand-600 shrink-0 mt-0.5" aria-hidden="true" />
            <div>
                <h3 className="text-sm font-bold text-ink-900 m-0 mb-1">{title}</h3>
                <p className="text-xs text-ink-500 m-0 leading-relaxed">{body}</p>
            </div>
        </div>
    );
}

/* -------------------------------------------------------------------------- */

function Faq({ product, trialDays }) {
    const items = [
        {
            q: "Do I need the official WhatsApp Business API?",
            a: "No. You can connect the number you already use by scanning a QR code, or connect an official WhatsApp Business API number if you have one. Both work; the official API supports more message types and higher volumes.",
        },
        {
            q: "Will my messages definitely be delivered?",
            a: "No product can promise that, and we do not. We hand each message to WhatsApp and report back exactly what WhatsApp tells us — sent, delivered, read or failed, with the reason. What we can promise is that we never mark a message as sent when it was not.",
        },
        {
            q: "Can I message people who have not opted in?",
            a: `Not through ${product}. Consent is checked in the send path itself, and an opt-out always wins. This is a product for messaging people who agreed to hear from you.`,
        },
        {
            q: "What happens if my number gets restricted by WhatsApp?",
            a: "That is between you and Meta, and nothing here can prevent it — we do not build features to evade provider enforcement. Sending is paced and consent is enforced, which is what genuinely reduces the risk: complaints are what get numbers restricted.",
        },
        {
            q: trialDays > 0 ? "What happens when the trial ends?" : "What happens if I cancel?",
            a: trialDays > 0
                ? "Sending stops and everything else stays exactly where it is. Your contacts, campaigns, templates and history are all still there when you choose a plan."
                : "You keep full access until the end of the period you have already paid for. Nothing is deleted when a subscription ends.",
        },
        {
            q: "Can I export my data?",
            a: "Yes. You can export your workspace's data from your settings, and you can request deletion of your account. Financial and consent records are kept where we are required to keep them, and the trust page says exactly which.",
        },
    ];

    return (
        <Section tone="tinted" eyebrow="Questions" title="Answers before you sign up">
            <div className="grid gap-4 lg:grid-cols-2">
                {items.map((item) => (
                    <div key={item.q} className="rounded-card border border-ink-200 bg-surface p-5">
                        <h3 className="text-sm font-bold text-ink-900 m-0 mb-2">{item.q}</h3>
                        <p className="text-sm text-ink-500 m-0 leading-relaxed">{item.a}</p>
                    </div>
                ))}
            </div>
        </Section>
    );
}

/* -------------------------------------------------------------------------- */

function FinalCta({ cta, signupEnabled, product }) {
    return (
        <section className="bg-ink-900">
            <div className="max-w-3xl mx-auto px-4 sm:px-6 py-16 sm:py-20 text-center">
                <h2 className="text-3xl font-bold text-white m-0 mb-4">
                    Send your first campaign this afternoon
                </h2>
                <p className="text-base text-ink-300 m-0 mb-8">
                    Connect a number, import your contacts, and see who is eligible before anything
                    goes out.
                </p>
                {signupEnabled ? (
                    <Link to="/signup" className="no-underline">
                        <Button size="lg" className="gap-2">
                            {cta}
                            <ArrowRight size={18} aria-hidden="true" />
                        </Button>
                    </Link>
                ) : (
                    <Link to="/login" className="no-underline">
                        <Button size="lg">Sign in to {product}</Button>
                    </Link>
                )}
            </div>
        </section>
    );
}

/* -------------------------------------------------------------------------- */

function Section({ id, eyebrow, title, lead, tone = "plain", children }) {
    const background =
        tone === "tinted" ? "bg-ink-50 border-y border-ink-200" : tone === "dark" ? "bg-ink-900" : "bg-surface";
    const titleColor = tone === "dark" ? "text-white" : "text-ink-900";
    const leadColor = tone === "dark" ? "text-ink-300" : "text-ink-500";
    const eyebrowColor = tone === "dark" ? "text-brand-400" : "text-brand-700";

    return (
        <section id={id} className={background}>
            <div className="max-w-6xl mx-auto px-4 sm:px-6 py-14 sm:py-20">
                <div className="max-w-2xl mb-10">
                    {eyebrow && (
                        <p className={`text-xs font-bold uppercase tracking-wide m-0 mb-3 ${eyebrowColor}`}>
                            {eyebrow}
                        </p>
                    )}
                    <h2 className={`text-2xl sm:text-3xl font-bold m-0 ${titleColor}`}>{title}</h2>
                    {lead && <p className={`text-base mt-3 mb-0 leading-relaxed ${leadColor}`}>{lead}</p>}
                </div>
                {children}
            </div>
        </section>
    );
}

export default Landing;
