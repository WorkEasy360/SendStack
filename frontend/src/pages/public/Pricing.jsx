import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { Check, Minus } from "lucide-react";
import { useBranding } from "../../context/BrandingContext";
import api from "../../services/api";
import Button from "../../components/ui/Button";
import Spinner from "../../components/ui/Spinner";
import { LogoMark } from "../../components/layout/Logo";
import { formatMoney, formatLimit } from "../../utils/plans";

/**
 * The public price list.
 *
 * **Nothing on this page is hardcoded.** The plans, their prices, their
 * limits and which features each one includes all come from
 * `GET /billing/pricing/`, which reads the same `Plan` rows the entitlement
 * service enforces. A tier written into this component would eventually
 * promise a limit the send gate does not honour, and the customer would find
 * that out at the worst possible moment.
 *
 * That also means an operator changing a price in the admin changes this page
 * without a deploy, and a deployment with no public plans renders an honest
 * "talk to us" rather than an invented table.
 */
function Pricing() {
    const { branding } = useBranding();
    const [data, setData] = useState(null);
    const [loading, setLoading] = useState(true);
    const [failed, setFailed] = useState(false);

    useEffect(() => {
        document.title = `Pricing · ${branding.product_name}`;
    }, [branding.product_name]);

    useEffect(() => {
        let cancelled = false;
        api.get("/billing/pricing/")
            .then((response) => {
                if (!cancelled) setData(response.data);
            })
            .catch(() => {
                if (!cancelled) setFailed(true);
            })
            .finally(() => {
                if (!cancelled) setLoading(false);
            });
        return () => {
            cancelled = true;
        };
    }, []);

    const plans = data?.plans || [];

    // The union of every feature key any plan mentions, in the order the API
    // gave them. Built from the response rather than a constant here, so a new
    // plan feature appears in the comparison without a frontend change.
    const featureRows = useMemo(() => data?.features || [], [data]);

    if (loading) {
        return (
            <div className="min-h-screen flex items-center justify-center bg-ink-50">
                <Spinner />
            </div>
        );
    }

    return (
        <div className="min-h-screen bg-ink-50">
            <header className="border-b border-ink-200 bg-surface">
                <div className="max-w-6xl mx-auto px-4 sm:px-6 py-4 flex items-center justify-between gap-4">
                    <Link to="/" className="flex items-center gap-2.5 no-underline">
                        <LogoMark size={30} />
                        <span className="font-bold text-ink-900">{branding.product_name}</span>
                    </Link>
                    <nav className="flex items-center gap-2">
                        <Link to="/login">
                            <Button variant="ghost" size="sm">Sign in</Button>
                        </Link>
                        {data?.signup_enabled && (
                            <Link to="/signup">
                                <Button size="sm">Get started</Button>
                            </Link>
                        )}
                    </nav>
                </div>
            </header>

            <main className="max-w-6xl mx-auto px-4 sm:px-6 py-10 sm:py-16">
                <div className="text-center max-w-2xl mx-auto mb-10 sm:mb-14">
                    <h1 className="text-3xl sm:text-4xl font-bold text-ink-900 m-0 mb-3">
                        Simple pricing for WhatsApp at work
                    </h1>
                    <p className="text-base text-ink-500 m-0">
                        Campaigns, a shared inbox, automation and an AI agent in one workspace.
                        {data?.trials_enabled && data?.trial_days > 0 && (
                            <> Every plan starts with a {data.trial_days}-day free trial.</>
                        )}
                    </p>
                </div>

                {failed && (
                    <p className="text-center text-sm text-ink-500">
                        Our plans could not be loaded just now.{" "}
                        {branding.support_email && <>Email {branding.support_email} and we will help.</>}
                    </p>
                )}

                {!failed && plans.length === 0 && (
                    // No published plans is a real state -- a deployment that
                    // sells through conversations. Saying so beats an empty grid.
                    <div className="text-center bg-surface border border-ink-200 rounded-card p-10 max-w-lg mx-auto">
                        <h2 className="text-lg font-bold text-ink-900 m-0 mb-2">Plans are arranged with our team</h2>
                        <p className="text-sm text-ink-500 mb-5">
                            Tell us what you need and we will put together the right plan for you.
                        </p>
                        {branding.support_email && (
                            <a href={`mailto:${branding.support_email}`} className="no-underline">
                                <Button size="lg">Email {branding.support_email}</Button>
                            </a>
                        )}
                    </div>
                )}

                {plans.length > 0 && (
                    <>
                        <div className="grid gap-5 sm:grid-cols-2 lg:grid-cols-3 mb-12">
                            {plans.map((plan, index) => (
                                <PlanCard
                                    key={plan.id}
                                    plan={plan}
                                    currency={data.currency}
                                    signupEnabled={data.signup_enabled}
                                    trialDays={data.trials_enabled ? data.trial_days : 0}
                                    // The middle tier, where there is one. A
                                    // visual emphasis, not a different offer.
                                    featured={plans.length > 2 && index === 1}
                                />
                            ))}
                        </div>

                        <ComparisonTable plans={plans} featureRows={featureRows} currency={data.currency} />
                    </>
                )}

                <p className="text-center text-xs text-ink-400 mt-12">
                    Prices are in {data?.currency || "INR"}.
                    {!data?.payments_enabled && plans.length > 0 && (
                        <> Payment is arranged with our team rather than online.</>
                    )}
                </p>
            </main>

            <footer className="border-t border-ink-200 bg-surface">
                <div className="max-w-6xl mx-auto px-4 sm:px-6 py-6 text-center text-xs text-ink-400">
                    <Link to="/terms" className="text-ink-400 hover:text-ink-600">Terms</Link>
                    {" · "}
                    <Link to="/privacy" className="text-ink-400 hover:text-ink-600">Privacy</Link>
                    {" · "}
                    <Link to="/dpa" className="text-ink-400 hover:text-ink-600">Data processing</Link>
                    {branding.support_email && (
                        <>
                            {" · "}
                            <a href={`mailto:${branding.support_email}`} className="text-ink-400 hover:text-ink-600">
                                {branding.support_email}
                            </a>
                        </>
                    )}
                </div>
            </footer>
        </div>
    );
}

function PlanCard({ plan, currency, signupEnabled, trialDays, featured }) {
    const limits = plan.limits || {};
    return (
        <div
            className={`bg-surface rounded-card p-6 flex flex-col ${
                featured ? "border-2 border-brand-500 shadow-raised" : "border border-ink-200 shadow-card"
            }`}
        >
            {featured && (
                <span className="self-start text-xs font-bold uppercase tracking-wide text-brand-700 bg-brand-50 rounded-full px-2.5 py-1 mb-3">
                    Most popular
                </span>
            )}
            <h2 className="text-lg font-bold text-ink-900 m-0">{plan.name}</h2>
            {plan.description && <p className="text-sm text-ink-500 mt-1.5 mb-0">{plan.description}</p>}

            <p className="mt-5 mb-0">
                <span className="text-3xl font-bold text-ink-900">
                    {formatMoney(plan.price_minor, plan.currency || currency)}
                </span>
                <span className="text-sm text-ink-500">
                    {" / "}
                    {plan.interval === "YEARLY" ? "year" : "month"}
                </span>
            </p>

            <ul className="list-none p-0 mt-5 mb-6 space-y-2.5 grow">
                <LimitRow label="messages a month" value={limits.messages_per_month} />
                <LimitRow label="contacts" value={limits.contacts} />
                <LimitRow label="WhatsApp numbers" value={limits.devices} />
                <LimitRow label="team members" value={limits.users} />
                {(plan.features || [])
                    .filter((feature) => feature.included)
                    .map((feature) => (
                        <li key={feature.key} className="flex items-start gap-2 text-sm text-ink-700">
                            <Check size={16} className="text-brand-600 mt-0.5 shrink-0" aria-hidden="true" />
                            <span>{feature.label}</span>
                        </li>
                    ))}
            </ul>

            {signupEnabled ? (
                <Link to={`/signup?plan=${encodeURIComponent(plan.name)}`} className="no-underline">
                    <Button variant={featured ? "primary" : "secondary"} size="lg" className="w-full">
                        {trialDays > 0 ? `Start ${trialDays}-day trial` : "Get started"}
                    </Button>
                </Link>
            ) : (
                <Link to="/login" className="no-underline">
                    <Button variant="secondary" size="lg" className="w-full">Sign in</Button>
                </Link>
            )}
        </div>
    );
}

function LimitRow({ label, value }) {
    return (
        <li className="flex items-start gap-2 text-sm text-ink-700">
            <Check size={16} className="text-brand-600 mt-0.5 shrink-0" aria-hidden="true" />
            <span>
                <strong className="font-semibold text-ink-900">{formatLimit(value)}</strong> {label}
            </span>
        </li>
    );
}

/**
 * The side-by-side comparison.
 *
 * Wrapped in its own horizontal scroller: on a phone this is wider than the
 * viewport, and letting the page body scroll sideways instead would break
 * every other section.
 */
function ComparisonTable({ plans, featureRows, currency }) {
    return (
        <section aria-labelledby="compare-heading">
            <h2 id="compare-heading" className="text-xl font-bold text-ink-900 text-center mb-6">
                Compare plans
            </h2>
            <div className="overflow-x-auto bg-surface border border-ink-200 rounded-card">
                <table className="w-full min-w-[36rem] text-sm border-collapse">
                    <caption className="sr-only">Feature and limit comparison across plans</caption>
                    <thead>
                        <tr className="border-b border-ink-200">
                            <th scope="col" className="text-left font-semibold text-ink-600 p-4">Plan</th>
                            {plans.map((plan) => (
                                <th key={plan.id} scope="col" className="text-left font-bold text-ink-900 p-4">
                                    {plan.name}
                                    <span className="block text-xs font-normal text-ink-500 mt-0.5">
                                        {formatMoney(plan.price_minor, plan.currency || currency)}
                                        {plan.interval === "YEARLY" ? " / year" : " / month"}
                                    </span>
                                </th>
                            ))}
                        </tr>
                    </thead>
                    <tbody>
                        <ComparisonRow label="Messages a month" plans={plans} pick={(p) => formatLimit(p.limits?.messages_per_month)} />
                        <ComparisonRow label="Contacts" plans={plans} pick={(p) => formatLimit(p.limits?.contacts)} />
                        <ComparisonRow label="WhatsApp numbers" plans={plans} pick={(p) => formatLimit(p.limits?.devices)} />
                        <ComparisonRow label="Team members" plans={plans} pick={(p) => formatLimit(p.limits?.users)} />
                        {featureRows.map((feature) => (
                            <ComparisonRow
                                key={feature.key}
                                label={feature.label}
                                plans={plans}
                                pick={(plan) => {
                                    const match = (plan.features || []).find((row) => row.key === feature.key);
                                    return match?.included ? true : false;
                                }}
                            />
                        ))}
                    </tbody>
                </table>
            </div>
        </section>
    );
}

function ComparisonRow({ label, plans, pick }) {
    return (
        <tr className="border-b border-ink-100 last:border-b-0">
            <th scope="row" className="text-left font-medium text-ink-600 p-4">{label}</th>
            {plans.map((plan) => {
                const value = pick(plan);
                return (
                    <td key={plan.id} className="p-4 text-ink-900">
                        {value === true && (
                            <>
                                <Check size={17} className="text-brand-600" aria-hidden="true" />
                                <span className="sr-only">Included</span>
                            </>
                        )}
                        {value === false && (
                            <>
                                <Minus size={17} className="text-ink-300" aria-hidden="true" />
                                <span className="sr-only">Not included</span>
                            </>
                        )}
                        {typeof value === "string" && value}
                    </td>
                );
            })}
        </tr>
    );
}

export default Pricing;
