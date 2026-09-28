import { useRef, useState } from "react";
import { ArrowDown, ArrowUp, FileText, Film, ImageIcon, Link2, Paperclip, Upload, X } from "lucide-react";
import api, { apiErrorMessage } from "../../services/api";
import Button from "./Button";
import Alert from "./Alert";
import Badge from "./Badge";
import { Input } from "./Field";

/**
 * Attachment picker for message content: upload several images, videos and
 * documents at once, or paste a link to something already hosted elsewhere.
 *
 * The value is the `media_items` list the API takes — `{url, type, filename,
 * caption}` in send order. Order matters: the first attachment is the one the
 * single-attachment columns (`media_url` / `message_type` / `filename`) mirror,
 * so it is what every older reader of a message sees. Hence the reordering
 * controls rather than an unordered pile.
 */

const IMAGE_EXTENSIONS = ["jpg", "jpeg", "png", "gif", "webp", "bmp", "heic", "heif"];
const VIDEO_EXTENSIONS = ["mp4", "mov", "3gp", "mkv", "avi", "webm", "m4v"];

const TYPE_META = {
    IMAGE: { icon: ImageIcon, tone: "blue", label: "Image" },
    VIDEO: { icon: Film, tone: "violet", label: "Video" },
    DOCUMENT: { icon: FileText, tone: "amber", label: "Document" },
};

/** Mirrors the backend's classify(): anything not a picture or clip is a document. */
function classify(name = "", mimeType = "") {
    const extension = name.split("?")[0].split(".").pop()?.toLowerCase() || "";
    if (IMAGE_EXTENSIONS.includes(extension) || mimeType.startsWith("image/")) return "IMAGE";
    if (VIDEO_EXTENSIONS.includes(extension) || mimeType.startsWith("video/")) return "VIDEO";
    return "DOCUMENT";
}

function basename(url = "") {
    try {
        return decodeURIComponent(new URL(url).pathname.split("/").pop() || url);
    } catch {
        return url.split("/").pop() || url;
    }
}

/**
 * Compact field for the per-attachment caption/filename.
 *
 * Deliberately not <Input>: appending `py-1.5 text-xs` to it would lose to its
 * own `py-2.5 text-sm` on Tailwind source order, so the row would silently
 * stay full height.
 */
function SmallInput({ className = "", ...props }) {
    return (
        <input
            className={`w-full rounded-md border border-ink-200 bg-surface px-2 py-1 text-xs text-ink-900 placeholder:text-ink-400 transition-colors hover:border-ink-300 focus:border-brand-500 focus:outline-none focus:ring-2 focus:ring-brand-500/15 ${className}`}
            {...props}
        />
    );
}


function formatBytes(bytes) {
    if (!bytes && bytes !== 0) return "";
    if (bytes < 1024) return `${bytes} B`;
    if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} KB`;
    return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

export default function MediaPicker({
    value = [],
    onChange,
    disabled = false,
    max = 30,
    label = "Attachments",
    hint = "Images, videos and documents. Each one is sent as its own WhatsApp message, in this order.",
}) {
    const items = value || [];
    const inputRef = useRef(null);
    const [uploading, setUploading] = useState(false);
    const [dragging, setDragging] = useState(false);
    const [error, setError] = useState("");
    const [showUrlField, setShowUrlField] = useState(false);
    const [urlDraft, setUrlDraft] = useState("");
    // Upload responses carry a size; items loaded back from a saved record do
    // not, so this is best-effort detail rather than part of the value.
    const [sizes, setSizes] = useState({});

    const remaining = max - items.length;

    const addItems = (newItems) => {
        onChange([...items, ...newItems].slice(0, max));
    };

    const upload = async (files) => {
        const chosen = Array.from(files || []);
        if (!chosen.length) return;
        setError("");

        if (chosen.length > remaining) {
            setError(`Only ${remaining} more attachment${remaining === 1 ? "" : "s"} can be added.`);
            return;
        }

        setUploading(true);
        try {
            // One request for the whole selection: the endpoint takes a
            // repeated `files` field precisely so this is not N round trips.
            const form = new FormData();
            chosen.forEach((file) => form.append("files", file));
            // No content type is set here on purpose: `api` strips it for a
            // FormData body so the browser can supply the multipart boundary.
            const { data } = await api.post("/media/assets/", form);

            setSizes((previous) => {
                const next = { ...previous };
                data.forEach((asset) => (next[asset.url] = asset.size_bytes));
                return next;
            });
            addItems(
                data.map((asset) => ({
                    url: asset.url,
                    type: asset.kind,
                    filename: asset.original_filename || "",
                    caption: "",
                }))
            );
        } catch (err) {
            setError(apiErrorMessage(err, "Unable to upload these files."));
        } finally {
            setUploading(false);
            if (inputRef.current) inputRef.current.value = "";
        }
    };

    const addUrl = () => {
        const url = urlDraft.trim();
        if (!url) return;
        if (!/^https?:\/\//i.test(url)) {
            setError("Links must start with http:// or https://");
            return;
        }
        const type = classify(url);
        addItems([{ url, type, filename: type === "DOCUMENT" ? basename(url) : "", caption: "" }]);
        setUrlDraft("");
        setShowUrlField(false);
        setError("");
    };

    const update = (index, patch) => {
        onChange(items.map((item, position) => (position === index ? { ...item, ...patch } : item)));
    };

    const remove = (index) => {
        onChange(items.filter((_, position) => position !== index));
        setError("");
    };

    const move = (index, offset) => {
        const target = index + offset;
        if (target < 0 || target >= items.length) return;
        const next = [...items];
        [next[index], next[target]] = [next[target], next[index]];
        onChange(next);
    };

    const onDrop = (event) => {
        event.preventDefault();
        setDragging(false);
        if (!disabled) upload(event.dataTransfer.files);
    };

    return (
        <div className="mb-4">
            <span className="block text-sm font-semibold text-ink-700 mb-1.5">{label}</span>

            <div
                onDragOver={(event) => {
                    event.preventDefault();
                    setDragging(true);
                }}
                onDragLeave={() => setDragging(false)}
                onDrop={onDrop}
                className={`rounded-lg border border-dashed px-3 py-3 transition-colors ${
                    dragging ? "border-brand-500 bg-brand-50" : "border-ink-300 bg-ink-50"
                }`}
            >
                <div className="flex flex-wrap items-center gap-2">
                    <input
                        ref={inputRef}
                        type="file"
                        multiple
                        className="hidden"
                        onChange={(event) => upload(event.target.files)}
                        disabled={disabled || remaining <= 0}
                    />
                    <Button
                        type="button"
                        size="sm"
                        variant="secondary"
                        icon={Upload}
                        loading={uploading}
                        disabled={disabled || remaining <= 0}
                        onClick={() => inputRef.current?.click()}
                    >
                        Choose files
                    </Button>
                    <Button
                        type="button"
                        size="sm"
                        variant="ghost"
                        icon={Link2}
                        disabled={disabled || remaining <= 0}
                        onClick={() => setShowUrlField((open) => !open)}
                    >
                        Add a link
                    </Button>
                    <span className="text-xs text-ink-500">
                        {items.length > 0
                            ? `${items.length} of ${max} attached — drop more here`
                            : "or drop files here"}
                    </span>
                </div>

                {showUrlField && (
                    <div className="flex gap-2 mt-2.5">
                        <Input
                            type="url"
                            value={urlDraft}
                            placeholder="https://..."
                            autoFocus
                            onChange={(event) => setUrlDraft(event.target.value)}
                            onKeyDown={(event) => {
                                if (event.key === "Enter") {
                                    // Inside a <form>: Enter must add the link,
                                    // not submit the whole template.
                                    event.preventDefault();
                                    addUrl();
                                }
                            }}
                        />
                        <Button type="button" size="sm" variant="secondary" onClick={addUrl} disabled={!urlDraft.trim()}>
                            Add
                        </Button>
                    </div>
                )}
            </div>

            {error && <Alert tone="error" className="mt-2 mb-0">{error}</Alert>}

            {items.length > 0 && (
                <ul className="list-none p-0 m-0 mt-2.5 space-y-2">
                    {items.map((item, index) => {
                        const meta = TYPE_META[item.type] || TYPE_META.DOCUMENT;
                        const Icon = meta.icon;
                        const size = sizes[item.url];
                        return (
                            <li
                                key={`${item.url}-${index}`}
                                className="flex items-start gap-2.5 rounded-lg border border-ink-200 bg-surface p-2.5"
                            >
                                <span className="shrink-0 w-11 h-11 rounded-lg bg-ink-100 text-ink-500 flex items-center justify-center overflow-hidden">
                                    {item.type === "IMAGE" ? (
                                        <img
                                            src={item.url}
                                            alt=""
                                            className="w-full h-full object-cover"
                                            onError={(event) => {
                                                event.currentTarget.style.display = "none";
                                            }}
                                        />
                                    ) : (
                                        <Icon size={18} aria-hidden="true" />
                                    )}
                                </span>

                                <div className="min-w-0 flex-1">
                                    <div className="flex items-center gap-2 flex-wrap">
                                        <a
                                            href={item.url}
                                            target="_blank"
                                            rel="noreferrer"
                                            className="text-sm font-semibold text-ink-900 truncate no-underline hover:text-brand-700"
                                        >
                                            {item.filename || basename(item.url)}
                                        </a>
                                        <Badge tone={meta.tone}>{meta.label}</Badge>
                                        {index === 0 && items.length > 1 && <Badge tone="gray">Sent first</Badge>}
                                        {size !== undefined && (
                                            <span className="text-xs text-ink-500 tabular-nums">{formatBytes(size)}</span>
                                        )}
                                    </div>

                                    {item.type === "DOCUMENT" && (
                                        <SmallInput
                                            value={item.filename}
                                            onChange={(event) => update(index, { filename: event.target.value })}
                                            placeholder="Filename shown to the recipient"
                                            className="mt-1.5"
                                            required
                                        />
                                    )}
                                    <SmallInput
                                        value={item.caption || ""}
                                        onChange={(event) => update(index, { caption: event.target.value })}
                                        placeholder="Caption (optional)"
                                        className="mt-1.5"
                                    />
                                </div>

                                <div className="flex shrink-0 items-center gap-0.5">
                                    <button
                                        type="button"
                                        onClick={() => move(index, -1)}
                                        disabled={index === 0}
                                        aria-label="Move earlier"
                                        className="w-7 h-7 inline-flex items-center justify-center rounded-md border-none bg-transparent text-ink-400 hover:text-ink-800 hover:bg-ink-100 cursor-pointer disabled:opacity-30 disabled:cursor-not-allowed"
                                    >
                                        <ArrowUp size={14} />
                                    </button>
                                    <button
                                        type="button"
                                        onClick={() => move(index, 1)}
                                        disabled={index === items.length - 1}
                                        aria-label="Move later"
                                        className="w-7 h-7 inline-flex items-center justify-center rounded-md border-none bg-transparent text-ink-400 hover:text-ink-800 hover:bg-ink-100 cursor-pointer disabled:opacity-30 disabled:cursor-not-allowed"
                                    >
                                        <ArrowDown size={14} />
                                    </button>
                                    <button
                                        type="button"
                                        onClick={() => remove(index)}
                                        aria-label={`Remove ${item.filename || basename(item.url)}`}
                                        className="w-7 h-7 inline-flex items-center justify-center rounded-md border-none bg-transparent text-ink-400 hover:text-red-600 hover:bg-red-50 cursor-pointer"
                                    >
                                        <X size={14} />
                                    </button>
                                </div>
                            </li>
                        );
                    })}
                </ul>
            )}

            {hint && (
                <span className="block text-xs text-ink-500 mt-1.5 inline-flex items-center gap-1">
                    <Paperclip size={11} aria-hidden="true" />
                    {hint}
                </span>
            )}
        </div>
    );
}
