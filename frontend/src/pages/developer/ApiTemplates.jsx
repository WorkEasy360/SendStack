import { useEffect, useMemo, useState } from "react";
import { FileText } from "lucide-react";
import api, { apiErrorMessage } from "../../services/api";
import Alert from "../../components/ui/Alert";
import Badge from "../../components/ui/Badge";
import Card, { CardHeader } from "../../components/ui/Card";
import EmptyState from "../../components/ui/EmptyState";
import SearchInput from "../../components/ui/SearchInput";
import Spinner from "../../components/ui/Spinner";
import TemplateApiPanel, { TemplateApiActions } from "../../components/ui/TemplateApiPanel";

/**
 * Developer -> Templates.
 *
 * The same generated API JSON the Templates page shows per template, listed
 * for the whole workspace so somebody wiring up an integration can find the
 * template they want without switching screens.
 *
 * One request fetches every description, so selecting a template is instant
 * and the "Copy JSON" button on a collapsed row works without first expanding
 * it -- which is the commonest thing anybody does here.
 */
export default function ApiTemplates({ onTry }) {
    const [rows, setRows] = useState(null);
    const [error, setError] = useState("");
    const [query, setQuery] = useState("");
    const [selected, setSelected] = useState(null);

    useEffect(() => {
        api.get("/developer/api/templates")
            .then((response) => {
                setRows(response.data.results);
                setSelected(response.data.results[0]?.template_id || null);
            })
            .catch((err) => setError(apiErrorMessage(err, "Unable to load your templates.")));
    }, []);

    const filtered = useMemo(() => {
        if (!rows) return [];
        const needle = query.trim().toLowerCase();
        if (!needle) return rows;
        return rows.filter((row) => row.template_name.toLowerCase().includes(needle));
    }, [rows, query]);

    if (error) return <Alert tone="error">{error}</Alert>;
    if (!rows) return <Spinner />;

    if (rows.length === 0) {
        return (
            <EmptyState
                icon={FileText}
                title="No templates yet"
                description="Create a template under Templates, and the exact JSON needed to send it through the API will appear here."
            />
        );
    }

    const current = filtered.find((row) => row.template_id === selected) || filtered[0];

    return (
        <div className="space-y-5">
            <Alert tone="info">
                Every example below is generated from the template itself — the variable names come from its
                own text, so what you copy is what the API accepts.
            </Alert>

            <div className="grid lg:grid-cols-[18rem_1fr] gap-5 items-start">
                <Card padded={false} className="overflow-hidden">
                    <div className="p-3 border-b border-ink-100">
                        <SearchInput
                            value={query}
                            onChange={(event) => setQuery(event.target.value)}
                            onClear={() => setQuery("")}
                            placeholder="Find a template…"
                        />
                    </div>
                    <div className="max-h-[32rem] overflow-y-auto">
                        {filtered.length === 0 ? (
                            <p className="text-sm text-ink-500 p-4 m-0">No template matches “{query}”.</p>
                        ) : (
                            filtered.map((row) => (
                                <button
                                    key={row.template_id}
                                    type="button"
                                    onClick={() => setSelected(row.template_id)}
                                    className={`w-full text-left px-4 py-2.5 border-0 border-b border-ink-100 cursor-pointer transition-colors ${
                                        current?.template_id === row.template_id
                                            ? "bg-brand-50"
                                            : "bg-transparent hover:bg-ink-50"
                                    }`}
                                >
                                    <div className="text-sm font-semibold text-ink-900 truncate">
                                        {row.template_name}
                                    </div>
                                    <div className="text-xs text-ink-500 font-mono truncate">
                                        {row.template_id}
                                        {row.variables.length > 0 &&
                                            ` · ${row.variables.length} variable${row.variables.length === 1 ? "" : "s"}`}
                                    </div>
                                </button>
                            ))
                        )}
                    </div>
                </Card>

                {current && (
                    <Card>
                        <CardHeader
                            title={current.template_name}
                            description="Send this template through the API with the body below."
                            className="mb-4"
                            action={<Badge tone="gray">{current.template_id}</Badge>}
                        />
                        <div className="mb-4">
                            <TemplateApiActions
                                described={current}
                                onOpen={onTry ? () => onTry(current) : undefined}
                            />
                        </div>
                        <TemplateApiPanel templateId={current.template_id} described={current} />
                    </Card>
                )}
            </div>
        </div>
    );
}
