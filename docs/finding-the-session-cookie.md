# Finding the USPS session cookie

USPS no longer lets Home Assistant sign in to Informed Delivery directly — it
blocks the sign-in as automated traffic. Instead you sign in with your own
browser, which USPS allows, and hand Home Assistant **one cookie** from that
signed-in session. Home Assistant trades it once for a token it then renews on
its own; the cookie itself is never stored.

You do this once when you set up the integration, and again only if USPS stops
accepting the renewed token — for example after Home Assistant has been offline
long enough for it to expire.

The cookie is named **`dfdc00f11bc995e`** and belongs to **`verified.usps.com`**.
It is an HttpOnly cookie, so you cannot read it from the address bar or the
JavaScript console — you copy it from your browser's cookie view. The idea is
the same in every browser:

1. Sign in at **informeddelivery.usps.com** until you see your dashboard.
2. Open the developer tools and go to the stored-cookies view.
3. Select **`verified.usps.com`**, find the cookie named **`dfdc00f11bc995e`**,
   and copy its **Value** (a long string).
4. Paste that value into the Home Assistant form, together with your USPS
   username, **within a minute or two** — it only works while the browser
   session is still alive.

The sections below show where to click in each browser.

## Google Chrome / Microsoft Edge / Brave

1. Press `F12` (Windows/Linux) or `Cmd+Option+I` (Mac), or right-click the
   page → **Inspect**.
2. Open the **Application** tab (click **»** if it is hidden).
3. In the left sidebar, expand **Cookies** and click
   **`https://verified.usps.com`**.
4. In the list, find the row named **`dfdc00f11bc995e`**, double-click its
   **Value** cell, and copy the whole value.

## Mozilla Firefox

1. Press `F12` or right-click → **Inspect**.
2. Open the **Storage** tab (click **»** if it is hidden).
3. In the left sidebar, expand **Cookies** and click
   **`https://verified.usps.com`**.
4. Find the row named **`dfdc00f11bc995e`** and copy its **Value**.

## Safari

1. Open **Safari → Settings → Advanced** and turn on **"Show features for web
   developers"**.
2. **Develop → Show Web Inspector** (`Cmd+Option+I`), then open the
   **Storage** tab.
3. Select **Cookies → `verified.usps.com`**.
4. Find the row named **`dfdc00f11bc995e`** and copy its **Value**.

## Mobile browsers

Phone browsers have no developer tools with a cookie view. Do the sign-in on a
desktop or laptop browser instead. The cookie isn't tied to a device, so it
doesn't matter where you copy it, as long as you paste it into your Home
Assistant within a minute or two.

## Troubleshooting

- **There is no cookie named `dfdc00f11bc995e`.** Make sure you selected
  **`verified.usps.com`** and not `informeddelivery.usps.com` — the cookie
  lives under the first one, and only after you have fully signed in. If that
  exact name is missing, copy the cookie under `verified.usps.com` whose name
  is a long string of letters and digits.
- **Home Assistant says the browser session could not be used to sign in.** The
  value wasn't a live session: you were signed out, copied the wrong cookie, or
  it expired between copying and pasting. Sign in again at
  informeddelivery.usps.com, copy a fresh value, and paste it straight away.
- **Home Assistant says no address is enrolled in Informed Delivery.** The
  sign-in worked, but the account has no enrolled address. Enroll one at
  informeddelivery.usps.com first, then set up the integration.
