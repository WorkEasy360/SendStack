import { useEffect, useState } from "react";
import { ArrowRight, Tag, TriangleAlert } from "lucide-react";
import api, { apiErrorMessage } from "../../services/api";
import { formatLimit, formatMoney } from "../../utils/plans";
import Alert from "../ui/Alert";
import Button from "../ui/Button";
import Modal from "../ui/Modal";
import Spinner from "../ui/Spinner";
import { Field, Input } from "../ui/Field";

/**
 * What changing plan will do, shown before the customer commits to it.
 *
 * Before this, pressing "Choose plan" went straight to a payment window. That
 * is defensible for an upgrade and indefensible for a downgrade: the customer
 * was agreeing to lower limits without being told which ones, and with no
 * answer to the question everybody actually asks — *what happens to the
 * contacts I already have that the smaller plan does not allow?*
 *
 * Every number and every warning sentence here comes from
 * `GET /billing/plan-change/`. Nothing is computed in the browser, including
 * the discounted price: the coupon box sends a **string** to
 * `/billing/coupon/preview/` and renders what comes back, and checkout
 * re-resolves the same code from scratch. A frontend that worked out its own
 * discount would be a frontend that could be asked for a different one.
 *
 * Two outcomes, because they are two different acts. An upgrade is a payment
 * and hands off to the provider's checkout. A downgrade costs nothing and is
 * applied directly — routing it through a payment provider would mean creating
 * a zero-value order that the provider would refuse.
 */
function PlanChangeDialog({ plan, currency, paymentsEnabled, onClose, onUpgrade, onDowngraded }) {
    const [preview, setPreview] = useState(null);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState("");
    const [busy, setBusy] = useState(false);

    const [code, setCode] = useState("");
    const [quote, setQuote] = useState(null);
    const [couponError, setCouponError] = useState("");
    const [checkingCode, setCheckingCode] = useState(false);

    useEffect(() => {
        if (!plan) return undefined;
        let cancelled = false;
        setLoading(true);
        setError("");
        setQuote(null);
        setCode("");
        setCouponError("");

        api.get("/billing/plan-change/", { params: { plan: plan.id } })
            .then((response) => {
                if (!cancelled) setPreview(response.data);
            })
            .catch((err) => {
                if (!cancelled) setError(apiErrorMessage(err, "We could not work out what this change would do."));
            })
            .finally(() => {
                if (!cancelled) setLoading(false);
            });

        return () => {
            cancelled = true;
        };
    }, [plan]);

    if (!plan) return null;

    const direction = preview?.direction;
    const isUpgrade = direction === "UPGRADE";
    const isDowngrade = direction === "DOWNGRADE";

    const applyCoupon = async () => {
        setCheckingCode(true);
        setCouponError("");
        try {
            const response = await api.post("/billing/coupon/preview/", { plan: plan.id, code });
            setQuote(response.data);
        } catch (err) {
            setQuote(null);
            setCouponError(apiErrorMessage(err, "That code is not valid for this purchase."));
        } finally {
            setCheckingCode(false);
        }
    };

    const confirm = async () => {
        setBusy(true);
        setError("");
        try {
            if (isDowngrade || direction === "LATERAL") {
                const response = await api.post("/billing/plan-change/", { plan: plan.id });
                onDowngraded(response.data.billing);
            } else {
                // The code travels with the checkout, where it is resolved and
                // checked again. This dialog's quote is a display.
                await onUpgrade(plan, quote?.code || "");
            }
        } catch (err) {
            setError(apiErrorMessage(err, "That change could not be made."));
        } finally {
            setBusy(false);
        }
    };

    return (
        <Modal
            onClose={onClose}
            title={isDowngrade ? `Move to ${plan.name}` : `Upgrade to ${plan.name}`}
        >
            {loading && <Spinner />}
            <Alert>{error}</Alert>

            {!loading && preview && (
                <div className="space-y-5">
                    <div className="flex items-center gap-3 text-sm">
                        <span className="text-ink-500">{preview.current?.name || "No plan"}</span>
                        <ArrowRight size={16} className="text-ink-300" aria-hidden="true" />
                        <span className="font-bold text-ink-900">{preview.new?.name}</span>
                    </div>

                    <dl className="m-0 space-y-2 text-sm">
                        <Row
                            label="New price"
                            value={`${formatMoney(preview.new?.price_minor, preview.currency || currency)} / ${
                                preview.new?.interval === "YEARLY" ? "year" : "month"
                            }`}
                        />
                        {preview.current && (
                            <Row
                                label="Difference"
                                value={`${preview.price_difference_minor >= 0 ? "+" : "−"}${formatMoney(
                                    Math.abs(preview.price_difference_minor),
                                    preview.currency || currency,
                                )}`}
                                tone={preview.price_difference_minor > 0 ? undefined : "good"}
                            />
                        )}
                        <Row label="Takes effect" value={preview.effective?.detail} small />
                    </dl>

                    <div>
                        <h3 className="text-xs font-bold uppercase tracking-wide text-ink-400 m-0 mb-2">
                            Limits
                        </h3>
                        <dl className="m-0 space-y-1.5 text-sm">
                            {(preview.limits || []).map((limit) => (
                                <div key={limit.metric} className="flex items-baseline justify-between gap-4">
                                    <dt className="text-ink-500 m-0">{limit.label}</dt>
                                    <dd className="m-0 text-ink-900">
                                        <span className={limit.reduced ? "text-ink-400 line-through mr-1.5" : "hidden"}>
                                            {formatLimit(limit.current_limit)}
                                        </span>
                                        <span className={limit.reduced ? "text-amber-700 font-bold" : "font-medium"}>
                                            {formatLimit(limit.new_limit)}
                                        </span>
                                    </dd>
                                </div>
                            ))}
                        </dl>
                    </div>

                    {(preview.warnings || []).length > 0 && (
                        // Rendered exactly as the server wrote them. Mapping
                        // codes to copy here would be a second place for the
                        // wording to drift from the rule it describes.
                        <Alert tone="warning" title="Before you confirm">
                            <ul className="list-disc pl-5 m-0 space-y-1">
                                {preview.warnings.map((warning) => (
                                    <li key={warning} className="text-sm">{warning}</li>
                                ))}
                            </ul>
                        </Alert>
                    )}

                    {preview.usage_impact?.affected && (
                        <div className="rounded-lg border border-ink-200 bg-ink-50 p-3.5 flex gap-2.5">
                            <TriangleAlert size={16} className="text-amber-600 shrink-0 mt-0.5" aria-hidden="true" />
                            <p className="text-xs text-ink-700 m-0 leading-relaxed">
                                {preview.usage_impact.detail}
                            </p>
                        </div>
                    )}

                    {isUpgrade && paymentsEnabled && (
                        <div>
                            <Field label="Have a code?">
                                <div className="flex gap-2">
                                    <Input
                                        value={code}
                                        placeholder="WELCOME20"
                                        onChange={(e) => setCode(e.target.value.toUpperCase())}
                                    />
                                    <Button
                                        type="button"
                                        variant="secondary"
                                        onClick={applyCoupon}
                                        disabled={!code.trim() || checkingCode}
                                        loading={checkingCode}
                                        icon={Tag}
                                    >
                                        Apply
                                    </Button>
                                </div>
                            </Field>
                            {couponError && <p className="text-xs text-red-600 mt-1 mb-0">{couponError}</p>}
                            {quote && (
                                <div className="mt-3 rounded-lg bg-brand-50 border border-brand-200 px-3.5 py-3">
                                    <p className="text-sm text-brand-900 m-0 font-medium">
                                        {quote.code} applied
                                        {quote.description ? ` — ${quote.description}` : ""}
                                    </p>
                                    <p className="text-sm text-brand-900 m-0 mt-1">
                                        <span className="line-through text-brand-700/60 mr-2">
                                            {formatMoney(quote.list_amount_minor, quote.currency || currency)}
                                        </span>
                                        <strong>{formatMoney(quote.amount_minor, quote.currency || currency)}</strong>
                                    </p>
                                </div>
                            )}
                        </div>
                    )}

                    <div className="flex gap-2 justify-end pt-1">
                        <Button variant="ghost" onClick={onClose} disabled={busy}>
                            Cancel
                        </Button>
                        <Button onClick={confirm} loading={busy} disabled={direction === "SAME"}>
                            {isDowngrade ? `Move to ${plan.name}` : "Continue to payment"}
                        </Button>
                    </div>
                </div>
            )}
        </Modal>
    );
}

function Row({ label, value, tone, small }) {
    return (
        <div className="flex items-baseline justify-between gap-4">
            <dt className="text-ink-500 m-0 shrink-0">{label}</dt>
            <dd
                className={`m-0 text-right ${small ? "text-xs text-ink-500" : "font-medium"} ${
                    tone === "good" ? "text-brand-700" : "text-ink-900"
                }`}
            >
                {value}
            </dd>
        </div>
    );
}

export default PlanChangeDialog;
