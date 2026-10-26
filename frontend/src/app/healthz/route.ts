// Liveness for the web container. The backend serves /api/healthz and /api/readyz.
export const dynamic = "force-static";

export function GET() {
  return Response.json({ status: "ok" });
}
