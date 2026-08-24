import type { NextFunction, Request, Response } from "express";
import crypto from "node:crypto";
import { config } from "./config.js";

function safeEqual(a: string, b: string): boolean {
    const bufA = Buffer.from(a);
    const bufB = Buffer.from(b);
    if (bufA.length !== bufB.length) return false;
    return crypto.timingSafeEqual(bufA, bufB);
}

/** Requires `Authorization: Bearer <QR_PROVIDER_API_KEY>`, matching what qr_bridge.py sends. */
export function requireApiKey(req: Request, res: Response, next: NextFunction): void {
    const header = req.headers.authorization ?? "";
    const token = header.startsWith("Bearer ") ? header.slice("Bearer ".length) : "";
    if (!token || !safeEqual(token, config.apiKey)) {
        res.status(401).json({ error: "Invalid or missing API key." });
        return;
    }
    next();
}
