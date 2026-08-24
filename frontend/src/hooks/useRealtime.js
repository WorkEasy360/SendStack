import { useEffect, useRef, useState } from "react";
import api from "../services/api";

/**
 * A live connection to the tenant's event stream.
 *
 * The server hands out a **single-use** ticket and closes each stream after
 * ten minutes, which makes `EventSource`'s own reconnection useless: it
 * retries the same URL, the spent ticket is refused, and the browser settles
 * into a 401 loop. So the connection is driven from here instead — every
 * attempt, including the ones after a routine expiry, mints a fresh ticket.
 *
 * Live updates are a convenience over an API that already works. Anything
 * that goes wrong ends in `connected: false`, which is the caller's cue to
 * poll; nothing here is allowed to break the page.
 */

const FIRST_RETRY_MS = 1000;
const MAX_RETRY_MS = 30000;

export default function useRealtime({ enabled = true, onEvent } = {}) {
    const [connected, setConnected] = useState(false);
    // null while unknown; false once the server says Redis is unreachable, at
    // which point reconnecting is pointless and we stop trying.
    const [available, setAvailable] = useState(null);

    // Kept in a ref so a caller passing an inline handler — which every render
    // makes anew — does not tear the stream down and build it again.
    const handlerRef = useRef(onEvent);
    handlerRef.current = onEvent;

    useEffect(() => {
        if (!enabled) {
            setConnected(false);
            return undefined;
        }

        let source = null;
        let retryTimer = null;
        let delay = FIRST_RETRY_MS;
        // One flag for "this effect is over": stops a reconnect scheduled a
        // moment before unmount from opening a stream nobody will close.
        let cancelled = false;

        const scheduleRetry = () => {
            if (cancelled || retryTimer) return;
            const wait = delay;
            delay = Math.min(delay * 2, MAX_RETRY_MS);
            retryTimer = setTimeout(() => {
                retryTimer = null;
                connect();
            }, wait);
        };

        const connect = async () => {
            if (cancelled) return;

            let ticket;
            try {
                const response = await api.post("/realtime/ticket/");
                ticket = response.data.ticket;
            } catch {
                scheduleRetry();
                return;
            }
            if (cancelled || !ticket) return;

            const base = api.defaults.baseURL || "/api/v1";
            source = new EventSource(`${base}/realtime/stream/?ticket=${encodeURIComponent(ticket)}`);
            const mine = source;

            mine.onmessage = (event) => {
                let payload;
                try {
                    payload = JSON.parse(event.data);
                } catch {
                    return; // A frame we cannot read is not worth a broken inbox.
                }

                if (payload.type === "connected") {
                    setConnected(true);
                    setAvailable(true);
                    delay = FIRST_RETRY_MS;
                    return;
                }
                if (payload.type === "unavailable") {
                    // The deployment has no working Redis. Stop reconnecting
                    // and let the caller poll rather than hammering a stream
                    // that will never carry anything.
                    cancelled = true;
                    setAvailable(false);
                    setConnected(false);
                    mine.close();
                    return;
                }
                if (payload.type === "error") {
                    setConnected(false);
                    return;
                }

                handlerRef.current?.(payload);
            };

            // Fires both on a real failure and on the server's routine
            // ten-minute close. Either way the ticket is spent, so the only
            // way back is a new one.
            mine.onerror = () => {
                setConnected(false);
                mine.close();
                if (source === mine) source = null;
                scheduleRetry();
            };
        };

        connect();

        return () => {
            cancelled = true;
            if (retryTimer) clearTimeout(retryTimer);
            if (source) source.close();
            setConnected(false);
        };
    }, [enabled]);

    return { connected, available };
}
