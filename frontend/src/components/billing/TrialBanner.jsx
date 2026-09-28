import { Link } from "react-router-dom";
import { AlertTriangle, Clock } from "lucide-react";

/**
 * What a customer is told about their trial, and when.
 *
 * Three states and no more, because a banner that is always there is a banner
 * nobody reads:
 *
 * * **Running, with time to spare** -- nothing. Silence is the right message
 *   for somebody on day two of fourteen.
 * * **Ending soon** -- a quiet, factual line. No countdown timer, no "act
 *   now", no colour that reads as an emergency, because it is not one.
 * * **Ended** -- what actually changed (sending is paused), what did not
 *   (nothing was deleted), and how to fix it.
 *
 * `days_remaining` comes from the server, so the number here and the date the
 * send gate acts on are the same number.
 */

//: Below this, say something. Above it, stay quiet.
const NOTICE_FROM_DAYS = 5;

export default function TrialBanner({ trial, className = "mb-6" }) {
    if (!trial) return null;

    if (trial.access_state === "EXPIRED") {
        return (
            <div
                className={`flex items-start gap-3 rounded-card border border-amber-200 bg-amber-50 px-4 py-3.5 ${className}`}
                role="status"
            >
                <AlertTriangle size={18} className="mt-0.5 shrink-0 text-amber-600" aria-hidden="true" />
                <p className="text-sm text-amber-900 m-0">
                    <strong>Your trial has ended</strong>, so sending is paused. Your contacts, campaigns and
                    history are all still here — nothing has been deleted.{" "}
                    <Link to="/billing" className="font-semibold text-amber-900 underline">
                        Choose a plan
                    </Link>{" "}
                    to start sending again.
                </p>
            </div>
        );
    }

    if (!trial.active || trial.days_remaining === null || trial.days_remaining === undefined) {
        return null;
    }

    if (trial.days_remaining > NOTICE_FROM_DAYS) return null;

    return (
        <div
            className={`flex items-start gap-3 rounded-card border border-ink-200 bg-surface px-4 py-3.5 ${className}`}
            role="status"
        >
            <Clock size={18} className="mt-0.5 shrink-0 text-ink-400" aria-hidden="true" />
            <p className="text-sm text-ink-700 m-0">
                {trial.days_remaining === 0 ? (
                    <>Your free trial ends today.</>
                ) : (
                    <>
                        <strong>
                            {trial.days_remaining} day{trial.days_remaining === 1 ? "" : "s"} left
                        </strong>{" "}
                        in your free trial.
                    </>
                )}{" "}
                <Link to="/billing" className="font-medium text-brand-700 hover:text-brand-800">
                    See plans
                </Link>
            </p>
        </div>
    );
}
