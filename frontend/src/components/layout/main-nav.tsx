"use client";

import { useTranslations } from "next-intl";
import { useSession } from "@/components/auth/session-provider";
import { buttonVariants } from "@/components/ui/button";
import { Link, usePathname } from "@/i18n/navigation";
import { cn } from "@/lib/utils";

type Entry = { href: "/" | "/items"; label: "home" | "items" };

const ENTRIES: Entry[] = [
  { href: "/", label: "home" },
  { href: "/items", label: "items" },
];

/**
 * The way between the pages of the app. Shown only to someone who is signed in and has answered
 * the onboarding questions (before that every page sends them back to the questions). Hiding a
 * link is a convenience, never authorization: the backend answers every call by itself.
 */
export function MainNav() {
  const t = useTranslations("Nav");
  const pathname = usePathname();
  const { state } = useSession();
  if (state.status !== "authenticated") return null;
  if (typeof state.session.user.onboarding_completed_at !== "string") return null;

  return (
    <nav aria-label={t("main")}>
      <ul className="flex flex-wrap gap-1">
        {ENTRIES.map((entry) => {
          const current = entry.href === "/" ? pathname === "/" : pathname.startsWith(entry.href);
          return (
            <li key={entry.href}>
              <Link
                href={entry.href}
                aria-current={current ? "page" : undefined}
                className={cn(
                  buttonVariants({ variant: current ? "default" : "ghost", size: "sm" }),
                )}
              >
                {t(entry.label)}
              </Link>
            </li>
          );
        })}
      </ul>
    </nav>
  );
}
