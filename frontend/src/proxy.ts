import createMiddleware from "next-intl/middleware";
import { NextRequest, NextResponse } from "next/server";
import { buildCsp, makeNonce } from "./lib/csp";
import { routing } from "./i18n/routing";

// Redirects "/" and any path without a locale prefix to the right language.
const intl = createMiddleware(routing);

/**
 * Every page gets a Content-Security-Policy with a nonce made for that request (F-03). The nonce
 * goes to Next in the request headers so the scripts it writes carry it, and the policy goes back
 * on the response. The API is served by the backend and is not covered here.
 */
export default function proxy(request: NextRequest) {
  const nonce = makeNonce();
  // Caddy ends TLS and tells the web container which scheme the visitor used.
  const secure =
    request.headers.get("x-forwarded-proto") === "https" || request.nextUrl.protocol === "https:";
  const policy = buildCsp({
    nonce,
    development: process.env.NODE_ENV === "development",
    secure,
  });
  // The app has no page with a dot in its address. Next would answer such a path with a prebuilt
  // 404 page whose scripts cannot carry this request's nonce, so it is answered here, as plain
  // text, with the policy.
  const last = request.nextUrl.pathname.split("/").pop() ?? "";
  if (last.includes(".")) {
    return new NextResponse("Not found\n", {
      status: 404,
      headers: {
        "Content-Type": "text/plain; charset=utf-8",
        "Content-Security-Policy": policy,
        "X-Content-Type-Options": "nosniff",
      },
    });
  }
  const headers = new Headers(request.headers);
  headers.set("x-nonce", nonce);
  headers.set("Content-Security-Policy", policy);
  const response = intl(new NextRequest(request, { headers }));
  response.headers.set("Content-Security-Policy", policy);
  return response;
}

export const config = {
  // Skip the API (served by the backend), the build's own files, the health check and the icon.
  // A path with a dot in it is NOT skipped: it is answered by the proxy itself (see above).
  matcher: ["/((?!api|trpc|_next/static|_next/image|_vercel|healthz|favicon\\.ico).*)"],
};
