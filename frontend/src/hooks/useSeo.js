import { useEffect } from "react";

/**
 * Set the page's title, description, canonical URL and social preview.
 *
 * A single-page app serves one `index.html` for every route, so without this
 * every page a search engine or a chat client sees is called "SendStack" and
 * described by whatever was in the template. That is fine for the thirty
 * screens behind a login and wrong for the four in front of it, which are the
 * only ones anybody discovers.
 *
 * **Indexing is opt-in, and enforced from both ends.** `index.html` ships a
 * static `noindex, nofollow`, so the default state of the document -- and
 * therefore of every authenticated screen, none of which calls this hook -- is
 * "do not index". A public page opts in by passing `index: true`, and this
 * hook puts the `noindex` back when that page unmounts. Doing it in the
 * cleanup rather than in a route-level effect is what makes it reliable:
 * React runs a child's effects before its parent's, so a "reset on
 * navigation" effect higher up the tree would overwrite the public page's
 * opt-in instead of preceding it.
 *
 * A crawler cannot sign in, so this is not the access control -- that is the
 * API's job and it does it. It is for the previews: a customer pasting a link
 * to their campaign results into a group chat invites an unauthenticated fetch
 * of that URL, and the tag is what keeps the result out of an index.
 *
 * Rendered client-side, so a crawler that does not run JavaScript sees only
 * the static tags in index.html. That is the accepted trade for a SPA with
 * four public pages; a prerender service would be more machinery than the
 * problem.
 */

const DEFAULT_IMAGE = "/favicon.svg";
const PRIVATE = "noindex, nofollow";

function upsertMeta(selector, attributes) {
    let element = document.head.querySelector(selector);
    if (!element) {
        element = document.createElement("meta");
        Object.entries(attributes).forEach(([key, value]) => {
            if (key !== "content") element.setAttribute(key, value);
        });
        document.head.appendChild(element);
    }
    if (attributes.content !== undefined) {
        element.setAttribute("content", attributes.content);
    }
    return element;
}

function upsertLink(rel, href) {
    let element = document.head.querySelector(`link[rel="${rel}"]`);
    if (!element) {
        element = document.createElement("link");
        element.setAttribute("rel", rel);
        document.head.appendChild(element);
    }
    element.setAttribute("href", href);
    return element;
}

function removeLink(rel) {
    const element = document.head.querySelector(`link[rel="${rel}"]`);
    if (element) element.remove();
}

/**
 * @param {object} options
 * @param {string} options.title         The full <title>, written out rather than templated.
 * @param {string} [options.description] Meta description, reused for the social preview.
 * @param {string} [options.canonical]   Path this page canonically lives at, e.g. "/pricing".
 * @param {boolean} [options.index]      Whether crawlers may index it. Default false.
 * @param {string} [options.image]       Root-relative or absolute social preview image.
 */
export function useSeo({ title, description, canonical, index = false, image = DEFAULT_IMAGE }) {
    useEffect(() => {
        const previousTitle = document.title;
        if (title) document.title = title;

        upsertMeta('meta[name="robots"]', {
            name: "robots",
            content: index ? "index, follow" : PRIVATE,
        });

        if (description) {
            upsertMeta('meta[name="description"]', { name: "description", content: description });
            upsertMeta('meta[property="og:description"]', {
                property: "og:description",
                content: description,
            });
        }

        if (title) {
            upsertMeta('meta[property="og:title"]', { property: "og:title", content: title });
        }

        upsertMeta('meta[property="og:type"]', { property: "og:type", content: "website" });
        upsertMeta('meta[name="twitter:card"]', { name: "twitter:card", content: "summary_large_image" });

        if (image) {
            const absolute = image.startsWith("http") ? image : `${window.location.origin}${image}`;
            upsertMeta('meta[property="og:image"]', { property: "og:image", content: absolute });
        }

        if (canonical) {
            // Built from the live origin rather than a configured base URL: a
            // reseller serving this on their own domain must canonicalise to
            // their domain, not to the platform's.
            const href = canonical.startsWith("http")
                ? canonical
                : `${window.location.origin}${canonical}`;
            upsertLink("canonical", href);
            upsertMeta('meta[property="og:url"]', { property: "og:url", content: href });
        }

        return () => {
            document.title = previousTitle;
            // Back to the closed default. Whatever renders next has to ask.
            upsertMeta('meta[name="robots"]', { name: "robots", content: PRIVATE });
            if (canonical) removeLink("canonical");
        };
    }, [title, description, canonical, index, image]);
}

/**
 * The private-page case: name the tab and nothing else.
 *
 * Separate from `useSeo` so a screen behind the login cannot become indexable
 * by copying a call and leaving an argument off.
 */
export function usePageTitle(title) {
    useSeo({ title, index: false, image: undefined });
}

export default useSeo;
