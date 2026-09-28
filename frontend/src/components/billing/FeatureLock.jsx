import { Link } from "react-router-dom";
import { Lock } from "lucide-react";
import Card from "../ui/Card";
import { useEntitlements } from "../../context/EntitlementsContext";

/**
 * What a feature the plan does not include looks like.
 *
 * Shown rather than hidden: somebody who cannot see that AI answering exists
 * will never ask for it, and a blank page where a feature used to be reads as
 * a bug rather than as a plan. The plan's own name is read from the API --
 * never hardcoded, because "Pro" and "Enterprise" are a reseller's words and
 * every reseller here picks their own.
 *
 * Presentation only. The backend refuses the same work with a 403 whether or
 * not this rendered -- see docs/ENTITLEMENTS.md.
 */

export function FeatureLocked({ title, description, className = "" }) {
    const { planName } = useEntitlements();

    return (
        <Card className={`text-center ${className}`}>
            <div className="mx-auto w-11 h-11 rounded-full bg-ink-100 flex items-center justify-center mb-3">
                <Lock size={19} className="text-ink-400" aria-hidden="true" />
            </div>
            <h2 className="text-base font-bold text-ink-900 m-0">{title}</h2>
            <p className="text-sm text-ink-500 mt-1.5 mb-0 max-w-md mx-auto">
                {description}
            </p>
            <p className="text-sm text-ink-500 mt-3 mb-0">
                {planName
                    ? `Not included in your ${planName} plan.`
                    : "Not included in your current plan."}{" "}
                <Link to="/billing" className="font-medium text-brand-700 hover:text-brand-800">
                    See plans
                </Link>
            </p>
        </Card>
    );
}

/**
 * Renders `children` when the plan includes `feature`, and the locked card
 * otherwise.
 */
export default function FeatureGate({ feature, title, description, children, className = "" }) {
    const { hasFeature } = useEntitlements();

    if (hasFeature(feature)) return children;
    return <FeatureLocked title={title} description={description} className={className} />;
}

/** The inline version, for a tab or a panel inside a page that stays usable. */
export function FeatureNotice({ feature, label, className = "" }) {
    const { hasFeature, planName } = useEntitlements();

    if (hasFeature(feature)) return null;

    return (
        <div
            className={`flex items-start gap-2.5 border border-ink-200 bg-ink-50 rounded-lg px-3.5 py-3 text-sm text-ink-600 ${className}`}
        >
            <Lock size={16} className="shrink-0 mt-0.5 text-ink-400" aria-hidden="true" />
            <p className="m-0">
                <span className="font-semibold text-ink-800">{label}</span> is not included in your{" "}
                {planName || "current"} plan.{" "}
                <Link to="/billing" className="font-medium text-brand-700 hover:text-brand-800">
                    See plans
                </Link>
            </p>
        </div>
    );
}
