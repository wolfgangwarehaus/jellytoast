# Signing the GitHub Windows artifacts (Azure Artifact Signing)

This signs the **direct-download** `.exe` / portable `.zip`. (The Microsoft
Store path re-signs its MSIX separately — see `packaging/msix/`.) Authenticode
signing shows your verified name instead of "unknown publisher" and starts
building SmartScreen reputation — it does **not** clear the warning on day one;
that accrues with download volume. EV no longer buys an instant bypass (removed
2024), so this OV-class signing is the right tool.

`release.yml`'s `build-windows` job has two signing steps that are **skipped
until the secrets below exist** (`SIGN` resolves to `false`), so the release
pipeline keeps building unsigned in the meantime.

## One-time setup (you're eligible: US/Canada individual)

1. Create an **Azure Artifact Signing** account on a *paid* Azure subscription
   (free/trial/sponsored subscriptions are rejected). Pick a region, e.g.
   East US (`https://eus.codesigning.azure.net/`) or West US 2.
2. Complete **individual identity validation** (government ID + selfie via
   Microsoft Verified ID / AU10TIX). 1–20 business days.
3. Create a **Public Trust certificate profile**.
4. Create an Entra **service principal** and grant it the
   **"Artifact Signing Certificate Profile Signer"** role on the account.
5. Add these repo **secrets** (Settings → Secrets and variables → Actions):

   | Secret | Value |
   | --- | --- |
   | `AZURE_TENANT_ID` | Entra tenant id |
   | `AZURE_CLIENT_ID` | service principal application (client) id |
   | `AZURE_CLIENT_SECRET` | service principal secret |
   | `AZURE_SIGNING_ENDPOINT` | region URL, e.g. `https://eus.codesigning.azure.net/` |
   | `AZURE_SIGNING_ACCOUNT` | signing account name |
   | `AZURE_SIGNING_PROFILE` | certificate profile name |

Once `AZURE_CLIENT_ID` is present, both signing steps activate automatically on
the next tagged release. Cost: ~$9.99/mo (Basic tier, 5,000 signatures).
Timestamping is mandatory (certs are ~3-day) and is already wired
(`http://timestamp.acs.microsoft.com`).

> **Action version:** the steps use `azure/trusted-signing-action@v0` (stable,
> well-documented inputs). Microsoft also republished it as
> `azure/artifact-signing-action` after the 2026 rename — migrate when you bump.
