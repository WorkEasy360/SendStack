import QRCode from "qrcode";

/** Renders Baileys' raw QR pairing string to a ready-to-display PNG data URL. */
export async function renderQrDataUrl(rawQr: string): Promise<string> {
    return QRCode.toDataURL(rawQr, { margin: 1, width: 320 });
}
