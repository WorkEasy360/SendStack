import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { ArrowRight, CheckCircle2, ListChecks } from "lucide-react";
import api from "../../services/api";
import Card, { CardHeader } from "../ui/Card";
import { timeAgo } from "./CrmBits";

/**
 * "What should I do next?" — the first thing on the dashboard.
 *
 * Charts tell you how last month went. This tells you what is waiting, and
 * every tile is a link straight to the list that answers it, so noticing the
 * work and starting it are the same click.
 *
 * Deliberately quiet when there is nothing to do: an empty inbox of work is
 * good news and should read as good news, not as an empty state that looks
 * like something failed to load.
 */

const TONE = {
    urgent: "border-red-200 bg-red-50/60 hover:border-red-300",
    normal: "border-ink-200 bg-surface hover:border-brand-300",
    idle: "border-ink-200 bg-surface opacity-60",
};

function Tile({ item }) {
    const tone = item.count === 0 ? "idle" : item.urgent > 0 ? "urgent" : "normal";
    const body = (
        <>
            <p
                className={`text-2xl font-bold m-0 tabular-nums ${
                    tone === "urgent" ? "text-red-700" : item.count ? "text-ink-900" : "text-ink-400"
                }`}
            >
                {item.count}
            </p>
            <p className="text-sm font-semibold text-ink-700 mt-1 mb-0">{item.label}</p>
            {item.detail && <p className="text-xs text-ink-500 mt-0.5 mb-0">{item.detail}</p>}
        </>
    );

    if (!item.count) {
        return <div className={`rounded-card border p-4 ${TONE.idle}`}>{body}</div>;
    }
    return (
        <Link
            to={item.link}
            className={`block rounded-card border p-4 no-underline transition-colors ${TONE[tone]}`}
        >
            {body}
        </Link>
    );
}

export default function TodayPanel() {
    const [digest, setDigest] = useState(null);
    const [failed, setFailed] = useState(false);

    useEffect(() => {
        let cancelled = false;
        api.get("/crm/today/")
            .then((response) => {
                if (!cancelled) setDigest(response.data);
            })
            .catch(() => {
                // This panel is a convenience on a page that has other things
                // to show; it hides itself rather than erroring over them.
                if (!cancelled) setFailed(true);
            });
        return () => {
            cancelled = true;
        };
    }, []);

    if (failed || !digest) return null;

    const items = digest.items || [];
    const anything = items.some((item) => item.count > 0);

    return (
        <Card className="mb-6">
            <CardHeader
                title="What should I do today?"
                description={
                    anything ? "Everything waiting on you, most urgent first." : "Nothing is waiting on you."
                }
                action={
                    <Link
                        to="/tasks"
                        className="inline-flex items-center gap-1.5 text-sm font-semibold text-brand-700 no-underline hover:text-brand-800"
                    >
                        All tasks <ArrowRight size={14} aria-hidden="true" />
                    </Link>
                }
                className="mb-4"
            />

            {anything ? (
                <div className="grid grid-cols-2 lg:grid-cols-3 xl:grid-cols-6 gap-3">
                    {items.map((item) => (
                        <Tile key={item.key} item={item} />
                    ))}
                </div>
            ) : (
                <div className="flex items-center gap-3 rounded-card border border-ink-200 bg-ink-50/60 px-4 py-5">
                    <CheckCircle2 size={20} className="text-brand-600 shrink-0" aria-hidden="true" />
                    <p className="text-sm text-ink-600 m-0">
                        You are all caught up — no follow-ups due, no chats waiting, nothing going cold.
                    </p>
                </div>
            )}

            {digest.tasks?.length > 0 && (
                <div className="mt-5 pt-5 border-t border-ink-200">
                    <p className="flex items-center gap-2 text-xs font-bold uppercase tracking-wider text-ink-400 mt-0 mb-3">
                        <ListChecks size={14} aria-hidden="true" /> Next up
                    </p>
                    <ul className="list-none p-0 m-0 space-y-2">
                        {digest.tasks.slice(0, 5).map((task) => (
                            <li key={task.id} className="flex items-center justify-between gap-3 text-sm">
                                <span className="min-w-0 truncate text-ink-800">
                                    {task.title}
                                    {task.contact_name && (
                                        <span className="text-ink-400"> · {task.contact_name}</span>
                                    )}
                                </span>
                                <span
                                    className={`shrink-0 text-xs tabular-nums ${
                                        task.is_overdue ? "text-red-600 font-semibold" : "text-ink-500"
                                    }`}
                                >
                                    {timeAgo(task.due_at)}
                                </span>
                            </li>
                        ))}
                    </ul>
                </div>
            )}
        </Card>
    );
}
