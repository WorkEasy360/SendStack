import { useCallback, useEffect, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { AlertTriangle, ArrowUpRight, Check, CreditCard, Loader2, Minus } from "lucide-react";
import api, { apiErrorMessage } from "../../services/api";
import { useEntitlements } from "../../context/EntitlementsContext";
import { useToast } from "../../components/ui/Toast";
import Alert from "../../components/ui/Alert";
import Badge from "../../components/ui/Badge";
import Button from "../../components/ui/Button";
import Card, { CardHeader } from "../../components/ui/Card";
import PlanChangeDialog from "../../components/billing/PlanChangeDialog";
import ConfirmDialog from "../../components/ui/ConfirmDialog";
import PageHeader from "../../components/ui/PageHeader";
import Spinner from "../../components/ui/Spinner";
import { formatLimit, formatMoney, subscriptionLabel, usagePercent, usageTone } from "../../utils/plans";
import { loadPaymentScript } from "../../utils/paymentScript";

/**
 * The customer's own billing page.
 *
 * Everything here is server state. In particular, **payment status is never
 * inferred from the URL**: a return from a payment provider carries
 * `?checkout=<id>`, and all that does is tell this page which checkout to go
 * and *ask the server about*. `?payment=success` in the address bar changes
 * nothing, which is the point -- see the `verifying` branch below.
 */
function Billing() {
    const toast = useToast();
    const { refresh: refreshEntitlements } = useEntitlements();
    const [searchParams, setSearchParams] = useSearchParams();

    const [data, setData] = useState(null);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState("");
    const [busyPlan, setBusyPlan] = useState(null);
    const [verifying, setVerifying] = useState(false);
    const [confirmCancel, setConfirmCancel] = useState(false);
    // The plan the customer has picked but not yet confirmed. Pressing
    // "Choose plan" used to go straight to a payment window; it now opens
    // the preview dialog, because a downgrade in particular is a decision
    // nobody should make without being shown which limits drop.
    const [pendingPlan, setPendingPlan] = useState(null);

    const returningCheckout = searchParams.get("checkout");

    const load = useCallback(async () => {
        try {
            const response = await api.get("/billing/overview/");
            setData(response.data);
            setError("");
        } catch (err) {
            setError(apiErrorMessage(err, "Your billing details could not be loaded."));
        } finally {
            setLoading(false);
        }
    }, []);

    useEffect(() => {
        load();
    }, [load]);

    /**
     * The payment return.
     *
     * The customer is back from the provider and the browser is claiming
     * nothing at all -- it just names a checkout. This asks the server what
     * became of it, and the server asks the provider if it does not already
     * know. Only then does the page say anything about whether it worked.
     */
    useEffect(() => {
        if (!returningCheckout) return;
        let cancelled = false;
        setVerifying(true);

        api.get(`/billing/checkout/${returningCheckout}/status/`)
            .then((response) => {
                if (cancelled) return;
                setData(response.data.billing);
                refreshEntitlements();
                const status = response.data.checkout?.status;
                if (status === "PAID") {
                    toast.success("Payment confirmed. Your plan is active.");
                } else {
                    toast.info("We have not received this payment yet. If it was taken, it will appear shortly.");
                }
            })
            .catch((err) => {
                if (!cancelled) {
                    toast.error(apiErrorMessage(err, "We could not confirm that payment. Nothing has changed."));
                }
            })
            .finally(() => {
                if (cancelled) return;
                setVerifying(false);
                // Clear the parameter so a refresh does not re-run this.
                const next = new URLSearchParams(searchParams);
                next.delete("checkout");
                next.delete("payment");
                setSearchParams(next, { replace: true });
            });

        return () => {
            cancelled = true;
        };
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [returningCheckout]);

    const startCheckout = async (plan, couponCode = "") => {
        setBusyPlan(plan.id);
        try {
            const response = await api.post("/billing/checkout/", {
                plan: plan.id,
                // A string to look up, never a discount. The server resolves
                // what it is worth and freezes the amount onto the checkout.
                ...(couponCode ? { code: couponCode } : {}),
            });
            const checkout = response.data;
            // Loaded now rather than on every page load -- see utils/paymentScript.
            await loadPaymentScript(checkout.public_params?.provider);
            openProviderCheckout(checkout, {
                onDone: async (payload) => {
                    try {
                        const verified = await api.post(
                            `/billing/checkout/${checkout.id}/verify/`, payload,
                        );
                        setData(verified.data.billing);
                        refreshEntitlements();
                        toast.success("Payment confirmed. Your plan is active.");
                    } catch (err) {
                        toast.error(apiErrorMessage(err, "We could not confirm that payment."));
                        load();
                    }
                },
                onDismiss: () => {
                    toast.info("Payment cancelled. Nothing has been charged.");
                },
                onUnavailable: () => {
                    toast.error("The payment window could not be opened. Please try again.");
                },
            });
        } catch (err) {
            toast.error(apiErrorMessage(err, "We could not start that payment."));
            throw err;
        } finally {
            setBusyPlan(null);
            setPendingPlan(null);
        }
    };

    const cancelSubscription = async () => {
        setConfirmCancel(false);
        try {
            const response = await api.post("/billing/cancel/", {});
            setData(response.data.billing);
            refreshEntitlements();
            toast.success(response.data.detail);
        } catch (err) {
            toast.error(apiErrorMessage(err, "Your subscription could not be cancelled."));
        }
    };

    if (loading) {
        return (
            <div className="flex items-center justify-center py-20">
                <Spinner />
            </div>
        );
    }

    const subscription = data?.subscription;
    const usage = data?.usage;

    return (
        <div>
            <PageHeader title="Plan & billing" description="What your workspace is on, and what it has used." />

            <Alert>{error}</Alert>

            {verifying && (
                <Card className="mb-5 flex items-center gap-3">
                    <Loader2 size={18} className="animate-spin text-brand-600" aria-hidden="true" />
                    <p className="text-sm text-ink-700 m-0">
                        Verifying your payment with the provider. This only takes a moment.
                    </p>
                </Card>
            )}

            <CurrentPlanCard
                subscription={subscription}
                currency={data?.currency}
                paymentsEnabled={data?.payments_enabled}
                trialDays={data?.trial_days}
                onCancel={() => setConfirmCancel(true)}
            />

            {usage && <UsageCard usage={usage} />}

            <AvailablePlans
                plans={data?.available_plans || []}
                currency={data?.currency}
                currentPlanId={subscription?.plan_id}
                paymentsEnabled={data?.payments_enabled}
                busyPlan={busyPlan}
                onChoose={setPendingPlan}
            />

            <PaymentHistory payments={data?.payments || []} invoices={data?.invoices} currency={data?.currency} />

            {pendingPlan && (
                <PlanChangeDialog
                    plan={pendingPlan}
                    currency={data?.currency}
                    paymentsEnabled={data?.payments_enabled}
                    onClose={() => setPendingPlan(null)}
                    onUpgrade={startCheckout}
                    onDowngraded={(billing) => {
                        setData(billing);
                        setPendingPlan(null);
                        refreshEntitlements();
                        toast.success("Your plan has been changed. Nothing has been deleted.");
                    }}
                />
            )}

            {confirmCancel && (
                <ConfirmDialog
                    title="Cancel your subscription?"
                    /*
                      The exact effect, with the date on it. A cancel button
                      whose timing is not stated is the dark pattern this phase
                      is explicitly not allowed to ship.
                    */
                    message={
                        subscription?.current_period_end
                            ? `You keep full access until ${new Date(subscription.current_period_end).toLocaleDateString()}. Nothing is deleted, and you can start a new plan at any time.`
                            : "Nothing is deleted, and you can start a new plan at any time."
                    }
                    confirmLabel="Cancel subscription"
                    cancelLabel="Keep my plan"
                    tone="danger"
                    onConfirm={cancelSubscription}
                    onClose={() => setConfirmCancel(false)}
                />
            )}
        </div>
    );
}

/**
 * Open the provider's hosted checkout.
 *
 * Deliberately the only provider-aware code in the frontend, and it is
 * defensive: if the provider's script has not loaded, it says so rather than
 * failing silently or -- far worse -- pretending the payment went through.
 */
function openProviderCheckout(checkout, { onDone, onDismiss, onUnavailable }) {
    const params = checkout.public_params || {};

    if (params.provider === "razorpay" && typeof window.Razorpay === "function") {
        const razorpay = new window.Razorpay({
            key: params.key_id,
            order_id: params.order_id,
            amount: params.amount,
            currency: params.currency,
            name: document.title,
            // The handler hands the provider's response straight to the
            // server. Nothing here inspects it or decides anything from it:
            // the signature check and the provider fetch happen server-side.
            handler: (response) => onDone(response),
            modal: { ondismiss: onDismiss },
        });
        razorpay.open();
        return;
    }

    onUnavailable();
}

function CurrentPlanCard({ subscription, currency, paymentsEnabled, trialDays, onCancel }) {
    if (!subscription) {
        return (
            <Card className="mb-5">
                <CardHeader
                    title="No plan yet"
                    description={
                        trialDays > 0
                            ? "Choose a plan below to start sending."
                            : "Your workspace is not on a plan. Choose one below, or talk to us."
                    }
                    className="mb-0"
                />
            </Card>
        );
    }

    const state = subscription.access_state;
    const expired = state === "EXPIRED";
    const grace = state === "GRACE";

    return (
        <Card className="mb-5">
            <CardHeader
                title={subscription.plan}
                description={
                    subscription.is_trial && subscription.trial_days_remaining !== null
                        ? `Trial — ${subscription.trial_days_remaining} day${subscription.trial_days_remaining === 1 ? "" : "s"} left`
                        : `${formatMoney(subscription.price_minor, subscription.currency || currency)} / ${subscription.interval === "YEARLY" ? "year" : "month"}`
                }
                action={
                    <Badge tone={expired ? "red" : grace ? "amber" : subscription.is_trial ? "blue" : "green"}>
                        {subscriptionLabel(subscription.status)}
                    </Badge>
                }
                className="mb-4"
            />

            <dl className="grid gap-4 sm:grid-cols-3 m-0">
                <Detail
                    label="Current period"
                    value={
                        subscription.current_period_end
                            ? `until ${new Date(subscription.current_period_end).toLocaleDateString()}`
                            : "—"
                    }
                />
                <Detail label="Billing interval" value={subscription.interval === "YEARLY" ? "Yearly" : "Monthly"} />
                <Detail
                    label="Price"
                    value={formatMoney(subscription.price_minor, subscription.currency || currency)}
                />
            </dl>

            {expired && (
                <p className="mt-4 mb-0 text-sm text-red-700 bg-red-50 border border-red-200 rounded-lg px-3.5 py-2.5">
                    <AlertTriangle size={15} className="inline-block mr-1.5 -mt-0.5" aria-hidden="true" />
                    Sending is paused because this plan has ended. <strong>Nothing has been deleted</strong> — your
                    contacts, campaigns and history are all still here. Choose a plan to start sending again.
                </p>
            )}

            {grace && (
                <p className="mt-4 mb-0 text-sm text-amber-800 bg-amber-50 border border-amber-200 rounded-lg px-3.5 py-2.5">
                    A payment on this account is outstanding. Your workspace keeps working for now.
                </p>
            )}

            {subscription.status === "CANCELLED" && subscription.current_period_end && !expired && (
                <p className="mt-4 mb-0 text-sm text-ink-600 bg-ink-50 border border-ink-200 rounded-lg px-3.5 py-2.5">
                    Cancelled. You keep full access until{" "}
                    <strong>{new Date(subscription.current_period_end).toLocaleDateString()}</strong>.
                </p>
            )}

            {paymentsEnabled &&
                !subscription.is_trial &&
                subscription.status !== "CANCELLED" &&
                !expired && (
                    <div className="mt-5 pt-4 border-t border-ink-100">
                        <Button variant="ghost" size="sm" onClick={onCancel}>
                            Cancel subscription
                        </Button>
                    </div>
                )}
        </Card>
    );
}

function Detail({ label, value }) {
    return (
        <div>
            <dt className="text-xs font-semibold uppercase tracking-wide text-ink-500 m-0">{label}</dt>
            <dd className="text-sm text-ink-900 mt-1 m-0">{value}</dd>
        </div>
    );
}

const METRIC_LABELS = {
    messages_per_month: "Messages this period",
    contacts: "Contacts",
    devices: "WhatsApp numbers",
    users: "Team members",
};

function UsageCard({ usage }) {
    const metrics = usage.usage || {};
    return (
        <Card className="mb-5">
            <CardHeader title="Usage" description="Counted from your data, not from a running tally." className="mb-4" />
            <div className="space-y-4">
                {Object.entries(METRIC_LABELS).map(([key, label]) => {
                    const block = metrics[key];
                    if (!block) return null;
                    return <UsageBar key={key} label={label} block={block} />;
                })}
            </div>
        </Card>
    );
}

function UsageBar({ label, block }) {
    const percent = block.percent ?? usagePercent(block.used, block.limit);
    const tone = usageTone(percent);
    const bar = {
        ok: "bg-brand-600",
        warning: "bg-amber-500",
        critical: "bg-orange-500",
        full: "bg-red-600",
    }[tone];

    return (
        <div>
            <div className="flex items-baseline justify-between gap-3 mb-1.5">
                <span className="text-sm font-medium text-ink-700">{label}</span>
                <span className="text-sm text-ink-500 tabular-nums">
                    {Number(block.used || 0).toLocaleString()} / {formatLimit(block.limit)}
                </span>
            </div>
            {percent !== null && (
                <div
                    className="h-2 rounded-full bg-ink-100 overflow-hidden"
                    role="progressbar"
                    aria-valuenow={percent}
                    aria-valuemin={0}
                    aria-valuemax={100}
                    aria-label={label}
                >
                    <div className={`h-full rounded-full ${bar}`} style={{ width: `${percent}%` }} />
                </div>
            )}
            {tone === "full" && (
                <p className="text-xs text-red-700 mt-1.5 mb-0">
                    This limit is full. Existing data is untouched; new additions are blocked until you upgrade.
                </p>
            )}
            {(tone === "warning" || tone === "critical") && (
                <p className="text-xs text-amber-700 mt-1.5 mb-0">
                    You have used {percent}% of this limit.
                </p>
            )}
        </div>
    );
}

function AvailablePlans({ plans, currency, currentPlanId, paymentsEnabled, busyPlan, onChoose }) {
    if (!plans.length) return null;

    return (
        <Card className="mb-5">
            <CardHeader
                title="Plans"
                description={
                    paymentsEnabled
                        ? "Change plan at any time. Your data is never deleted by a plan change."
                        : "Plans on this deployment are arranged with our team."
                }
                className="mb-4"
            />
            <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
                {plans.map((plan) => {
                    const current = plan.id === currentPlanId;
                    return (
                        <div
                            key={plan.id}
                            className={`rounded-xl border p-4 flex flex-col ${
                                current ? "border-brand-500 bg-brand-50/40" : "border-ink-200"
                            }`}
                        >
                            <div className="flex items-start justify-between gap-2">
                                <h3 className="text-sm font-bold text-ink-900 m-0">{plan.name}</h3>
                                {current && <Badge tone="green">Current</Badge>}
                            </div>
                            <p className="text-lg font-bold text-ink-900 mt-2 mb-3">
                                {formatMoney(plan.price_minor, plan.currency || currency)}
                                <span className="text-xs font-normal text-ink-500">
                                    {" / "}
                                    {plan.interval === "YEARLY" ? "year" : "month"}
                                </span>
                            </p>
                            <ul className="list-none p-0 m-0 mb-4 space-y-1.5 grow text-xs text-ink-600">
                                <li>{formatLimit(plan.limits?.messages_per_month)} messages a month</li>
                                <li>{formatLimit(plan.limits?.contacts)} contacts</li>
                                <li>{formatLimit(plan.limits?.devices)} WhatsApp numbers</li>
                                {(plan.features || []).map((feature) => (
                                    <li key={feature.key} className="flex items-center gap-1.5">
                                        {feature.included ? (
                                            <Check size={13} className="text-brand-600 shrink-0" aria-hidden="true" />
                                        ) : (
                                            <Minus size={13} className="text-ink-300 shrink-0" aria-hidden="true" />
                                        )}
                                        <span className={feature.included ? "" : "text-ink-400"}>{feature.label}</span>
                                    </li>
                                ))}
                            </ul>
                            {paymentsEnabled ? (
                                <Button
                                    variant={current ? "secondary" : "primary"}
                                    size="sm"
                                    className="w-full"
                                    disabled={current}
                                    loading={busyPlan === plan.id}
                                    onClick={() => onChoose(plan)}
                                    icon={current ? undefined : CreditCard}
                                >
                                    {current ? "Your plan" : "Choose plan"}
                                </Button>
                            ) : (
                                <p className="text-xs text-ink-500 m-0">Contact us to move to this plan.</p>
                            )}
                        </div>
                    );
                })}
            </div>
        </Card>
    );
}

function PaymentHistory({ payments, invoices, currency }) {
    return (
        <Card>
            <CardHeader title="Payments" className="mb-4" />
            {payments.length === 0 ? (
                <p className="text-sm text-ink-500 m-0">No payments yet.</p>
            ) : (
                <div className="overflow-x-auto">
                    <table className="w-full min-w-[32rem] text-sm border-collapse">
                        <thead>
                            <tr className="border-b border-ink-200 text-left">
                                <th scope="col" className="font-semibold text-ink-600 py-2 pr-4">Date</th>
                                <th scope="col" className="font-semibold text-ink-600 py-2 pr-4">Plan</th>
                                <th scope="col" className="font-semibold text-ink-600 py-2 pr-4">Amount</th>
                                <th scope="col" className="font-semibold text-ink-600 py-2 pr-4">Status</th>
                                <th scope="col" className="font-semibold text-ink-600 py-2">Reference</th>
                            </tr>
                        </thead>
                        <tbody>
                            {payments.map((payment) => (
                                <tr key={payment.id} className="border-b border-ink-100 last:border-b-0">
                                    <td className="py-2.5 pr-4 text-ink-700">
                                        {new Date(payment.captured_at || payment.created_at).toLocaleDateString()}
                                    </td>
                                    <td className="py-2.5 pr-4 text-ink-700">{payment.plan_name || "—"}</td>
                                    <td className="py-2.5 pr-4 text-ink-900 tabular-nums">
                                        {formatMoney(payment.amount_minor, payment.currency || currency)}
                                    </td>
                                    <td className="py-2.5 pr-4">
                                        <Badge tone={payment.status === "CAPTURED" ? "green" : "red"}>
                                            {payment.status === "CAPTURED" ? "Paid" : "Failed"}
                                        </Badge>
                                    </td>
                                    <td className="py-2.5 text-xs text-ink-500 font-mono">
                                        {payment.provider_payment_id}
                                    </td>
                                </tr>
                            ))}
                        </tbody>
                    </table>
                </div>
            )}

            {invoices && invoices.available === false && (
                // Said out loud rather than shown as an empty "Invoices" table,
                // which would read as a bug or, worse, as a claim.
                <p className="text-xs text-ink-500 mt-4 mb-0 flex items-start gap-1.5">
                    <ArrowUpRight size={13} className="mt-0.5 shrink-0" aria-hidden="true" />
                    {invoices.detail}
                </p>
            )}
        </Card>
    );
}

export default Billing;
