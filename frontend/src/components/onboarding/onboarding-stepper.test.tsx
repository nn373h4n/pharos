import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import type { ReactElement } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import en from "../../../messages/en.json";
import type { Department, Profession, Profile, ProfileOptions } from "@/lib/api";

const search = new URLSearchParams();
const hardNavigate = vi.fn();
const getProfile = vi.fn<() => Promise<Profile>>();
const getProfileOptions = vi.fn<() => Promise<ProfileOptions>>();
const getProfessions = vi.fn<() => Promise<Profession[]>>();
const getDepartments = vi.fn<() => Promise<Department[]>>();
const saveProfile = vi.fn<(change: unknown, csrf: string) => Promise<Profile>>();
const completeOnboarding = vi.fn<(csrf: string) => Promise<Profile>>();

vi.mock("next/navigation", () => ({ useSearchParams: () => search }));
vi.mock("@/lib/navigate", () => ({ hardNavigate: (url: string) => hardNavigate(url) }));
vi.mock("@/lib/api", async (original) => ({
  ...(await original<typeof import("@/lib/api")>()),
  getProfile: () => getProfile(),
  getProfileOptions: () => getProfileOptions(),
  getProfessions: () => getProfessions(),
  getDepartments: () => getDepartments(),
  saveProfile: (change: unknown, csrf: string) => saveProfile(change, csrf),
  completeOnboarding: (csrf: string) => completeOnboarding(csrf),
}));
vi.mock("@/components/auth/session-provider", () => ({
  useSession: () => ({
    state: {
      status: "authenticated",
      session: { user: {}, csrf_token: "csrf-1" },
    },
  }),
}));

const { ApiProblem } = await import("@/lib/api");
const { OnboardingStepper } = await import("./onboarding-stepper");

const empty: Profile = {
  profession_id: null,
  ai_level: null,
  interests: [],
  department_id: null,
  locale: null,
  effective_locale: "en",
  onboarding_started_at: null,
  onboarding_completed_at: null,
};

const professions: Profession[] = [
  { id: "p1", code: "accountant", names: { en: "Accountant", az: "Mühasib" } },
  { id: "p2", code: "hr", names: { en: "HR specialist", az: "HR mütəxəssisi" } },
];
const departments: Department[] = [{ id: "d1", name: "Finance" }];
const options: ProfileOptions = {
  ai_levels: ["beginner", "intermediate", "advanced"],
  interests: ["writing", "summarizing", "brand_new_topic"],
  locales: ["en", "az", "tr", "ru"],
};

function show(element: ReactElement) {
  return render(
    <NextIntlClientProvider locale="en" messages={en}>
      {element}
    </NextIntlClientProvider>,
  );
}

async function ready(profile: Profile = empty) {
  getProfile.mockResolvedValue(profile);
  show(<OnboardingStepper />);
  await screen.findByRole("heading", { level: 2 });
}

const next = () => fireEvent.click(screen.getByRole("button", { name: /^(Next|Finish)$/ }));
const heading = () => screen.getByRole("heading", { level: 2 }).textContent;

beforeEach(() => {
  for (const key of [...search.keys()]) search.delete(key);
  getProfileOptions.mockResolvedValue(options);
  getProfessions.mockResolvedValue(professions);
  getDepartments.mockResolvedValue(departments);
  saveProfile.mockResolvedValue(empty);
  completeOnboarding.mockResolvedValue(empty);
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("OnboardingStepper", () => {
  it("starts with the profession and says where the person is", async () => {
    await ready();
    expect(heading()).toBe("What is your profession?");
    // Named twice on purpose: once for screen readers (the legend), once in the visible hint.
    expect(screen.getAllByText(/Step 1 of 4/)).toHaveLength(2);
    const current = screen
      .getByRole("navigation", { name: "Progress" })
      .querySelector("[aria-current]");
    expect(current?.textContent).toBe("Profession");
    expect(screen.getByRole("radio", { name: "Accountant" })).toBeTruthy();
  });

  it("will not go on without an answer, and does not call the server", async () => {
    await ready();
    next();
    expect((await screen.findByRole("alert")).textContent).toBe(
      "Choose your profession to continue.",
    );
    expect(saveProfile).not.toHaveBeenCalled();
    expect(heading()).toBe("What is your profession?");
  });

  it("saves the profession and the optional department, then asks about interests", async () => {
    await ready();
    fireEvent.click(screen.getByRole("radio", { name: "Accountant" }));
    fireEvent.change(screen.getByLabelText("Department (optional)"), { target: { value: "d1" } });
    next();
    await waitFor(() => expect(heading()).toBe("What would you like AI to help with?"));
    expect(saveProfile).toHaveBeenCalledWith(
      { profession_id: "p1", department_id: "d1" },
      "csrf-1",
    );
  });

  it("shows profession names in the language of the page, falling back to English", async () => {
    getProfessions.mockResolvedValue([
      { id: "p9", code: "only_en", names: { en: "Only English" } },
    ]);
    await ready();
    expect(screen.getByRole("radio", { name: "Only English" })).toBeTruthy();
  });

  it("collects interests, names the ones it has text for, and shows other codes as they are", async () => {
    await ready({ ...empty, profession_id: "p1" });
    expect(heading()).toBe("What would you like AI to help with?");
    expect(screen.getByRole("checkbox", { name: "Writing" })).toBeTruthy();
    fireEvent.click(screen.getByRole("checkbox", { name: "Writing" }));
    fireEvent.click(screen.getByRole("checkbox", { name: "brand_new_topic" }));
    fireEvent.click(screen.getByRole("checkbox", { name: "Writing" })); // changed their mind
    next();
    await waitFor(() => expect(heading()).toBe("Which language do you prefer?"));
    expect(saveProfile).toHaveBeenCalledWith({ interests: ["brand_new_topic"] }, "csrf-1");
  });

  it("needs at least one interest", async () => {
    await ready({ ...empty, profession_id: "p1" });
    next();
    expect((await screen.findByRole("alert")).textContent).toBe(
      "Choose at least one interest to continue.",
    );
    expect(saveProfile).not.toHaveBeenCalled();
  });

  it("switches the page to the chosen language and continues with the last step", async () => {
    await ready({ ...empty, profession_id: "p1", interests: ["writing"] });
    expect(heading()).toBe("Which language do you prefer?");
    fireEvent.click(screen.getByRole("radio", { name: "Azərbaycanca" }));
    next();
    await waitFor(() => expect(hardNavigate).toHaveBeenCalledWith("/az/onboarding?step=4"));
    expect(saveProfile).toHaveBeenCalledWith({ locale: "az" }, "csrf-1");
  });

  it("stays on the page when the chosen language is the one already shown", async () => {
    await ready({ ...empty, profession_id: "p1", interests: ["writing"] });
    fireEvent.click(screen.getByRole("radio", { name: "English" }));
    next();
    await waitFor(() => expect(heading()).toBe("How much have you used AI so far?"));
    expect(hardNavigate).not.toHaveBeenCalled();
  });

  it("saves the experience, completes onboarding and goes to the start page", async () => {
    await ready({ ...empty, profession_id: "p1", interests: ["writing"], locale: "en" });
    expect(heading()).toBe("How much have you used AI so far?");
    expect(screen.getByText("I use it now and then.")).toBeTruthy();
    fireEvent.click(screen.getByRole("radio", { name: /Intermediate/ }));
    next();
    await waitFor(() => expect(hardNavigate).toHaveBeenCalledWith("/en"));
    expect(saveProfile).toHaveBeenCalledWith({ ai_level: "intermediate" }, "csrf-1");
    expect(completeOnboarding).toHaveBeenCalledWith("csrf-1");
    expect(saveProfile.mock.invocationCallOrder[0]!).toBeLessThan(
      completeOnboarding.mock.invocationCallOrder[0]!,
    );
  });

  it("continues where the person stopped, or where the address says", async () => {
    search.set("step", "3");
    await ready({ ...empty, profession_id: "p1", interests: ["writing"] });
    expect(heading()).toBe("Which language do you prefer?");
  });

  it("ignores a step in the address that does not exist", async () => {
    search.set("step", "9");
    await ready({ ...empty, profession_id: "p1" });
    expect(heading()).toBe("What would you like AI to help with?");
  });

  it("keeps the answers when going back", async () => {
    await ready({ ...empty, profession_id: "p1", interests: ["writing"] });
    fireEvent.click(screen.getByRole("button", { name: "Back" }));
    expect(heading()).toBe("What would you like AI to help with?");
    expect((screen.getByRole("checkbox", { name: "Writing" }) as HTMLInputElement).checked).toBe(
      true,
    );
  });

  it("does not ask again once onboarding is done", async () => {
    getProfile.mockResolvedValue({ ...empty, onboarding_completed_at: "2026-10-28T09:00:00Z" });
    show(<OnboardingStepper />);
    await waitFor(() => expect(hardNavigate).toHaveBeenCalledWith("/en"));
    expect(screen.queryByRole("heading", { level: 2 })).toBeNull();
  });

  it.each([
    [new ApiProblem(422, "validation_failed"), "Your answer could not be saved. Try again."],
    [new ApiProblem(503, "service_unavailable"), "The service is not available right now."],
    [new ApiProblem(0, "network_error"), "The service is not available right now."],
  ])(
    "says what went wrong when saving fails (%#) and lets the person try again",
    async (problem, text) => {
      await ready();
      saveProfile.mockRejectedValue(problem);
      fireEvent.click(screen.getByRole("radio", { name: "Accountant" }));
      next();
      expect((await screen.findByRole("alert")).textContent).toContain(text);
      expect(heading()).toBe("What is your profession?");
      expect((screen.getByRole("button", { name: "Next" }) as HTMLButtonElement).disabled).toBe(
        false,
      );
    },
  );

  it("goes back to the first open step when the server says answers are missing", async () => {
    await ready({ ...empty, profession_id: "p1", interests: ["writing"], locale: "en" });
    completeOnboarding.mockRejectedValue(new ApiProblem(409, "onboarding_incomplete"));
    getProfile.mockResolvedValue({ ...empty, profession_id: "p1", locale: "en" }); // interests were cleared
    fireEvent.click(screen.getByRole("radio", { name: /Beginner/ }));
    next();
    expect((await screen.findByRole("alert")).textContent).toContain("earlier answers are missing");
    await waitFor(() => expect(heading()).toBe("What would you like AI to help with?"));
    expect(hardNavigate).not.toHaveBeenCalled();
  });

  it("does not send a department the person did not change", async () => {
    await ready({ ...empty, department_id: "d1" });
    expect((screen.getByLabelText("Department (optional)") as HTMLSelectElement).value).toBe("d1");
    fireEvent.click(screen.getByRole("radio", { name: "Accountant" }));
    next();
    await waitFor(() => expect(saveProfile).toHaveBeenCalled());
    expect(saveProfile).toHaveBeenCalledWith({ profession_id: "p1" }, "csrf-1");
  });

  it("clears a department the person removed", async () => {
    search.set("step", "1");
    await ready({ ...empty, profession_id: "p1", department_id: "d1" });
    fireEvent.change(screen.getByLabelText("Department (optional)"), { target: { value: "" } });
    next();
    await waitFor(() =>
      expect(saveProfile).toHaveBeenCalledWith(
        { profession_id: "p1", department_id: null },
        "csrf-1",
      ),
    );
  });

  it("does not let the address skip questions that are still open", async () => {
    search.set("step", "4");
    await ready({ ...empty, profession_id: "p1" });
    expect(heading()).toBe("What would you like AI to help with?");
  });

  it("does not follow a saved language that is not one of ours", async () => {
    search.set("step", "3");
    await ready({
      ...empty,
      profession_id: "p1",
      interests: ["writing"],
      locale: "//evil.test",
    });
    expect(heading()).toBe("Which language do you prefer?");
    next();
    await waitFor(() => expect(heading()).toBe("How much have you used AI so far?"));
    expect(hardNavigate).not.toHaveBeenCalled();
  });

  it("says so when there are no professions to choose from", async () => {
    getProfessions.mockResolvedValue([]);
    await ready();
    expect(screen.getByRole("alert").textContent).toContain("No professions are set up");
  });

  it("says so when the questions cannot be loaded", async () => {
    getProfile.mockRejectedValue(new ApiProblem(503, "service_unavailable"));
    show(<OnboardingStepper />);
    expect((await screen.findByRole("alert")).textContent).toContain("could not be loaded");
  });
});
