# Customer context and value

All of this is an assumption made for the exercise. The brief is intentionally vague.

## The customer (fictional)

**Aurora Home**, a mid-size European online retailer with about 400,000 contacts, selling to EU
customers, so GDPR applies. A small compliance team (2 people) and a marketing team share two
products that do not integrate well:

| | Consent management platform | Email marketing platform |
|---|---|---|
| Role | Legal record of who consented to what, and when | Sends campaigns. Customers also unsubscribe here, through the link in each email |
| Used by | Compliance, legal, the data protection officer | Marketing |

## The manual process today

- Every week a compliance analyst exports a CSV from the consent platform and uploads it to the email platform.
- Unsubscribes made in the email platform are re-keyed into the consent platform by hand, often days later.

## The problems this causes

- People who withdrew consent can still receive campaigns (legal and reputational risk).
- A stale export can silently re-subscribe someone who already unsubscribed.
- The legal record is wrong for days, and re-keying loses the original timestamp of the withdrawal.
- Nobody can say which system is right.

## What the glue service does

It reconciles consent status per contact between the two systems on a schedule. Withdrawals flow in
both directions and are recorded with the customer's original timestamp. Opt-ins only flow from the
consent platform, because only it holds the proof of consent. Failures are retried, permanent
failures are parked in a dead-letter list for a human, and every decision is audited.

## Value (estimates, to be validated with the customer)

- About 1 to 2 analyst-days per week of manual export and re-keying removed.
- Delay between a withdrawal and suppression reduced from days to minutes (one sync interval).
- Every consent decision is traceable: which rule, when, and from which source.

## Explicitly out of scope

UI, authentication, real vendor APIs, cloud deployment. See "Known limitations" in the README.
