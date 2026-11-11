# Participant briefing: template

A template for what to tell pilot participants before they first sign in (gate G11). Copy it,
replace every `<placeholder>`, and **delete or change any sentence that is not true of your
installation** (for example the list of approved tools, the retention periods and the model
provider). Keep the copy with your pilot records, not in the Pharos repository: the repository
never holds your organization's names, addresses or people.

The most important part is "What you must not paste". The others say what the product does so
nobody has to guess.

---

## Pharos pilot: what you should know

**What it is.** Pharos shows short, practical examples of how people in your profession use AI, in
your language. It is a pilot run by `<team or role>` until `<end date>`.

### What you must not paste

Cards sometimes come with a ready-made prompt that you can copy into an AI tool. Before you paste
anything of your own into **any** AI tool, including the ones Pharos links to:

- Do not paste real data about customers, colleagues, suppliers or yourself: names, e-mail addresses,
  phone numbers, account or tax numbers, contract terms, salaries, passwords, keys or tokens.
- Do not paste internal documents, source code, tickets or e-mails that you would not post publicly.
- Replace real values with placeholders (`<customer>`, `<amount>`, `<date>`) and add the real ones
  yourself afterwards.
- If you did paste something you should not have, tell `<contact>` the same day. Nobody will be
  blamed for reporting it; the point is that we can act on it.

An AI tool outside Pharos is governed by that tool's own terms, not by Pharos. Pharos cannot see or
stop what you type there.

### Which tools

`<List the approved tools here, or write: "Pharos offers Copy only; no tool is linked.">` A link
inside Pharos opens only a tool on that list. If you are unsure whether a tool is allowed for your
work, ask `<contact>` first.

### What Pharos stores about you

- Who you are: your account from `<identity provider>`, your name or user name, your e-mail
  address and the department the provider sends.
- Your choices: your profession and language, and what you do with the cards (that you viewed or saved one, marked it useful or not useful, tried it, and
  the minutes you say it saved). This is kept for `<12 months, or your setting>`.
- Administrators see a daily summary of use, as counts. A group with fewer than five people is not
  shown. That lowers the chance that a number points at one person; it does not remove it (an
  administrator who noted earlier counts could still work one out).
- The audit log records administrative actions and also every sign-in and sign-out, with your
  account's identifier and a one-way hash of the address you connected from. It is kept for
  `<365 days, or your setting>` and can be read by `<who>`.
- `<If personal feeds are on: feeds you add yourself are seen only by you. If they are off, delete
  this line.>`

### What goes to an AI model

`<If no model is switched on, write: "No AI model is used inside Pharos.">` Otherwise: the public
text of an article (its title, a short excerpt, the website name and the language) and the
profession the example is written for are sent to `<provider>` to write the summary and the
example. What you type, your name, your department and your own feeds are not sent.

### Your data and your questions

You can ask `<contact>` to see or delete your Pharos data. Deleting removes your profile, your
activity and your own feeds. It does not reach everything at once: backups made earlier keep your
data until they expire, container logs keep what they hold until they rotate, and older audit
entries keep your account's identifier, which leads to no name once you are deleted. If you sign in
through `<identity provider>`, your account there is separate and `<who>` must remove it there too,
or Pharos makes a new empty account at your next sign-in. Questions about this briefing:
`<contact>`.

---

## Notes for the person who sends it

- Send it before the first sign-in, and again if the approved tools or the provider change.
- Record who was briefed and when. That record is the evidence for gate G11.
- This text is a starting point. It is not legal advice; have it read by whoever is responsible for
  data protection in your organization.
- It is in English because the repository is. Translate it for your people (Azerbaijani, Turkish,
  Russian); a translation is yours to keep with your records.
