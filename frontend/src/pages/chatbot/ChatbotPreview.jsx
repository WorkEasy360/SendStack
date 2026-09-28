import { useCallback, useEffect, useRef, useState } from "react";
import { Bot, RotateCcw, Send, User, X } from "lucide-react";
import api, { apiErrorMessage } from "../../services/api";
import Alert from "../../components/ui/Alert";
import Button, { IconButton } from "../../components/ui/Button";
import Spinner from "../../components/ui/Spinner";
import { Input } from "../../components/ui/Field";
import { describeTrigger } from "../../utils/chatbotGraph";

/**
 * Talk to the draft, without a phone.
 *
 * **This is not a mock.** Every message posts the whole transcript to
 * `POST /chatbot/bots/:id/simulate/`, which replays it through the same engine
 * a real WhatsApp message goes through — the same rules, the same graph, the
 * same conditions, the same AI call — inside a transaction that is thrown away.
 * What comes back is what the bot *would* have said, and the execution path it
 * took to decide.
 *
 * The transcript lives here, in the browser, because the server deliberately
 * keeps nothing between requests: a preview must not leave a conversation, a
 * contact or a message behind in a real workspace. That is why every turn sends
 * the whole conversation rather than just the new line.
 */

export default function ChatbotPreview({ bot, saveState, trigger, onSave, onClose }) {
    const [turns, setTurns] = useState([]);
    const [transcript, setTranscript] = useState([]);
    const [steps, setSteps] = useState([]);
    const [variables, setVariables] = useState({});
    const [draft, setDraft] = useState("");
    const [busy, setBusy] = useState(false);
    const [error, setError] = useState("");
    const [showSteps, setShowSteps] = useState(false);
    const scroller = useRef(null);

    useEffect(() => {
        scroller.current?.scrollTo({ top: scroller.current.scrollHeight, behavior: "smooth" });
    }, [transcript, busy]);

    const run = useCallback(
        async (nextTurns) => {
            setBusy(true);
            setError("");
            try {
                const response = await api.post(`/chatbot/bots/${bot.id}/simulate/`, { messages: nextTurns });
                setTurns(nextTurns);
                setTranscript(response.data.transcript || []);
                setSteps(response.data.steps || []);
                setVariables(response.data.variables || {});
            } catch (requestError) {
                setError(apiErrorMessage(requestError, "Could not run the preview."));
            } finally {
                setBusy(false);
            }
        },
        [bot.id],
    );

    const send = useCallback(
        (turn) => {
            if (busy) return;
            run([...turns, turn]);
            setDraft("");
        },
        [busy, run, turns],
    );

    const reset = () => {
        setTurns([]);
        setTranscript([]);
        setSteps([]);
        setVariables({});
        setError("");
    };

    // The options offered by the last thing the bot said. Tapping one sends the
    // button *id* rather than the label, which is exactly what a real tap does
    // — so a preview exercises per-option branching for real instead of
    // accidentally testing text matching.
    const lastBotTurn = [...transcript].reverse().find((entry) => entry.role === "bot");
    const offered = lastBotTurn?.buttons || [];

    return (
        <div className="fixed inset-0 z-50 flex justify-end bg-black/40" role="dialog" aria-label="Test this bot">
            <button type="button" className="flex-1 cursor-default" onClick={onClose} aria-label="Close preview" />

            <div className="w-full sm:w-[420px] bg-surface flex flex-col shadow-2xl">
                <div className="flex items-center gap-2 px-4 py-3 border-b border-ink-200">
                    <Bot size={16} className="text-brand-600" />
                    <div className="min-w-0">
                        <p className="text-sm font-semibold text-ink-900 m-0 truncate">Test {bot.name}</p>
                        <p className="text-[11px] text-ink-500 m-0">Runs the draft. Nothing is sent or saved.</p>
                    </div>
                    <IconButton icon={RotateCcw} onClick={reset} className="ml-auto" title="Start again" aria-label="Start again" />
                    <IconButton icon={X} onClick={onClose} aria-label="Close" />
                </div>

                {saveState !== "saved" && (
                    <Alert tone="warning" className="m-3 mb-0 text-xs">
                        The preview runs the <em>saved</em> draft, and you have unsaved changes.{" "}
                        <button type="button" className="underline font-semibold cursor-pointer" onClick={onSave}>
                            Save first
                        </button>
                        .
                    </Alert>
                )}

                {error && (
                    <Alert tone="error" className="m-3 mb-0 text-xs">
                        {error}
                    </Alert>
                )}

                <div ref={scroller} className="flex-1 overflow-y-auto px-3 py-3 space-y-2 bg-ink-50">
                    {transcript.length === 0 && !busy && (
                        <div className="text-center py-8">
                            <p className="text-xs text-ink-400 m-0">
                                Send a message to see what this bot would reply.
                            </p>
                            {/*
                              * Named here because a flow with a keyword trigger looks
                              * broken from this panel otherwise: "hello" gets the
                              * fallback, and nothing on screen says why.
                              */}
                            {trigger && trigger.type !== "ANY_MESSAGE" && (
                                <p className="text-[11px] text-ink-500 mt-1.5 mb-0">
                                    The flow starts {describeTrigger(trigger)}.
                                </p>
                            )}
                        </div>
                    )}

                    {transcript.map((entry, index) => (
                        <Bubble key={index} entry={entry} />
                    ))}

                    {busy && (
                        <div className="flex justify-center py-2">
                            <Spinner size="sm" />
                        </div>
                    )}
                </div>

                {offered.length > 0 && (
                    <div className="flex flex-wrap gap-1.5 px-3 py-2 border-t border-ink-100 bg-surface">
                        {offered.map((button) => (
                            <button
                                key={button.id}
                                type="button"
                                disabled={busy}
                                onClick={() => send({ text: button.title, button_id: button.id })}
                                className="px-2.5 py-1 rounded-full border border-brand-300 bg-brand-50 text-brand-700 text-xs font-semibold cursor-pointer hover:bg-brand-100 disabled:opacity-50"
                            >
                                {button.title}
                            </button>
                        ))}
                    </div>
                )}

                <form
                    className="flex items-center gap-2 px-3 py-2.5 border-t border-ink-200"
                    onSubmit={(event) => {
                        event.preventDefault();
                        if (draft.trim()) send({ text: draft.trim() });
                    }}
                >
                    <Input
                        value={draft}
                        placeholder="Type as the customer…"
                        disabled={busy}
                        onChange={(event) => setDraft(event.target.value)}
                    />
                    <Button type="submit" size="sm" icon={Send} disabled={busy || !draft.trim()} aria-label="Send" />
                </form>

                <div className="border-t border-ink-200 max-h-56 overflow-y-auto">
                    <button
                        type="button"
                        onClick={() => setShowSteps((open) => !open)}
                        className="w-full flex items-center justify-between px-3 py-2 text-[11px] font-semibold text-ink-600 cursor-pointer hover:bg-ink-50"
                    >
                        <span>What the bot did ({steps.length} steps)</span>
                        <span className="text-ink-400">{showSteps ? "Hide" : "Show"}</span>
                    </button>

                    {showSteps && (
                        <div className="px-3 pb-3">
                            {Object.keys(variables).length > 0 && (
                                <div className="mb-2 flex flex-wrap gap-1">
                                    {Object.entries(variables).map(([name, value]) => (
                                        <span
                                            key={name}
                                            className="text-[10px] font-mono bg-emerald-50 border border-emerald-200 text-emerald-800 rounded px-1.5 py-0.5"
                                        >
                                            {name} = {String(value)}
                                        </span>
                                    ))}
                                </div>
                            )}

                            <ol className="list-none p-0 m-0 space-y-1">
                                {steps.map((step, index) => (
                                    <li key={index} className="flex gap-2 text-[11px]">
                                        <span
                                            className={`shrink-0 font-mono font-semibold ${
                                                step.outcome === "ERROR"
                                                    ? "text-red-600"
                                                    : step.outcome === "SKIPPED"
                                                      ? "text-ink-400"
                                                      : "text-emerald-600"
                                            }`}
                                        >
                                            {step.step}
                                        </span>
                                        <span className="text-ink-600 min-w-0 break-words">{step.detail}</span>
                                    </li>
                                ))}
                            </ol>
                        </div>
                    )}
                </div>
            </div>
        </div>
    );
}

function Bubble({ entry }) {
    if (entry.role === "system") {
        return (
            <p className="text-[11px] text-center text-ink-500 italic m-0 py-1">{entry.text}</p>
        );
    }

    const mine = entry.role === "customer";
    return (
        <div className={`flex gap-2 ${mine ? "justify-end" : "justify-start"}`}>
            {!mine && (
                <span className="shrink-0 w-6 h-6 rounded-full bg-brand-100 text-brand-600 flex items-center justify-center mt-0.5">
                    <Bot size={12} />
                </span>
            )}
            <div
                className={`max-w-[75%] rounded-2xl px-3 py-2 text-xs whitespace-pre-wrap break-words ${
                    mine ? "bg-brand-600 text-white rounded-br-sm" : "bg-surface border border-ink-200 text-ink-800 rounded-bl-sm"
                }`}
            >
                {entry.text || <span className="italic opacity-60">(no text)</span>}

                {(entry.media || []).length > 0 && (
                    <p className="text-[10px] opacity-70 mt-1 mb-0">📎 {entry.media.length} attachment(s)</p>
                )}

                {(entry.buttons || []).length > 0 && (
                    <div className="mt-1.5 pt-1.5 border-t border-ink-100 space-y-0.5">
                        {entry.buttons.map((button) => (
                            <p key={button.id} className="text-[11px] font-semibold text-brand-600 m-0">
                                {button.title}
                            </p>
                        ))}
                    </div>
                )}
            </div>
            {mine && (
                <span className="shrink-0 w-6 h-6 rounded-full bg-ink-200 text-ink-600 flex items-center justify-center mt-0.5">
                    <User size={12} />
                </span>
            )}
        </div>
    );
}
