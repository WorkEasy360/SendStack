import { Link } from "react-router-dom";
import {
    AlertTriangle,
    Archive,
    Check,
    Database,
    FileText,
    KeyRound,
    Lock,
    ScrollText,
    Server,
    UserMinus,
} from "lucide-react";
import { useBranding } from "../../context/BrandingContext";
import Button from "../../components/ui/Button";
import { LogoMark } from "../../components/layout/Logo";
import { useSeo } from "../../hooks/useSeo";

/**
 * What this deployment actually does with a customer's data.
 *
 * The editorial rule for this page is narrower than for the rest of the
 * marketing site: **every control listed here is one somebody can point at in
 * the code.** Not a roadmap item, not an intention, not a control the
 * framework offers that this deployment has not switched on. A trust page is
 * read by the one buyer who will check, and a single overstated line on it
 * costs more than the deal it was written to win.
 *
 * That is why the page has a "What we do not claim" section and why it says so
 * about the QR session store. A page that only lists strengths tells a careful
 * reader nothing, because they know the weaknesses exist and are now guessing
 * at which ones were left out. See SECURITY.md, which this page is the public
 * summary of -- when the two disagree, SECURITY.md is right and this is stale.
 */
function Trust() {
    const { branding } = useBranding();
    const product = branding.product_name;

    useSeo({
        title: `Security and trust · ${product}`,
        description:
            "The security controls implemented in this product, what data it processes, " +
            "and what we deliberately do not claim.",
        canonical: "/trust",
        index: true,
    });

    return (
        <div className="min-h-screen bg-ink-50">
            <header className="border-b border-ink-200 bg-surface">
                <div className="max-w-4xl mx-auto px-4 sm:px-6 py-4 flex items-center justify-between gap-4">
                    <Link to="/" className="flex items-center gap-2.5 no-underline">
                        <LogoMark size={30} />
                        <span className="font-bold text-ink-900">{product}</span>
                    </Link>
                    <nav className="flex items-center gap-2">
                        <Link to="/pricing">
                            <Button variant="ghost" size="sm">Pricing</Button>
                        </Link>
                        <Link to="/login">
                            <Button variant="secondary" size="sm">Sign in</Button>
                        </Link>
                    </nav>
                </div>
            </header>

            <main className="max-w-4xl mx-auto px-4 sm:px-6 py-10 sm:py-16">
                <h1 className="text-3xl sm:text-4xl font-bold text-ink-900 m-0 mb-3">
                    Security and trust
                </h1>
                <p className="text-base text-ink-600 m-0 mb-10 leading-relaxed max-w-2xl">
                    Your contacts trusted you with their phone number, and you are about to trust us
                    with it. This page says what {product} does with it — limited to controls that
                    are implemented, and honest about the ones that are not.
                </p>

                <Block
                    icon={Database}
                    title="Separation between workspaces"
                    lead="One customer cannot reach another's data, and it is not a matter of remembering to filter."
                >
                    <Point>
                        Every record — contact, message, campaign, template, invoice, audit entry —
                        belongs to exactly one workspace, and every query is scoped to the workspace
                        of the signed-in user.
                    </Point>
                    <Point>
                        No API endpoint accepts an object id and looks it up globally. A request for
                        a record in another workspace returns "not found", not "forbidden", because
                        the difference would confirm the record exists.
                    </Point>
                    <Point>
                        Isolation is covered by its own test suite, run on every change, that
                        attempts cross-workspace reads and writes and asserts they fail.
                    </Point>
                </Block>

                <Block
                    icon={KeyRound}
                    title="Accounts and access"
                    lead="What it takes to get in, and what stops somebody who should not."
                >
                    <Point>
                        Passwords are hashed with Argon2. They are never stored, logged, or
                        recoverable — a reset issues a new one rather than revealing the old.
                    </Point>
                    <Point>
                        Repeated failed sign-ins lock the individual account, not the IP, so one
                        attacker cannot lock out a whole office and a distributed attempt still hits
                        the limit.
                    </Point>
                    <Point>
                        Sessions use short-lived access tokens with refresh. Signing out, changing a
                        password or having a workspace suspended ends access.
                    </Point>
                    <Point>
                        Roles limit what a member of your team can do. Team members cannot reach
                        billing or another workspace's data at all.
                    </Point>
                </Block>

                <Block
                    icon={Lock}
                    title="Encryption"
                    lead="Where it is applied, stated specifically rather than as “bank-grade”."
                >
                    <Point>
                        <strong>In transit:</strong> HTTPS is enforced with HSTS, and session and
                        CSRF cookies are secure and HttpOnly.
                    </Point>
                    <Point>
                        <strong>Provider credentials:</strong> WhatsApp and AI provider keys are
                        encrypted with a deployment key before they are written to the database, and
                        are never returned by any API — the settings screen shows that a key is set,
                        never the key.
                    </Point>
                    <Point>
                        <strong>Backups:</strong> archives are encrypted with a passphrase before
                        they leave the host, and the production check fails a release that has
                        backups running unencrypted.
                    </Point>
                </Block>

                <Block
                    icon={Archive}
                    title="Backups and recovery"
                    lead="A backup nobody has restored is a file with a promising name."
                >
                    <Point>
                        Backups run on a schedule, are verified as readable, and are copied off the
                        host. A configured destination that fails to accept the upload fails the
                        whole backup run rather than falling back to the local disk.
                    </Point>
                    <Point>
                        Restores are tested on a schedule into a throwaway database, and separately
                        the off-host copy is fetched back, checked byte for byte and restored — so
                        the claim is about the copy that would actually be used in a disaster.
                    </Point>
                    <Point>
                        The deployment reports on itself: when a backup last succeeded, whether it
                        left the host, and when one was last restored.
                    </Point>
                </Block>

                <Block
                    icon={ScrollText}
                    title="Audit and monitoring"
                    lead="A record of what happened, kept where it can be read back."
                >
                    <Point>
                        Sensitive actions — sign-ins, permission changes, credit adjustments,
                        subscription changes, exports, deletions — are written to an append-only
                        audit log with the actor, the time and the source.
                    </Point>
                    <Point>
                        Every request carries a correlation id that follows the work through the
                        queue, so a support question about one campaign can be traced end to end.
                    </Point>
                    <Point>
                        Logs and error reports are scrubbed of credentials and tokens before they
                        are stored or sent anywhere.
                    </Point>
                </Block>

                <Block
                    icon={UserMinus}
                    title="Consent, export and deletion"
                    lead="Your customers' choices, and yours."
                >
                    <Point>
                        An opt-out always wins. The order is enforced in the send path itself:
                        opt-out beats consent, consent beats eligibility. No interface setting can
                        reverse it.
                    </Point>
                    <Point>
                        You can export your workspace's data, and request deletion of your account.
                        Deletion is a request with a confirmation and a cooling-off window, not an
                        irreversible click.
                    </Point>
                    <Point>
                        Financial records, consent evidence and audit entries are retained where we
                        are required to keep them, and are separated from the customer data that is
                        deleted. Retention windows are enforced by a scheduled job rather than left
                        to accumulate.
                    </Point>
                </Block>

                <Block
                    icon={Server}
                    title="What we process, and why"
                    lead="The data this product handles, in the plainest terms we can put it."
                >
                    <div className="overflow-x-auto -mx-1">
                        <table className="w-full text-sm border-collapse">
                            <thead>
                                <tr className="text-left">
                                    <th className="py-2 px-1 font-bold text-ink-700 border-b border-ink-200">Data</th>
                                    <th className="py-2 px-1 font-bold text-ink-700 border-b border-ink-200">Why</th>
                                </tr>
                            </thead>
                            <tbody className="text-ink-600">
                                <ProcessingRow
                                    what="Your contacts' names, phone numbers and custom fields"
                                    why="To address and personalise the messages you send them."
                                />
                                <ProcessingRow
                                    what="Consent status and opt-out records"
                                    why="To decide, before every send, who may be messaged."
                                />
                                <ProcessingRow
                                    what="Message content and delivery status"
                                    why="To send the message and report back what the provider said happened."
                                />
                                <ProcessingRow
                                    what="Your account details and team members"
                                    why="To sign you in, and to say who did what in the audit log."
                                />
                                <ProcessingRow
                                    what="Usage counts and billing records"
                                    why="To enforce your plan's limits and to charge the right amount."
                                />
                                <ProcessingRow
                                    what="Payment identifiers from the payment provider"
                                    why="To match a payment to your subscription. Card details are typed on the provider's page and never reach us."
                                />
                            </tbody>
                        </table>
                    </div>
                    <p className="text-sm text-ink-500 mt-4 mb-0">
                        Read the{" "}
                        <Link to="/privacy" className="text-brand-700 font-medium">privacy notice</Link>{" "}
                        and the{" "}
                        <Link to="/dpa" className="text-brand-700 font-medium">data processing terms</Link>{" "}
                        for the full detail, including sub-processors and where data is stored.
                    </p>
                </Block>

                {/*
                  The section that makes the rest of the page worth reading. A
                  buyer who checks will find these anyway; finding them here
                  first is the difference between candour and a discovery.
                */}
                <div className="rounded-card border border-amber-300 bg-amber-50 p-6 mb-8">
                    <div className="flex items-center gap-2.5 mb-4">
                        <AlertTriangle size={20} className="text-amber-700" aria-hidden="true" />
                        <h2 className="text-lg font-bold text-ink-900 m-0">What we do not claim</h2>
                    </div>
                    <ul className="list-none p-0 m-0 space-y-3">
                        <NotClaimed title="No compliance certification">
                            We are not SOC 2, ISO 27001 or PCI certified, and we do not describe
                            ourselves as “GDPR compliant” or “DPDP compliant”. We can describe the
                            controls we implement — this page is that description — and you should
                            take your own advice about your obligations.
                        </NotClaimed>
                        <NotClaimed title="No delivery guarantee">
                            We hand your message to WhatsApp and report exactly what WhatsApp tells
                            us. We cannot promise delivery, we cannot promise your number will never
                            be restricted, and we do not build features to evade provider
                            enforcement.
                        </NotClaimed>
                        <NotClaimed title="QR-linked session data is not encrypted at rest">
                            When you link a number by QR code, the WhatsApp session lives in a
                            private Docker volume, readable only by the service's own user and never
                            exposed on the network. It is not additionally encrypted on disk yet.
                            Somebody with host filesystem access could use it; there is no path to it
                            without that access. Numbers connected through the official WhatsApp
                            Business API are not affected.
                        </NotClaimed>
                        <NotClaimed title="No unlimited plan">
                            Every plan states its message, contact, number and team limits, and those
                            limits are enforced on the server. If a page ever shows you a limit the
                            product does not honour, that is a bug and we want to hear about it.
                        </NotClaimed>
                    </ul>
                </div>

                <div className="rounded-card border border-ink-200 bg-surface p-6">
                    <div className="flex items-center gap-2.5 mb-3">
                        <FileText size={20} className="text-ink-500" aria-hidden="true" />
                        <h2 className="text-lg font-bold text-ink-900 m-0">Reporting a security problem</h2>
                    </div>
                    <p className="text-sm text-ink-600 m-0 leading-relaxed">
                        If you believe you have found a vulnerability, tell us before you tell anyone
                        else and give us a reasonable chance to fix it. We will not pursue you for
                        research done in good faith that does not access other customers' data,
                        degrade the service, or involve social engineering.
                    </p>
                    {branding.support_email && (
                        <p className="text-sm text-ink-600 mt-3 mb-0">
                            <a href={`mailto:${branding.support_email}`} className="text-brand-700 font-medium">
                                {branding.support_email}
                            </a>
                        </p>
                    )}
                </div>
            </main>

            <footer className="border-t border-ink-200 bg-surface">
                <div className="max-w-4xl mx-auto px-4 sm:px-6 py-6 text-center text-xs text-ink-400">
                    <Link to="/" className="text-ink-400 hover:text-ink-600">Home</Link>
                    {" · "}
                    <Link to="/pricing" className="text-ink-400 hover:text-ink-600">Pricing</Link>
                    {" · "}
                    <Link to="/terms" className="text-ink-400 hover:text-ink-600">Terms</Link>
                    {" · "}
                    <Link to="/privacy" className="text-ink-400 hover:text-ink-600">Privacy</Link>
                    {" · "}
                    <Link to="/dpa" className="text-ink-400 hover:text-ink-600">Data processing</Link>
                </div>
            </footer>
        </div>
    );
}

function Block({ icon: Icon, title, lead, children }) {
    return (
        <section className="rounded-card border border-ink-200 bg-surface p-6 mb-6">
            <div className="flex items-center gap-2.5 mb-2">
                <span className="w-9 h-9 rounded-lg bg-brand-50 text-brand-700 flex items-center justify-center shrink-0">
                    <Icon size={18} aria-hidden="true" />
                </span>
                <h2 className="text-lg font-bold text-ink-900 m-0">{title}</h2>
            </div>
            {lead && <p className="text-sm text-ink-500 mt-0 mb-4 leading-relaxed">{lead}</p>}
            <ul className="list-none p-0 m-0 space-y-2.5">{children}</ul>
        </section>
    );
}

function Point({ children }) {
    return (
        <li className="flex gap-2.5 text-sm text-ink-600 leading-relaxed">
            <Check size={16} className="text-brand-600 shrink-0 mt-0.5" aria-hidden="true" />
            <span>{children}</span>
        </li>
    );
}

function NotClaimed({ title, children }) {
    return (
        <li>
            <p className="text-sm font-bold text-ink-900 m-0 mb-1">{title}</p>
            <p className="text-sm text-ink-700 m-0 leading-relaxed">{children}</p>
        </li>
    );
}

function ProcessingRow({ what, why }) {
    return (
        <tr>
            <td className="py-2 px-1 border-b border-ink-100 align-top">{what}</td>
            <td className="py-2 px-1 border-b border-ink-100 align-top">{why}</td>
        </tr>
    );
}

export default Trust;
