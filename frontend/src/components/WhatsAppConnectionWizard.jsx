import { useEffect, useRef, useState } from "react";
import { Check, Cloud, MessageCircle, RefreshCw, Smartphone } from "lucide-react";
import api, { apiErrorMessage } from "../services/api";
import Modal from "./ui/Modal";
import Button from "./ui/Button";
import Alert from "./ui/Alert";
import { StatusBadge } from "./ui/Badge";
import Spinner from "./ui/Spinner";
import { Field, Input, Select } from "./ui/Field";

const METHODS = [
    {
        accountType: "WHATSAPP",
        connectionMethod: "QR",
        icon: MessageCircle,
        title: "Normal WhatsApp",
        description: "Connect your existing WhatsApp account using QR/device linking.",
    },
    {
        accountType: "BUSINESS",
        connectionMethod: "QR",
        icon: Smartphone,
        title: "WhatsApp Business App",
        description: "Connect your existing WhatsApp Business app account using QR/device linking.",
    },
    {
        accountType: "API",
        connectionMethod: "CLOUD_API",
        icon: Cloud,
        title: "WhatsApp API",
        description: "Connect using official API / Business Platform credentials.",
    },
];

const SUCCESS_COPY = {
    WHATSAPP: { heading: "WhatsApp Connected", typeLabel: "Normal WhatsApp", cta: "Open WhatsApp Dashboard" },
    BUSINESS: { heading: "WhatsApp Business Connected", typeLabel: "WhatsApp Business", cta: "Continue" },
    API: { heading: "WhatsApp API Connected", typeLabel: "WhatsApp API", cta: "Continue" },
};

const POLL_INTERVAL_MS = 2000;

const EMPTY_API_FORM = {
    phone_number_id: "",
    whatsapp_business_account_id: "",
    access_token: "",
    app_secret: "",
    webhook_verify_token: "",
};

// connect/refresh-qr/test-api all fail with a custom {message: "..."} body
// (see ConnectionManager's human-readable error mapping on the backend)
// rather than DRF's usual validation-error shape, so prefer that field.
function connectionErrorMessage(err, fallback) {
    return err?.response?.data?.message || apiErrorMessage(err, fallback);
}

function formatCountdown(seconds) {
    if (seconds == null || seconds < 0) return "--:--";
    const mm = String(Math.floor(seconds / 60)).padStart(2, "0");
    const ss = String(seconds % 60).padStart(2, "0");
    return `${mm}:${ss}`;
}

function Steps({ current, methodLabel }) {
    const steps = ["Account created", methodLabel, "Connected"];
    return (
        <ol className="flex items-center gap-2 mb-6 m-0 p-0 list-none">
            {steps.map((label, index) => {
                const stepNumber = index + 1;
                const done = current > stepNumber;
                const active = current === stepNumber;
                return (
                    <li key={label} className="flex items-center gap-2 min-w-0">
                        <span
                            className={`shrink-0 w-5 h-5 rounded-full flex items-center justify-center text-[10px] font-bold ${
                                done
                                    ? "bg-brand-600 text-white"
                                    : active
                                      ? "bg-ink-900 text-ink-50"
                                      : "bg-ink-200 text-ink-500"
                            }`}
                        >
                            {done ? <Check size={11} strokeWidth={3} /> : stepNumber}
                        </span>
                        <span
                            className={`text-xs font-semibold truncate ${
                                done || active ? "text-ink-800" : "text-ink-400"
                            }`}
                        >
                            {label}
                        </span>
                        {index < steps.length - 1 && <span className="w-4 h-px bg-ink-200 shrink-0" />}
                    </li>
                );
            })}
        </ol>
    );
}

export default function WhatsAppConnectionWizard({ onClose, onConnected, existingAccount = null }) {
    const methodForAccount = (acc) => METHODS.find((m) => m.accountType === acc.account_type) || METHODS[0];

    const [step, setStep] = useState(existingAccount ? "reconnecting" : "select"); // select | reconnecting | qr | api | success
    const [name, setName] = useState(existingAccount?.name || "");
    const [selectedMethod, setSelectedMethod] = useState(existingAccount ? methodForAccount(existingAccount) : null);
    const [account, setAccount] = useState(existingAccount);
    const [error, setError] = useState("");
    const [busy, setBusy] = useState(false);

    const [qrData, setQrData] = useState(null);
    const [expiresAt, setExpiresAt] = useState(null);
    const [secondsLeft, setSecondsLeft] = useState(null);
    const [connectionMessage, setConnectionMessage] = useState("");

    const [apiForm, setApiForm] = useState(EMPTY_API_FORM);
    const [apiTested, setApiTested] = useState(false);
    const [apiTestResult, setApiTestResult] = useState(null);

    const pollRef = useRef(null);
    const countdownRef = useRef(null);
    const connectRequestedRef = useRef(false);

    useEffect(() => {
        return () => {
            clearInterval(pollRef.current);
            clearInterval(countdownRef.current);
        };
    }, []);

    // Reconnecting an existing account skips account creation entirely --
    // jump straight to (re)starting a connection attempt on it. Cloud API
    // accounts already have credentials stored, so this just re-validates
    // them; QR accounts need a fresh session/QR code.
    useEffect(() => {
        if (!existingAccount) return;

        if (existingAccount.status === "CONNECTED") {
            // "Details" on an already-connected account -- just show its
            // details. Re-running /connect/ here would start a brand new QR
            // pairing session and disrupt an active device link for no
            // reason.
            setStep("success");
            return;
        }

        if (existingAccount.status === "QR_READY") {
            // A QR session may already be waiting when the devices page is
            // opened (for example after an operator refreshes a corrupted
            // linked-device session). Reuse that session instead of calling
            // /connect/ and creating a second socket/QR for the same number.
            setStep("qr");
            void loadQr(existingAccount.id);
            startPolling(existingAccount.id);
            return;
        }

        const method = methodForAccount(existingAccount);
        if (method.connectionMethod === "CLOUD_API" && !existingAccount.has_access_token) {
            // No stored credentials to reconnect with (shouldn't normally
            // happen) -- fall back to the full credential form.
            setStep("api");
        } else if (!connectRequestedRef.current) {
            // React StrictMode runs this effect twice in development, and a
            // second /connect/ in the same instant used to create a second
            // QR session beside the first. Only one of the pair can be
            // scanned; the other expired two minutes later and took the
            // freshly connected account down with it. Guarding the request
            // rather than the whole effect matters: the simulated unmount
            // clears the poll interval, so the QR_READY branch above has to
            // be free to run again and restart it.
            connectRequestedRef.current = true;
            startConnection(existingAccount, method);
        }
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, []);

    useEffect(() => {
        clearInterval(countdownRef.current);
        if (!expiresAt) {
            setSecondsLeft(null);
            return;
        }
        const tick = () => {
            const remaining = Math.max(0, Math.round((new Date(expiresAt).getTime() - Date.now()) / 1000));
            setSecondsLeft(remaining);
        };
        tick();
        countdownRef.current = setInterval(tick, 1000);
        return () => clearInterval(countdownRef.current);
    }, [expiresAt]);

    const stopPolling = () => clearInterval(pollRef.current);

    const startPolling = (accountId) => {
        stopPolling();
        pollRef.current = setInterval(() => checkStatus(accountId), POLL_INTERVAL_MS);
    };

    const checkStatus = async (accountId) => {
        try {
            const response = await api.get(`/whatsapp/accounts/${accountId}/connection-status/`);
            const { status, message } = response.data;
            if (status === "CONNECTED") {
                stopPolling();
                setAccount((prev) => ({ ...prev, status, phone_number: response.data.phone_number }));
                setStep("success");
            } else if (status === "EXPIRED") {
                stopPolling();
                setConnectionMessage("QR code expired. Generate a new QR code.");
                setQrData(null);
            } else if (status === "ERROR") {
                stopPolling();
                setError(message || "Unable to connect this WhatsApp account.");
            }
        } catch {
            // A transient poll failure isn't worth surfacing -- the next tick retries.
        }
    };

    // Shared by "create + connect" (new QR account), "test then connect"
    // (new Cloud API account), and "reconnect" (existing account, any
    // method): calls /connect/ and routes to whichever step the result
    // implies. QR_READY needs a QR fetched + polling; Cloud API resolves
    // synchronously to CONNECTED or ERROR.
    const startConnection = async (targetAccount, methodChoice) => {
        const isQr = methodChoice?.connectionMethod === "QR";
        setBusy(true);
        setError("");
        setStep(isQr ? "qr" : "reconnecting");
        try {
            const connectResponse = await api.post(`/whatsapp/accounts/${targetAccount.id}/connect/`);
            const { status, expires_at, message } = connectResponse.data;

            if (status === "QR_READY") {
                setExpiresAt(expires_at);
                await loadQr(targetAccount.id);
                startPolling(targetAccount.id);
            } else if (status === "CONNECTED") {
                setAccount((prev) => ({ ...(prev || targetAccount), status: "CONNECTED" }));
                setStep("success");
            } else if (isQr) {
                setConnectionMessage(message || "Unable to start this connection.");
            } else {
                setStep("api");
                setError(message || "Unable to connect this WhatsApp account.");
            }
        } catch (err) {
            // The account row already exists (in ERROR status) -- surface
            // why rather than losing that context.
            const friendly = connectionErrorMessage(err, "Unable to start this connection.");
            if (isQr) {
                setConnectionMessage(friendly);
            } else {
                setStep("api");
                setError(friendly);
            }
        } finally {
            setBusy(false);
        }
    };

    const beginQrConnection = async (methodChoice) => {
        setError("");
        setBusy(true);

        let createdAccount;
        try {
            const createResponse = await api.post("/whatsapp/accounts/", {
                name,
                account_type: methodChoice.accountType,
                connection_method: methodChoice.connectionMethod,
            });
            createdAccount = createResponse.data;
            setAccount(createdAccount);
        } catch (err) {
            setError(apiErrorMessage(err, "Unable to create this WhatsApp account."));
            setBusy(false);
            return;
        }

        await startConnection(createdAccount, methodChoice);
    };

    const loadQr = async (accountId) => {
        try {
            const response = await api.get(`/whatsapp/accounts/${accountId}/qr/`);
            setQrData(response.data.qr_data);
            setExpiresAt(response.data.expires_at);
            setConnectionMessage("");
        } catch (err) {
            setConnectionMessage(apiErrorMessage(err, "Unable to load the QR code."));
        }
    };

    const refreshQr = async () => {
        if (!account) return;
        setBusy(true);
        setConnectionMessage("");
        try {
            const response = await api.post(`/whatsapp/accounts/${account.id}/refresh-qr/`);
            if (response.data.status === "QR_READY") {
                setExpiresAt(response.data.expires_at);
                await loadQr(account.id);
                startPolling(account.id);
            } else {
                setConnectionMessage(response.data.message || "Unable to refresh the QR code.");
            }
        } catch (err) {
            setConnectionMessage(connectionErrorMessage(err, "Unable to refresh the QR code."));
        } finally {
            setBusy(false);
        }
    };

    const cancelQr = () => {
        stopPolling();
        onConnected?.();
        onClose();
    };

    const handleContinue = () => {
        if (!name.trim() || !selectedMethod) return;
        if (selectedMethod.connectionMethod === "QR") {
            beginQrConnection(selectedMethod);
        } else {
            setStep("api");
        }
    };

    const updateApiField = (field) => (event) => {
        setApiForm((prev) => ({ ...prev, [field]: event.target.value }));
        setApiTested(false);
        setApiTestResult(null);
    };

    const testApiConnection = async (event) => {
        event.preventDefault();
        setError("");
        setBusy(true);
        try {
            let currentAccount = account;
            if (!currentAccount) {
                const createResponse = await api.post("/whatsapp/accounts/", {
                    name,
                    account_type: "API",
                    connection_method: "CLOUD_API",
                    ...apiForm,
                });
                currentAccount = createResponse.data;
                setAccount(currentAccount);
            } else {
                // Reconnecting an existing account whose stored credentials
                // didn't work -- save whatever new values were typed before
                // testing. Only non-blank fields are sent so a field left
                // empty keeps its previously stored value.
                const changedFields = Object.fromEntries(Object.entries(apiForm).filter(([, value]) => value));
                if (Object.keys(changedFields).length > 0) {
                    const updateResponse = await api.patch(`/whatsapp/accounts/${currentAccount.id}/`, changedFields);
                    currentAccount = updateResponse.data;
                    setAccount(currentAccount);
                }
            }
            const testResponse = await api.post(`/whatsapp/accounts/${currentAccount.id}/test-api/`);
            setApiTestResult({ success: true, ...testResponse.data });
            setApiTested(true);
        } catch (err) {
            setApiTestResult({ success: false, message: connectionErrorMessage(err, "These credentials could not be verified.") });
            setApiTested(false);
        } finally {
            setBusy(false);
        }
    };

    const finishApiConnection = async () => {
        if (!account) return;
        await startConnection(account, { connectionMethod: "CLOUD_API" });
    };

    const finish = () => {
        onConnected?.();
        onClose();
    };

    const stepIndex = { select: 1, qr: 2, api: 2, reconnecting: 2, success: 3 }[step];
    const successCopy = SUCCESS_COPY[selectedMethod?.accountType || "API"];

    return (
        <Modal
            title={existingAccount ? "Reconnect WhatsApp Device" : "Add WhatsApp Device"}
            onClose={onClose}
            width="lg"
        >
            {step !== "select" && (
                <Steps
                    current={stepIndex}
                    methodLabel={selectedMethod?.connectionMethod === "QR" ? "Scan QR" : "Enter credentials"}
                />
            )}

            <Alert>{error}</Alert>

            {step === "reconnecting" && (
                <div className="text-center py-10">
                    <Spinner />
                    <p className="text-sm text-ink-500 mt-2 mb-0">Reconnecting…</p>
                </div>
            )}

            {step === "select" && (
                <div>
                    <Field label="Device Name" required>
                        <Input
                            value={name}
                            onChange={(e) => setName(e.target.value)}
                            placeholder="e.g. Sales WhatsApp"
                            autoFocus
                            required
                        />
                    </Field>

                    <p className="text-sm font-semibold text-ink-700 mb-2">How do you want to connect?</p>
                    <div className="flex flex-col gap-2 mb-6">
                        {METHODS.map((method) => {
                            const Icon = method.icon;
                            const isSelected = selectedMethod?.accountType === method.accountType;
                            return (
                                <button
                                    type="button"
                                    key={method.accountType}
                                    onClick={() => setSelectedMethod(method)}
                                    aria-pressed={isSelected}
                                    className={`flex items-start gap-3 text-left rounded-xl border px-4 py-3.5 cursor-pointer transition-all ${
                                        isSelected
                                            ? "border-brand-500 bg-brand-50/60 ring-4 ring-brand-500/10"
                                            : "border-ink-200 bg-surface hover:border-ink-300 hover:bg-ink-50"
                                    }`}
                                >
                                    <span
                                        className={`shrink-0 w-9 h-9 rounded-lg flex items-center justify-center ${
                                            isSelected ? "bg-brand-600 text-white" : "bg-ink-100 text-ink-500"
                                        }`}
                                    >
                                        <Icon size={17} aria-hidden="true" />
                                    </span>
                                    <span className="min-w-0">
                                        <span className="block font-semibold text-sm text-ink-900">{method.title}</span>
                                        <span className="block text-xs text-ink-500 mt-0.5">{method.description}</span>
                                    </span>
                                    {isSelected && (
                                        <span className="shrink-0 ml-auto text-brand-600">
                                            <Check size={17} strokeWidth={2.5} />
                                        </span>
                                    )}
                                </button>
                            );
                        })}
                    </div>

                    <Button className="w-full" disabled={!name.trim() || !selectedMethod} loading={busy} onClick={handleContinue}>
                        {busy ? "Please wait..." : "Continue"}
                    </Button>
                </div>
            )}

            {step === "qr" && (
                <div className="text-center">
                    <div className="mx-auto w-56 h-56 p-2 rounded-xl border border-ink-200 bg-surface flex items-center justify-center">
                        {qrData ? (
                            <img src={qrData} alt="WhatsApp QR code" className="w-full h-full rounded-lg" />
                        ) : (
                            <Spinner />
                        )}
                    </div>

                    <ol className="text-sm text-ink-600 mt-5 text-left bg-ink-50 border border-ink-200 rounded-xl px-4 py-3.5 space-y-1 list-decimal list-inside m-0">
                        <li>Open WhatsApp on your phone</li>
                        <li>Go to Settings → Linked Devices</li>
                        <li>Tap “Link a Device” and scan this code</li>
                    </ol>

                    {qrData && (
                        <p className="text-sm font-semibold text-ink-700 mt-3 mb-0 tabular-nums">
                            QR expires in {formatCountdown(secondsLeft)}
                        </p>
                    )}

                    {connectionMessage && (
                        <Alert tone="info" className="mt-4 mb-0">
                            {connectionMessage}
                        </Alert>
                    )}

                    <div className="flex gap-2 mt-5">
                        <Button variant="secondary" icon={RefreshCw} className="flex-1" onClick={refreshQr} loading={busy}>
                            Refresh QR
                        </Button>
                        <Button variant="ghost" className="flex-1" onClick={cancelQr}>
                            Cancel
                        </Button>
                    </div>

                    {qrData && !connectionMessage && (
                        <p className="inline-flex items-center gap-2 text-xs text-ink-400 mt-4 mb-0">
                            <span className="w-1.5 h-1.5 rounded-full bg-brand-500 animate-pulse" />
                            Waiting for connection...
                        </p>
                    )}
                </div>
            )}

            {step === "api" && (
                <form onSubmit={testApiConnection}>
                    <Field label="API Provider">
                        <Select value="meta_cloud_api" disabled>
                            <option value="meta_cloud_api">Meta Cloud API</option>
                        </Select>
                    </Field>
                    <Field label="Phone Number ID" required>
                        <Input value={apiForm.phone_number_id} onChange={updateApiField("phone_number_id")} required />
                    </Field>
                    <Field label="WhatsApp Business Account ID" hint="Optional.">
                        <Input
                            value={apiForm.whatsapp_business_account_id}
                            onChange={updateApiField("whatsapp_business_account_id")}
                        />
                    </Field>
                    <Field label="Access Token" required>
                        <Input type="password" value={apiForm.access_token} onChange={updateApiField("access_token")} required />
                    </Field>
                    <Field label="App Secret" hint="Used to verify incoming webhook signatures." required>
                        <Input type="password" value={apiForm.app_secret} onChange={updateApiField("app_secret")} required />
                    </Field>
                    <Field label="Webhook Verify Token" hint="Set this same value in your Meta App's webhook subscription.">
                        <Input value={apiForm.webhook_verify_token} onChange={updateApiField("webhook_verify_token")} />
                    </Field>

                    {apiTestResult && (
                        <Alert tone={apiTestResult.success ? "success" : "error"}>
                            {apiTestResult.success
                                ? `Credentials verified${apiTestResult.phone_number ? ` — ${apiTestResult.phone_number}` : ""}.`
                                : apiTestResult.message}
                        </Alert>
                    )}

                    <div className="flex gap-2">
                        <Button type="submit" variant="secondary" className="flex-1" loading={busy}>
                            {busy ? "Testing..." : "Test Connection"}
                        </Button>
                        <Button type="button" className="flex-1" disabled={!apiTested || busy} onClick={finishApiConnection}>
                            Continue
                        </Button>
                    </div>
                </form>
            )}

            {step === "success" && account && (
                <div className="text-center py-2">
                    <span className="inline-flex w-12 h-12 rounded-full bg-brand-50 text-brand-600 items-center justify-center mb-3">
                        <Check size={24} strokeWidth={3} aria-hidden="true" />
                    </span>
                    <h3 className="text-lg font-bold text-ink-900 mb-5 mt-0">{successCopy.heading}</h3>

                    <div className="text-left bg-ink-50 border border-ink-200 rounded-xl p-4 mb-5 text-sm divide-y divide-ink-200">
                        <div className="flex justify-between items-center gap-3 py-2 first:pt-0">
                            <span className="text-ink-500">Device</span>
                            <strong className="text-ink-900 truncate">{account.name}</strong>
                        </div>
                        <div className="flex justify-between items-center gap-3 py-2">
                            <span className="text-ink-500">Phone</span>
                            <strong className="text-ink-900 tabular-nums">{account.phone_number || "—"}</strong>
                        </div>
                        <div className="flex justify-between items-center gap-3 py-2">
                            <span className="text-ink-500">Type</span>
                            <strong className="text-ink-900">{successCopy.typeLabel}</strong>
                        </div>
                        <div className="flex justify-between items-center gap-3 py-2 last:pb-0">
                            <span className="text-ink-500">Status</span>
                            <StatusBadge status="CONNECTED" />
                        </div>
                    </div>

                    <Button className="w-full" onClick={finish}>
                        {successCopy.cta}
                    </Button>
                </div>
            )}
        </Modal>
    );
}
