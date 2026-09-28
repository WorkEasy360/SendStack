import { useEffect, useState } from "react";
import { LifeBuoy, Send } from "lucide-react";
import api, { apiErrorMessage } from "../../services/api";
import { useBranding } from "../../context/BrandingContext";
import { useToast } from "../../components/ui/Toast";
import { usePageTitle } from "../../hooks/useSeo";
import Alert from "../../components/ui/Alert";
import Badge from "../../components/ui/Badge";
import Button from "../../components/ui/Button";
import Card, { CardHeader } from "../../components/ui/Card";
import EmptyState from "../../components/ui/EmptyState";
import PageHeader from "../../components/ui/PageHeader";
import Spinner from "../../components/ui/Spinner";
import { Field, Input, Select, Textarea } from "../../components/ui/Field";

/**
 * Where a customer asks for help without leaving the product.
 *
 * The form is deliberately short -- a category, a subject, what happened --
 * and everything else useful to whoever answers is captured by the server at
 * the moment of asking: the plan, the device states, recent failure counts,
 * the correlation id of this very request. Asking the customer to gather that
 * is how a support form becomes a form nobody fills in, and they would be
 * guessing at half of it anyway.
 *
 * The panel beside the form says what that snapshot contains and, just as
 * importantly, what it does not. A customer who is told their contacts and
 * message content are never attached does not have to wonder.
 */
function Support() {
    const { branding } = useBranding();
    const toast = useToast();

    usePageTitle(`Support · ${branding.product_name}`);

    const [categories, setCategories] = useState([]);
    const [requests, setRequests] = useState([]);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState("");
    const [submitting, setSubmitting] = useState(false);
    const [form, setForm] = useState({ category: "TECHNICAL", subject: "", message: "" });

    const load = async () => {
        try {
            const [categoriesRes, requestsRes] = await Promise.all([
                api.get("/support/requests/categories/"),
                api.get("/support/requests/"),
            ]);
            setCategories(categoriesRes.data.categories || []);
            setRequests(requestsRes.data.results || requestsRes.data || []);
        } catch (err) {
            setError(apiErrorMessage(err, "Your support requests could not be loaded."));
        } finally {
            setLoading(false);
        }
    };

    useEffect(() => {
        load();
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, []);

    const submit = async (event) => {
        event.preventDefault();
        setSubmitting(true);
        setError("");
        try {
            const response = await api.post("/support/requests/", form);
            toast.success(`Request ${response.data.reference} sent. We will be in touch.`);
            setForm({ category: "TECHNICAL", subject: "", message: "" });
            await load();
        } catch (err) {
            setError(apiErrorMessage(err, "Your request could not be sent."));
        } finally {
            setSubmitting(false);
        }
    };

    return (
        <div>
            <PageHeader
                title="Support"
                description="Tell us what is happening and we will look into it."
            />

            <Alert>{error}</Alert>

            <div className="grid gap-6 lg:grid-cols-[minmax(0,1fr)_22rem] items-start">
                <Card>
                    <CardHeader title="Ask for help" className="mb-4" />
                    <form onSubmit={submit} className="space-y-4">
                        <Field label="What is this about?">
                            <Select
                                value={form.category}
                                onChange={(e) => setForm({ ...form, category: e.target.value })}
                            >
                                {categories.map((category) => (
                                    <option key={category.value} value={category.value}>
                                        {category.label}
                                    </option>
                                ))}
                            </Select>
                        </Field>

                        <Field label="Subject" required>
                            <Input
                                value={form.subject}
                                maxLength={160}
                                required
                                placeholder="Campaign did not send this morning"
                                onChange={(e) => setForm({ ...form, subject: e.target.value })}
                            />
                        </Field>

                        <Field
                            label="What happened?"
                            required
                            hint="What you were doing, what you expected, and what happened instead."
                        >
                            <Textarea
                                rows={6}
                                value={form.message}
                                required
                                onChange={(e) => setForm({ ...form, message: e.target.value })}
                            />
                        </Field>

                        <Button type="submit" disabled={submitting} className="gap-2">
                            <Send size={16} aria-hidden="true" />
                            {submitting ? "Sending…" : "Send request"}
                        </Button>
                    </form>
                </Card>

                <Card>
                    <CardHeader title="What we already know" className="mb-4" />
                    {/*
                      Said explicitly rather than left implicit. A customer who
                      knows the device state and failure counts travel with the
                      request writes a shorter, more useful one -- and a customer
                      who does not know might reasonably wonder what else is read.
                    */}
                    <p className="text-sm text-ink-500 m-0 mb-3">
                        When you send a request we attach a snapshot of your workspace so we do not
                        have to ask: your plan, your usage, whether your numbers are connected, and
                        how many messages failed recently.
                    </p>
                    <p className="text-sm text-ink-500 m-0">
                        We never attach your contacts, their phone numbers, or the content of any
                        message you have sent.
                    </p>
                    {branding.support_email && (
                        <p className="text-sm text-ink-500 mt-3 mb-0">
                            You can also email{" "}
                            <a
                                href={`mailto:${branding.support_email}`}
                                className="text-brand-700 font-medium"
                            >
                                {branding.support_email}
                            </a>
                            .
                        </p>
                    )}
                </Card>
            </div>

            <Card className="mt-6">
                <CardHeader title="Your requests" className="mb-4" />
                {loading && <Spinner />}
                {!loading && requests.length === 0 && (
                    <EmptyState
                        icon={LifeBuoy}
                        title="No support requests yet"
                        description="When you send one it appears here, with a reference you can quote."
                        variant="plain"
                    />
                )}
                {!loading && requests.length > 0 && (
                    <ul className="list-none p-0 m-0 divide-y divide-ink-100">
                        {requests.map((request) => (
                            <li key={request.id} className="py-3 flex items-start justify-between gap-4">
                                <div className="min-w-0">
                                    <p className="text-sm font-medium text-ink-900 m-0">{request.subject}</p>
                                    <p className="text-xs text-ink-400 m-0 mt-0.5">
                                        {request.reference} · {request.category_label} ·{" "}
                                        {new Date(request.created_at).toLocaleDateString()}
                                    </p>
                                </div>
                                <Badge tone={request.status === "RESOLVED" ? "green" : "gray"}>
                                    {request.status_label}
                                </Badge>
                            </li>
                        ))}
                    </ul>
                )}
            </Card>
        </div>
    );
}

export default Support;
