# Access longevity

GoogleMCP calls Google APIs with one stored refresh token for your Google
account. This page covers how long that token lasts, what ends it, and how
to recover. Facts were checked on 2026-10-05 against the linked Google
pages.

## The 7-day Testing-mode trap

Google's rule, quoted from
[Using OAuth 2.0 to Access Google APIs: Refresh token expiration](https://developers.google.com/identity/protocols/oauth2#expiration):

> A Google Cloud Platform project with an OAuth consent screen configured for
> an external user type and a publishing status of "Testing" is issued a
> refresh token expiring in 7 days, unless the only OAuth scopes requested are
> a subset of name, email address, and user profile (through the
> `userinfo.email, userinfo.profile, openid` scopes, or their OpenID Connect
> equivalents).

A personal @gmail.com account can only use the External user type, and
GoogleMCP requests Gmail, Drive, Calendar, and other API scopes. So while the
app is in Testing, the token stops working 7 days after consent, whether or
not it is used. The
[audience help page](https://support.google.com/cloud/answer/15549945) says
the same from the user side: "Authorizations by a test user will expire seven
days from the time of consent." Testing mode also caps the app at 100 listed
test users.

The symptom is an `invalid_grant` error on token refresh about a week after
`googlemcp auth login`.

## Recommended: In production, unverified, personal use

Set the publishing status to **In production**
([manual-steps.md, step 3](manual-steps.md#3-google-auth-platform-audience-and-test-user))
and do not submit for verification. Google allows this for personal use
([Exceptions to verification requirements](https://support.google.com/cloud/answer/13464323)):

> If the app is for your personal use (fewer than 100 users), you and your
> limited number of users can continue using the app without going through
> verification (users will be allowed to click through "unverified app"
> warning screens during sign-in).

What that means in practice:

- **Unverified app screen.** Each consent shows "Google hasn't verified this
  app" because the app requests sensitive and restricted scopes. Click
  **Advanced**, then **Go to GoogleMCP (unsafe)**. This is expected for an
  app you created for yourself. The same screen appears in Testing.
- **User cap.** Unverified apps are limited to "100 new users in total, after
  the app presents the unverified app screen"
  ([Unverified apps](https://support.google.com/cloud/answer/7454865)). The
  cap is a lifetime count of new users, so keep the app to yourself.
- **No 7-day expiry.** Tokens follow the normal rules below instead.

Do not add other people's accounts. The personal-use exception assumes the
app is for your own use, and the server acts only as the account that signed
in with `googlemcp auth login`. Anyone else who wants GoogleMCP should create
their own project and OAuth client.

## Restricted scopes

Gmail scopes and full Drive scopes are restricted. Public apps that request
them must pass verification and "undergo an annual security assessment"
([Restricted scope verification](https://support.google.com/cloud/answer/13465431)).
That page does not mention personal use. The assessment is part of
verification, and the personal-use exception above waives verification, so a
single-user unverified app does not need it. Google does not state that
inference directly on the restricted-scope page; if Google sends a
verification request for this project, answer that it is personal use.

To reduce exposure, GoogleMCP requests read scopes by default and adds write
scopes only for products listed in `GOOGLEMCP_WRITE_PRODUCTS`. Disable Gmail
or Drive in Terraform (`products = { gmail = false }`) and the server if you
do not need them.

## What ends a refresh token

From [Refresh token expiration](https://developers.google.com/identity/protocols/oauth2#expiration),
a refresh token can stop working when:

| Cause | Google's wording | Notes for GoogleMCP |
|---|---|---|
| Revocation | "The user has revoked your app's access." | Removing GoogleMCP at <https://myaccount.google.com/connections> or running `googlemcp auth revoke` |
| Inactivity | "The refresh token has not been used for six months." | Any server use refreshes it; a server idle for six months needs a new login |
| Password change | "The user changed passwords and the refresh token contains Gmail scopes." | Applies whenever Gmail is enabled |
| Token count | "There is currently a limit of 100 refresh tokens per Google Account per OAuth 2.0 client ID. If the limit is reached, creating a new refresh token automatically invalidates the oldest refresh token without warning." | Repeated logins with the same client on many machines can silently invalidate an older token; share one token through Secret Manager instead |
| Time-based access | "The user granted time-based access to your app and the access expired." | Only if a time limit was chosen at consent |
| Testing mode | 7-day rule above | Avoided by In production |
| Admin policy | `admin_policy_enforced` | Workspace accounts only |

Separately, OAuth clients unused for six months are deleted, with an email
warning 30 days before
([Manage OAuth clients](https://support.google.com/cloud/answer/15549257)).

## Recovery

Commands use the default keyring store. With the optional Secret Manager
backend, set `GOOGLEMCP_GCP_PROJECT=PROJECT_ID` and add
`--secret-backend secretmanager` to each `googlemcp` command.

1. Confirm the problem: `uv run googlemcp auth status`. An `invalid_grant` error
   means the refresh token is no longer valid.
2. Sign in again:

   ```sh
   uv run googlemcp auth login
   ```

   This stores a new refresh token (with Secret Manager, a new version of
   `googlemcp-refresh-token`). The local server reads the store when it
   needs a token, so restart it only if it keeps failing.
3. If the app is still in Testing, switch it to In production now, or this
   repeats in 7 days.
4. If the client itself is gone (deleted after six months idle), create a new
   Desktop client ([manual-steps.md, section 5](manual-steps.md#5-desktop-oauth-client-for-googlemcp-auth-login))
   and log in with `--client-secrets`.
5. Secret Manager backend only: old refresh-token versions can be disabled
   once step 1's `auth status` passes with the new token:
   `gcloud secrets versions list googlemcp-refresh-token --project=PROJECT_ID`,
   then `gcloud secrets versions disable VERSION --secret=googlemcp-refresh-token --project=PROJECT_ID`
   for older versions only. Never disable the latest version; the CLI and
   server read `latest`.
