import { createContext, useCallback, useContext, useEffect, useState } from "react";
import axios from "axios";
import api from "../services/api";
import { applyBrandColor } from "../utils/brandColor";
import { useDarkMode } from "../utils/theme";

/**
 * The brand this installation is wearing.
 *
 * Two sources, because the login page has to be branded before anyone has an
 * account to look branding up with: signed out, it is resolved from the host
 * (a reseller's own domain); signed in, from the user's tenant. The signed-in
 * answer wins as soon as it arrives, so someone reaching a reseller's domain
 * and signing into a differently-branded customer ends up with the right one.
 *
 * Defaults are baked in rather than left blank, so a failed request paints
 * the stock product instead of an app with no name.
 */

const DEFAULTS = {
    product_name: "SendStack",
    logo_url: "",
    primary_color: "#059669",
    support_email: "",
};

const BrandingContext = createContext({ branding: DEFAULTS, reloadBranding: () => {} });

export function BrandingProvider({ children }) {
    const [branding, setBranding] = useState(DEFAULTS);

    const loadPublic = useCallback(async () => {
        try {
            // Deliberately not the shared client: that one attaches a token
            // and redirects to /login on 401, neither of which makes sense
            // for the endpoint the login page itself calls.
            const response = await axios.get(
                `${api.defaults.baseURL || "/api/v1"}/tenants/branding/public/`,
                { params: { host: window.location.hostname } }
            );
            setBranding({ ...DEFAULTS, ...response.data });
        } catch {
            setBranding(DEFAULTS);
        }
    }, []);

    const reloadBranding = useCallback(async () => {
        if (!localStorage.getItem("access_token")) {
            await loadPublic();
            return;
        }
        try {
            const response = await api.get("/tenants/branding/effective/");
            setBranding({ ...DEFAULTS, ...response.data });
        } catch {
            await loadPublic();
        }
    }, [loadPublic]);

    useEffect(() => {
        reloadBranding();
    }, [reloadBranding]);

    // Colour is a document-level concern (it drives every `brand-` utility),
    // and the name is what the browser tab says.
    // Dark mode derives the tints and text steps from this same colour, so
    // the ramp is re-applied when the theme flips.
    const dark = useDarkMode();
    useEffect(() => {
        applyBrandColor(branding.primary_color, dark);
    }, [branding.primary_color, dark]);

    useEffect(() => {
        document.title = branding.product_name || DEFAULTS.product_name;
    }, [branding.product_name]);

    return (
        <BrandingContext.Provider value={{ branding, reloadBranding }}>{children}</BrandingContext.Provider>
    );
}

export function useBranding() {
    return useContext(BrandingContext);
}
