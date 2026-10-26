import createMiddleware from "next-intl/middleware";
import { routing } from "./i18n/routing";

// Redirects "/" and any path without a locale prefix to the right language.
export default createMiddleware(routing);

export const config = {
  // Skip the API (served by the backend), Next internals, the health check and static files.
  matcher: ["/((?!api|trpc|_next|_vercel|healthz|.*\\..*).*)"],
};
