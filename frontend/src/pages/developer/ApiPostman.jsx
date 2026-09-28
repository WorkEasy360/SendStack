import { useEffect, useState } from "react";
import { Download, Link2, ShieldAlert } from "lucide-react";
import api, { apiErrorMessage } from "../../services/api";
import Alert from "../../components/ui/Alert";
import Button from "../../components/ui/Button";
import Card, { CardHeader } from "../../components/ui/Card";
import { CopyField } from "../../components/ui/CodeBlock";
import Spinner from "../../components/ui/Spinner";
import { useToast } from "../../components/ui/Toast";

/**
 * Developer -> Postman.
 *
 * Downloads a collection and an environment, both generated on the server from
 * the OpenAPI document. Nothing about the file is described in this component:
 * the variable table comes from the same list the exporter uses, so the page
 * documents the file it is offering rather than a remembered version of it.
 *
 * The download goes through the axios instance and is turned into a Blob here
 * rather than being a plain `<a href>`, because these endpoints are
 * JWT-authenticated -- a bare link would arrive without the Authorization
 * header and 401.
 */
export default function ApiPostman() {
    const toast = useToast();
    const [guide, setGuide] = useState(null);
    const [error, setError] = useState("");
    const [downloading, setDownloading] = useState("");

    useEffect(() => {
        api.get("/developer/api/postman/guide")
            .then((response) => setGuide(response.data))
            .catch((err) => setError(apiErrorMessage(err, "Unable to load the Postman guide.")));
    }, []);

    const download = async (kind, filename) => {
        setDownloading(kind);
        try {
            const response = await api.get(`/developer/api/postman?type=${kind}`);
            const blob = new Blob([JSON.stringify(response.data, null, 2)], {
                type: "application/json",
            });
            const url = URL.createObjectURL(blob);
            const link = document.createElement("a");
            link.href = url;
            link.download = filename;
            document.body.appendChild(link);
            link.click();
            document.body.removeChild(link);
            // Revoked once the click has been dispatched; leaving it alive
            // holds the whole file in memory for the life of the tab.
            URL.revokeObjectURL(url);
            toast.success(`${filename} downloaded.`);
        } catch (err) {
            toast.error(apiErrorMessage(err, "Could not build the file."));
        } finally {
            setDownloading("");
        }
    };

    if (error) return <Alert tone="error">{error}</Alert>;
    if (!guide) return <Spinner />;

    return (
        <div className="space-y-5">
            <Card>
                <CardHeader
                    title="Postman collection"
                    description="Every endpoint this deployment serves, as importable requests. Generated from the live API, so nothing in it 404s."
                    className="mb-4"
                />

                <div className="flex flex-wrap gap-2">
                    <Button
                        icon={Download}
                        loading={downloading === "collection"}
                        onClick={() =>
                            download("collection", "WhatsApp Marketing API.postman_collection.json")
                        }
                    >
                        Download collection
                    </Button>
                    <Button
                        variant="secondary"
                        icon={Download}
                        loading={downloading === "environment"}
                        onClick={() =>
                            download("environment", "WhatsApp Marketing API.postman_environment.json")
                        }
                    >
                        Download environment
                    </Button>
                    <Button
                        variant="ghost"
                        icon={Link2}
                        onClick={() => window.open(guide.collection_url, "_blank", "noopener")}
                    >
                        Import from URL
                    </Button>
                </div>

                <p className="text-xs text-ink-500 mt-3 mb-0">
                    In Postman you can also use <strong>Import → Link</strong> and paste the collection URL —
                    it stays up to date as endpoints change.
                </p>
                <CopyField value={guide.collection_url} className="mt-2" />
            </Card>

            <Card>
                <CardHeader
                    title="Environment variables"
                    description="The collection references these instead of literals, so switching between a token or a deployment is one edit rather than forty."
                    className="mb-4"
                />

                <div className="border border-ink-200 rounded-lg overflow-x-auto">
                    <table className="w-full text-sm min-w-[34rem]">
                        <thead className="bg-ink-50">
                            <tr>
                                <th className="text-left font-semibold text-ink-600 px-3 py-2">Variable</th>
                                <th className="text-left font-semibold text-ink-600 px-3 py-2">Value</th>
                                <th className="text-left font-semibold text-ink-600 px-3 py-2">What it is</th>
                            </tr>
                        </thead>
                        <tbody>
                            {guide.variables.map((row) => (
                                <tr key={row.key} className="border-t border-ink-100">
                                    <td className="px-3 py-2 font-mono text-ink-800 whitespace-nowrap">
                                        {row.key}
                                    </td>
                                    <td className="px-3 py-2 font-mono text-ink-600 break-all">
                                        {row.secret ? (
                                            <span className="text-amber-700">paste your token here</span>
                                        ) : (
                                            row.example || <span className="text-ink-400">optional</span>
                                        )}
                                    </td>
                                    <td className="px-3 py-2 text-ink-600">{row.description}</td>
                                </tr>
                            ))}
                        </tbody>
                    </table>
                </div>

                <Alert tone="warning" className="mt-4 mb-0">
                    <span className="inline-flex items-start gap-1.5">
                        <ShieldAlert size={14} className="shrink-0 mt-0.5" aria-hidden="true" />
                        <span>
                            The environment file ships with <code className="font-mono">api_token</code> empty
                            on purpose. We cannot fill it in — tokens are stored hashed and are not recoverable
                            — and a file with a live key in it is a file that ends up committed to a
                            repository.
                        </span>
                    </span>
                </Alert>
            </Card>

            <Card>
                <CardHeader title="Setting it up" className="mb-4" />
                <ol className="text-sm text-ink-700 space-y-2 m-0 pl-5">
                    {guide.steps.map((step) => (
                        <li key={step}>{step}</li>
                    ))}
                </ol>
            </Card>
        </div>
    );
}
