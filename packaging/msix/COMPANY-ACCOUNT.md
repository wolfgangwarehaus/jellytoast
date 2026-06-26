# Future project: move jellytoast to a Microsoft Store **Company** account

**Status: deferred / future to-do** (decided 2026-06-26). Not blocking any release.

## Why bother

A Company Store account unlocks two things an Individual account can't have:

1. **Hands-off Store update automation.** The `msstore` submission API authenticates
   through a Microsoft Entra *application*, and the "Microsoft Entra applications"
   tab needed to authorize it is **Company-only**. With a Company account, the
   already-built [`msstore.yml`](../../.github/workflows/msstore.yml) auto-submits
   every release (Option A) — no manual upload. (Today, on the Individual account,
   we use **Option B**: CI builds the `.msix` and attaches it to the release; you
   upload it to Partner Center by hand. See [STORE-AUTOMATION.md](STORE-AUTOMATION.md).)
2. **A company publisher name** — the Store listing shows **"Wolfgang Warehaus"**
   instead of a personal legal name.

## The catch (read before starting)

- **No conversion.** Individual→Company is *not* supported — you create a **new**
  Company account. ([source](https://learn.microsoft.com/en-us/windows/apps/publish/partner-center/open-a-developer-account))
- **Your live listing doesn't auto-transfer.** Product `9PNLTPXGHN79` stays on the
  Individual account. You must decide:
  - **Re-publish** jellytoast under the Company account → it gets a **new Store ID**,
    so the website/README deep-links (`apps.microsoft.com/detail/9PNLTPXGHN79`) change
    and existing installs won't auto-migrate to the new listing; **or**
  - **Request a transfer** via a Microsoft support ticket (Partner Center → Support →
    Non-technical / Dashboard → submit an incident). It's undocumented and not
    guaranteed, "may include verification from both accounts and republishing."
    ([Q&A](https://learn.microsoft.com/en-us/answers/questions/716594/i-need-to-transfer-application-to-another-vendor))
  - Decide this **before** creating the account — it's the real cost of the move.

## Prerequisites (all free)

- **Registration: $0** (Microsoft dropped the company fee in May 2026).
- **Business identity (a "Trader"):** a sole proprietor qualifies — no incorporation
  required. Verify with **either** a **D‑U‑N‑S number** (free from Dun & Bradstreet,
  but the free tier can take up to ~30 days — start this early) **or** business
  documents (registration/license/tax filing → manual review, 2–5 business days).
  ([reqs](https://learn.microsoft.com/en-us/windows/apps/publish/store-business-verification-reqs))
- **A work email on your own domain** — Gmail/Yahoo are rejected. ✅ already covered:
  make `august@wolfgangwarehaus.com` via the existing Cloudflare email routing.

## Steps (when you decide to do it)

1. Resolve the **listing decision** above (re-publish vs. support transfer).
2. (If going the D‑U‑N‑S route) **request a free D‑U‑N‑S** now — it's the long pole.
3. Set up the **`august@wolfgangwarehaus.com`** mailbox/alias (Cloudflare routing).
4. Go to **<https://storedeveloper.microsoft.com>** → **Get started** → **Company
   account** → sign in (you can use the existing `wolfgangwarehaus66` Entra work
   account, which onboards the whole tenant — handy for the API later).
5. Enter business details (D‑U‑N‑S or docs), contact details (the domain email),
   accept the agreement, complete due diligence + business + employment verification
   (auto in minutes, or 2–5 business days if it goes to manual review).
6. Publish jellytoast on the Company account (per the listing decision in step 1).
7. **Turn on Option A automation:** the tenant is already associated (step 4), so —
   register/​reuse the Entra app (`jellytoast-store-ci`), add it under **User
   management → Microsoft Entra applications** with the **Manager** role, and set the
   four repo secrets `AZURE_AD_TENANT_ID` / `AZURE_AD_APPLICATION_CLIENT_ID` /
   `AZURE_AD_APPLICATION_SECRET` / `SELLER_ID` (use the **new** account's Seller ID).
   `msstore.yml` flips from "attach the `.msix`" to "auto-submit" with no code change.
   Full detail: [STORE-AUTOMATION.md](STORE-AUTOMATION.md).

## Don't forget after the move

- Update the website/README Store links if the **Store ID changed** (re-publish path).
- Point any "Microsoft Store" badge/links at the new listing.
- Calendar the **Entra client-secret rotation** (it expires).
