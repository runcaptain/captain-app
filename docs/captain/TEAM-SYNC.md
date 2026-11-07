# Team collaboration screen

The command palette entry **Team collaboration** opens `captain-team`, a native mod
using the shared Captain UI kit. No new core API or model is added.
The team workspace shown at the top is independent of the selected bucket workspace.

**Connect to your team** is the default setup. Enter the server URL, workspace and
access token from the team operator, then choose **Connect**. The token uses native
`InputState.set_masked(true)` and is cleared from the field before submission, on
failure, and on logout. The mod sends it only to the protected local daemon's
`POST /api/team/connect`; it never writes the credential to shared state, files,
messages or diagnostics. Connect failures use fixed wording, never raw response text.
The daemon validates the identity and workspace before returning success and handles
OS keychain persistence. Server URL and workspace remain visible as connection settings.

**Connection settings** lets a connected user replace the token. Cache continuity and
identity isolation are enforced by the daemon's verified user identity, not the entered
token or display name. The UI clears the prior view before loading the verified
connection's records. Signed-out setup works without an SSO provider.

Company SSO is optional and appears only when the daemon advertises `login_available`.
It shows the provider's verification URL and code and polls until accepted, rejected or
expired. No live company identity provider has been verified.

Pull latest reads the remote workspace into the private local cache. Select a record
to edit its JSON object; saving only queues a local draft. New records are private.
Push drafts explicitly publishes pending drafts. A pending draft must be pushed or discarded before
another edit can be saved, preventing duplicate queued revisions. Select a draft and
choose **Discard local draft** to remove it without publishing unwanted content. The
editor retains its text so you can correct it and save again. Discard cannot undo
already published changes. Conflicts require
pulling latest, comparing the current record, then selecting and reviewing the conflicting
proposal before saving its resolution. Resolution retains the original kind and visibility.
The screen limits each list to 200 entries; the CLI remains available for the full list.

The screen clears its displayed records and drafts on an authorization rejection and
offers sign-in again. A 401 preserves queued drafts until renewed access is verified;
a 403 revocation clears the cache and drafts. A known write-role denial preserves
local drafts and the signed-in view, with a message explaining the permission limit. Refresh cannot redisplay a rejected cache
before successful sign-in.
Previously cached data may remain visible offline until the next server contact.
Sign out is handled by the daemon and then the view clears its editor and reloads status.

The editor preserves the revision selected before editing, including across Pull latest.
A changed server history clears the editor and requires reloading. Conflict resolution
also uses the selected revision, so another update cannot be silently overwritten.

## Verification

`node --import ./tests/mod-render/stubs/register.mjs tests/mod-render/captain-team.test.mjs`
passes synthetic interaction checks for device sign-in, local-only save and discard, corrected re-save without publishing, preserved kind
and visibility, conflict resolution, pending-draft protection, authorization rejection,
and logout. These fixtures are not proof of a live login.

Native visual acceptance requires an explicitly coordinated isolated Captain profile.
Keep runtime discovery, credentials and screenshots outside source control. Do not
restart or modify a regular app profile during verification. Synthetic interaction
tests do not prove a live company SSO login or a production deployment.
