import express from "express";
import pino from "pino";

import { config } from "./config.js";
import { requireApiKey } from "./auth.js";
import { buildRoutes } from "./routes.js";
import { SessionStore } from "./sessionStore.js";
import { BaileysProvider } from "./providers/baileysProvider.js";

const logger = pino({ level: config.logLevel, name: "qr-bridge" });

async function main() {
    const store = new SessionStore(config.dataDir);
    const provider = new BaileysProvider(store);

    const app = express();
    app.use(express.json());

    // Unauthenticated -- used by Docker/orchestrator healthchecks.
    app.get("/health", (_req, res) => res.json({ status: "ok" }));

    app.use(requireApiKey);
    app.use(buildRoutes(provider));

    app.use((err: unknown, _req: express.Request, res: express.Response, _next: express.NextFunction) => {
        logger.error({ err }, "Unhandled error");
        res.status(500).json({ error: "Internal error." });
    });

    app.listen(config.port, () => {
        logger.info(`qr-bridge listening on :${config.port}`);
    });

    await provider.resumePersistedSessions();
}

main().catch((err) => {
    logger.error({ err }, "Fatal startup error");
    process.exit(1);
});
