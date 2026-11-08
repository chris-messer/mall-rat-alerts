# Stripe tip jar

**LIVE MODE.** Stripe Payment Link on the Mall Rat Alerts Stripe account. Real money is collected.

- Account: acct_1UYsNRBCOOlJY3M7, display name "Mallrat", country US, charges enabled, statement descriptor MALLRAT
- Payment link URL: https://donate.stripe.com/fZu00i0LjgLfeJFgem8IU00
- Payment link ID: plink_1UYuylBCOOlJY3M7Kq07Nh05
- Product ID: prod_Va2dsebZRCn7jB ("Buy the mall rat a coffee", metadata app=mall-rat-alerts)
- Price ID (used by the link): price_1UYuylBCOOlJY3M7GprOBLqk (one-time USD, custom amount, preset $5, min $1, max $100)
- Unused duplicate price on the same product: price_1UYuykBCOOlJY3M72opJ11BC (identical settings, not on any link; can be archived)
- Options: submit_type=donate, billing address auto, no phone or shipping, promotion codes off, submit message "Thanks for the tip. It helps pay for texts.", statement descriptor suffix MALLRAT, metadata app=mall-rat-alerts
- After-completion redirect: https://mallratalerts.com/thanks/

The live product, prices, and link were created at 13:47 on 2026-11-06 (the product ID matches the test product, consistent with Stripe's copy-to-live). The redirect and descriptor suffix were then set on that link instead of creating duplicates.

## Superseded objects (kept, not deleted)

- Test mode (sandbox acct_1UYsNZBuDtmvA4CO): link plink_1UYsY5BuDtmvA4COk1vAl4Ge (https://donate.stripe.com/test_00w00i8c83nw7eX7gv4F200), product prod_Va2dsebZRCn7jB, price price_1UYsY5BuDtmvA4COUAtnSXan. No longer used by the site.
- Old Versiful business account link plink_1UYsCDBcYhqWB9qE0DfSQuIM (https://donate.stripe.com/dRmeVcdsU9wih0UfJK14400). Replaced; not deactivated from this project. The Versiful account owner should deactivate it there if wanted.

## How the site uses it

`.env` holds `STRIPE_PAYMENT_LINK` (public URL only). `scripts/deploy.sh` exports it as `TF_VAR_stripe_payment_link`, and `terraform/site.tf` replaces the `<!--TIP_JAR-->` placeholder in `site/index.html` with `terraform/tip-jar.html.tpl`. Empty value = no tip button. The deploy script unsets `STRIPE_SECRET_KEY` and `STRIPE_PUBLISHABLE_KEY` before running Terraform; the site never needs either key.

To change the link: update `STRIPE_PAYMENT_LINK` in `.env` and run `scripts/deploy.sh`.

## Update the redirect URL

    POST /v1/payment_links/plink_1UYuylBCOOlJY3M7Kq07Nh05
    after_completion[type]=redirect
    after_completion[redirect][url]=https://mallratalerts.com/thanks/

## Deactivate the link

    POST /v1/payment_links/plink_1UYuylBCOOlJY3M7Kq07Nh05
    active=false

Authenticate with `STRIPE_SECRET_KEY` from `.env` (a live restricted key, `rk_live_`; HTTP basic auth, key as username). Never print it or copy it into site, Lambda, Terraform, or docs.
