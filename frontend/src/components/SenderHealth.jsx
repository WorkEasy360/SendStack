import { AlertTriangle, PauseCircle, PlayCircle, ShieldCheck, TrendingUp } from "lucide-react";
import Badge from "./ui/Badge";
import Button from "./ui/Button";

/**
 * How a number is doing, shown where the decisions about it get made.
 *
 * The score is the headline, but on its own it is just a number nobody can
 * act on -- so every part that produced it is shown next to it: how much of
 * today's allowance is gone, whether the number is still ramping up, and the
 * plain-language reasons the score is not 100.
 */

const GRADE = {
    healthy: { tone: "green", label: "Healthy" },
    watch: { tone: "amber", label: "Watch" },
    at_risk: { tone: "red", label: "At risk" },
};

function formatDuration(seconds) {
    if (!seconds || seconds < 60) return "under a minute";
    const minutes = Math.round(seconds / 60);
    if (minutes < 60) return `${minutes} min`;
    return `${Math.round(minutes / 60)} h`;
}

export function HealthBadge({ health }) {
    if (!health) return null;
    const grade = GRADE[health.grade] || GRADE.watch;
    return (
        <Badge tone={grade.tone}>
            {grade.label} · {health.score}
        </Badge>
    );
}

/** One window's usage as a labelled bar. A missing limit reads as "no limit". */
function UsageBar({ label, used, limit }) {
    const unlimited = limit === null || limit === undefined;
    const percent = unlimited ? 0 : Math.min(Math.round((used / Math.max(limit, 1)) * 100), 100);
    const tone = percent >= 100 ? "bg-red-500" : percent >= 80 ? "bg-amber-500" : "bg-brand-500";

    return (
        <div>
            <div className="flex items-center justify-between gap-2 text-xs mb-1">
                <span className="text-ink-500">{label}</span>
                <span className="font-semibold text-ink-800 tabular-nums">
                    {unlimited ? `${used} sent · no limit` : `${used} / ${limit}`}
                </span>
            </div>
            <div className="h-1.5 rounded-full bg-ink-100 overflow-hidden">
                <div
                    className={`h-full rounded-full transition-[width] ${unlimited ? "bg-ink-200" : tone}`}
                    style={{ width: `${unlimited ? 100 : percent}%` }}
                />
            </div>
        </div>
    );
}

/**
 * The health strip inside a device card.
 *
 * Pausing is offered here rather than buried in a settings screen because the
 * moment somebody wants it is the moment they are looking at a number that is
 * misbehaving.
 */
export function SenderHealthStrip({ health, busy, onPause, onResume, onOpenLimits }) {
    if (!health) return null;

    return (
        <div className="mt-4 pt-3 border-t border-ink-100">
            <div className="flex items-center justify-between gap-2 mb-3">
                <span className="inline-flex items-center gap-1.5 text-xs font-semibold text-ink-600">
                    <ShieldCheck size={14} aria-hidden="true" />
                    Sender health
                </span>
                <HealthBadge health={health} />
            </div>

            <div className="space-y-2.5">
                <UsageBar label="Today" used={health.daily_sent} limit={health.daily_limit} />
                <UsageBar label="This hour" used={health.hourly_sent} limit={health.hourly_limit} />
            </div>

            {health.warmup_active && (
                <p className="flex items-start gap-1.5 text-xs text-blue-700 bg-blue-50 rounded-md px-2 py-1.5 mt-3 mb-0">
                    <TrendingUp size={13} aria-hidden="true" className="mt-0.5 shrink-0" />
                    <span>
                        Warming up — day {health.warmup_day} of {health.warmup_days_total}. New numbers send less
                        while they build a normal pattern.
                    </span>
                </p>
            )}

            {health.is_paused && (
                <p className="flex items-start gap-1.5 text-xs text-amber-800 bg-amber-50 rounded-md px-2 py-1.5 mt-3 mb-0">
                    <PauseCircle size={13} aria-hidden="true" className="mt-0.5 shrink-0" />
                    <span>
                        {health.pause_reason || "Sending is paused."}
                        {health.resumes_in_seconds
                            ? ` Resumes on its own in ${formatDuration(health.resumes_in_seconds)}.`
                            : " This one needs you to resume it."}
                    </span>
                </p>
            )}

            {!health.is_paused && health.reasons?.length > 0 && (
                <ul className="list-none p-0 m-0 mt-3 space-y-1">
                    {health.reasons.map((reason) => (
                        <li key={reason} className="flex items-start gap-1.5 text-xs text-ink-500">
                            <AlertTriangle size={13} aria-hidden="true" className="mt-0.5 shrink-0 text-amber-500" />
                            {reason}
                        </li>
                    ))}
                </ul>
            )}

            <div className="flex flex-wrap gap-2 mt-3">
                {health.is_paused ? (
                    <Button size="sm" variant="secondary" icon={PlayCircle} disabled={busy} onClick={onResume}>
                        Resume sending
                    </Button>
                ) : (
                    <Button size="sm" variant="ghost" icon={PauseCircle} disabled={busy} onClick={onPause}>
                        Pause sending
                    </Button>
                )}
                <Button size="sm" variant="ghost" disabled={busy} onClick={onOpenLimits}>
                    Limits
                </Button>
            </div>
        </div>
    );
}
