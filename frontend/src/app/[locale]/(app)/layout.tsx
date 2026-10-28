import type { ReactNode } from "react";
import { AppGate } from "@/components/auth/app-gate";

// Everything inside this group needs a signed-in person.
export default function AppLayout({ children }: { children: ReactNode }) {
  return <AppGate>{children}</AppGate>;
}
